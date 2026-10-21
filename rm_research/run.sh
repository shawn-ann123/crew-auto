#!/usr/bin/env bash
# 四阶段研究流水线入口。日常使用只需 nightly 和 code。
set -euo pipefail

cd "$(dirname "$0")"

mkdir -p logs queue library nights proposals code baseline

exec 9>logs/run.lock
if ! flock -n 9; then
  echo "已有一轮研究在运行，本次不重复启动。" >&2
  exit 2
fi

if [[ -f STOP ]]; then
  echo "检测到 STOP 文件，已停止启动。删除 STOP 后可恢复。" >&2
  exit 3
fi

for var in ALL_PROXY all_proxy HTTPS_PROXY https_proxy HTTP_PROXY http_proxy; do
  val="${!var:-}"
  case "$val" in
    socks://*|socks5://*|socks5h://*)
      export "$var=http://${val#*://}"
      ;;
  esac
done

PYTHON_BIN="${PYTHON_BIN:-$PWD/.venv/bin/python}"
if [[ ! -x "$PYTHON_BIN" ]]; then
  echo "项目虚拟环境不可用：$PYTHON_BIN。请重建 .venv。" >&2
  exit 4
fi
if ! "$PYTHON_BIN" -c 'import crewai, crewai_cli' >/dev/null 2>&1; then
  echo "项目虚拟环境不完整：无法导入 CrewAI。请重建 .venv。" >&2
  exit 4
fi
if [[ ! -f .env ]]; then
  echo "缺少模型配置文件 .env。请先复制 .env.example 并填写模型地址和 API Key。" >&2
  exit 4
fi

# 工作量、调度和容错参数也统一从 .env 读取。
set -a
# shellcheck disable=SC1091
source .env
set +a

stage="${1:-nightly}"
date="${DATE:-$(date +%F)}"
papers="${NIGHTLY_PAPERS:-${PAPERS:-2}}"
repos="${NIGHTLY_REPOS:-${REPOS:-1}}"
cross="${NIGHTLY_CROSS:-${CROSS:-1}}"
min_evidence="${NIGHTLY_MIN_EVIDENCE:-1}"
min_proposals="${NIGHTLY_MIN_PROPOSALS:-0}"
max_runtime_minutes="${NIGHTLY_MAX_RUNTIME_MINUTES:-360}"
retry_max="${NIGHTLY_RETRY_MAX:-3}"
retry_delay="${NIGHTLY_RETRY_DELAY_SECONDS:-60}"
nightly_start="${NIGHTLY_START_TIME:-00:30}"
nightly_latest="${NIGHTLY_LATEST_START_TIME:-02:00}"
auto_mode=0
if [[ "$stage" == "auto-nightly" ]]; then
  auto_mode=1
  stage="nightly"
fi

for item in "$papers" "$repos" "$cross" "$min_evidence" "$min_proposals"; do
  if [[ ! "$item" =~ ^[0-9]+$ ]] || (( item > 50 )); then
    echo "夜间工作量与产出标准必须是 0-50 的整数。" >&2
    exit 4
  fi
done
for item in "$max_runtime_minutes" "$retry_max" "$retry_delay"; do
  if [[ ! "$item" =~ ^[0-9]+$ ]]; then
    echo "夜间时长和重试参数必须是非负整数。" >&2
    exit 4
  fi
done
if (( max_runtime_minutes < 1 || retry_max < 1 )); then
  echo "NIGHTLY_MAX_RUNTIME_MINUTES 和 NIGHTLY_RETRY_MAX 必须至少为 1。" >&2
  exit 4
fi
if [[ ! "$date" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]]; then
  echo "DATE 必须使用 YYYY-MM-DD 格式。" >&2
  exit 4
fi

if [[ ! -f logs/run_stats.csv ]]; then
  echo "date,stage,started_at,duration_sec,status" > logs/run_stats.csv
fi

stage_done() {
  local stage_name="$1"
  [[ "${FORCE:-0}" != "1" ]] && \
    [[ -f "logs/${stage_name}-${date}.json" ]] && \
    grep -q '"status": "DONE"' "logs/${stage_name}-${date}.json"
}

nightly_done() {
  [[ -f "logs/nightly-summary-${date}.json" ]] && \
    grep -qE '"status": "(DONE|BELOW_STANDARD)"' "logs/nightly-summary-${date}.json"
}

run_stage_once() {
  local stage_name="$1"
  local crew_file="$2"
  local timeout_seconds="$3"
  shift 3

  local started_at start_ts end_ts duration log_file log_start exit_code
  started_at="$(date -Is)"
  start_ts="$(date +%s)"
  log_file="logs/${stage_name}-${date}.log"
  log_start="$(stat -c %s "$log_file" 2>/dev/null || echo 0)"
  LAST_FAILURE_IS_LOCAL=0

  echo "=== ${stage_name} 开始 ${started_at} ===" | tee -a "$log_file"
  set +e
  timeout --signal=TERM --kill-after=30s "${timeout_seconds}s" \
    env CREWAI_DMN=1 "$PYTHON_BIN" -m tools.run_json_crew \
      --crew "$crew_file" "$@" 2>&1 | tee -a "$log_file"
  exit_code=${PIPESTATUS[0]}
  set -e

  end_ts="$(date +%s)"
  duration=$((end_ts - start_ts))
  if (( exit_code != 0 )); then
    if tail -c "+$((log_start + 1))" "$log_file" | \
      grep -qE 'Failed to spawn|bad interpreter|ModuleNotFoundError|ImportError|No such file or directory'; then
      LAST_FAILURE_IS_LOCAL=1
    fi
    echo "$date,$stage_name,$started_at,$duration,FAILED" >> logs/run_stats.csv
    echo "${stage_name} 运行失败，请查看 $log_file" >&2
    return "$exit_code"
  fi

  if ! env XDG_DATA_HOME="${XDG_DATA_HOME:-/tmp/rm-research-xdg}" \
    "$PYTHON_BIN" -m tools.validate_stage \
      --stage "$stage_name" --date "$date" --duration "$duration"; then
    echo "$date,$stage_name,$started_at,$duration,BLOCKED" >> logs/run_stats.csv
    return 5
  fi
  echo "$date,$stage_name,$started_at,$duration,DONE" >> logs/run_stats.csv
  echo "=== ${stage_name} 完成，耗时 ${duration}s ===" | tee -a "$log_file"
}

run_stage_resilient() {
  local stage_name="$1"
  local crew_file="$2"
  shift 2

  if stage_done "$stage_name"; then
    echo "${stage_name} 已通过校验，从断点继续，跳过重复执行。"
    return 0
  fi

  local attempt remaining exit_code
  for ((attempt = 1; attempt <= retry_max; attempt++)); do
    remaining="$("$PYTHON_BIN" -m tools.nightly_control remaining \
      --deadline "$nightly_deadline")"
    if (( remaining < 60 )); then
      echo "已到本晚时长上限或即将进入 DeepSeek 高峰时段，停止新尝试。" >&2
      return 7
    fi
    echo "${stage_name}：第 ${attempt}/${retry_max} 次尝试，可用时间 ${remaining}s。"
    if run_stage_once "$stage_name" "$crew_file" "$remaining" "$@"; then
      return 0
    else
      exit_code=$?
    fi
    if (( LAST_FAILURE_IS_LOCAL == 1 )); then
      echo "${stage_name} 是本地运行环境故障，自动重连无法解决，本次立即停止。" >&2
      return 4
    fi
    if (( attempt < retry_max )); then
      remaining="$("$PYTHON_BIN" -m tools.nightly_control remaining \
        --deadline "$nightly_deadline")"
      if (( remaining <= retry_delay + 60 )); then
        echo "剩余谷时时间不足以重试。" >&2
        return "$exit_code"
      fi
      echo "${stage_name} 将在 ${retry_delay}s 后自动重连并恢复。" >&2
      sleep "$retry_delay"
    fi
  done
  return "$exit_code"
}

case "$stage" in
  baseline)
    if [[ -f baseline/模块基线.md && -f baseline/检索计划.md && "${FORCE:-0}" != "1" ]]; then
      echo "研究基线已存在，不会自动覆盖。确认重做时使用 FORCE=1 ./run.sh baseline。" >&2
      exit 1
    fi
    run_stage_once baseline crew.a-baseline.jsonc "${STAGE_TIMEOUT_SECONDS:-7200}"
    ;;
  nightly)
    if [[ ! -f baseline/模块基线.md || ! -f baseline/检索计划.md ]]; then
      echo "请先运行 ./run.sh baseline 建立研究基线。" >&2
      exit 1
    fi
    policy_args=(policy --start "$nightly_start" --latest "$nightly_latest")
    if (( auto_mode == 1 )); then
      policy_args+=(--auto)
    fi
    set +e
    "$PYTHON_BIN" -m tools.nightly_control "${policy_args[@]}"
    policy_status=$?
    set -e
    if (( policy_status == 8 && auto_mode == 1 )); then
      exit 0
    fi
    if (( policy_status != 0 )); then
      exit "$policy_status"
    fi
    if (( auto_mode == 1 )) && nightly_done; then
      echo "${date} 的夜间研究已经完成。"
      exit 0
    fi
    nightly_deadline=$(( $(date +%s) + max_runtime_minutes * 60 ))
    "$PYTHON_BIN" -m tools.nightly_control prepare --date "$date"
    inputs_b="$(printf '{"date":"%s","papers_per_night":%s,"repos_per_night":%s,"cross_per_night":%s}' "$date" "$papers" "$repos" "$cross")"
    if run_stage_resilient digest-b crew.b-digest.jsonc --inputs "$inputs_b"; then
      :
    else
      exit_code=$?
      "$PYTHON_BIN" -m tools.nightly_control fail --date "$date" \
        --stage digest-b --exit-code "$exit_code" --reason "digest-b 运行失败"
      exit "$exit_code"
    fi
    inputs_c="$(printf '{"date":"%s","minimum_proposals":%s}' "$date" "$min_proposals")"
    if run_stage_resilient digest-c crew.c-synthesis.jsonc --inputs "$inputs_c"; then
      :
    else
      exit_code=$?
      "$PYTHON_BIN" -m tools.nightly_control fail --date "$date" \
        --stage digest-c --exit-code "$exit_code" --reason "digest-c 运行失败"
      exit "$exit_code"
    fi
    set +e
    "$PYTHON_BIN" -m tools.nightly_control verify \
      --date "$date" --min-evidence "$min_evidence" --min-proposals "$min_proposals"
    verify_status=$?
    set -e
    if (( verify_status == 6 )); then
      echo "本晚流程已完成，但产出低于 .env 中设定的标准。" >&2
    elif (( verify_status != 0 )); then
      "$PYTHON_BIN" -m tools.nightly_control fail --date "$date" \
        --stage verify --exit-code "$verify_status" --reason "夜间产出验收失败"
      exit "$verify_status"
    fi
    ;;
  synthesis)
    if [[ ! -f "nights/$date.md" ]]; then
      echo "缺少当日证据清单，不能单独综合。" >&2
      exit 1
    fi
    inputs_c="$(printf '{"date":"%s","minimum_proposals":0}' "$date")"
    run_stage_once synthesis crew.c-synthesis.jsonc "${STAGE_TIMEOUT_SECONDS:-7200}" --inputs "$inputs_c"
    ;;
  code)
    if [[ ! -f 审核方案.md ]] || ! grep -qE '^[[:space:]]*- \[x\][[:space:]]+采纳' 审核方案.md; then
      echo "《审核方案.md》中没有已采纳的方案。" >&2
      exit 1
    fi
    run_stage_once code crew.d-code.jsonc "${STAGE_TIMEOUT_SECONDS:-7200}"
    ;;
  *)
    echo "用法：./run.sh [baseline|nightly|auto-nightly|synthesis|code]" >&2
    exit 1
    ;;
esac
