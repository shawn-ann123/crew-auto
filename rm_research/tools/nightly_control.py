"""夜间任务的时间闸门、进度快照和产出验收。"""

from __future__ import annotations

import argparse
from datetime import datetime, time, timedelta
import json
from pathlib import Path
from zoneinfo import ZoneInfo

from tools.state_io import atomic_write_json, load_json_strict


SHANGHAI = ZoneInfo("Asia/Shanghai")


def parse_clock(value: str) -> time:
    try:
        return time.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"时间必须使用 HH:MM 格式：{value}") from exc


def is_deepseek_peak(moment: datetime) -> bool:
    """按官方北京时间峰时表保守判断；法定节假日仍按工作日处理。"""
    local = moment.astimezone(SHANGHAI)
    if local.weekday() >= 5:
        return False
    current = local.time().replace(tzinfo=None)
    return time(9) <= current < time(12) or time(14) <= current < time(18)


def seconds_until_peak(moment: datetime) -> int:
    """返回距下一个保守峰时起点的秒数。"""
    local = moment.astimezone(SHANGHAI)
    for offset in range(8):
        day = (local + timedelta(days=offset)).date()
        if day.weekday() >= 5:
            continue
        for boundary in (time(9), time(14)):
            candidate = datetime.combine(day, boundary, SHANGHAI)
            if candidate > local:
                return max(0, int((candidate - local).total_seconds()))
    raise RuntimeError("无法计算下一个 DeepSeek 峰时时间")


def in_window(current: time, start: time, latest: time) -> bool:
    if start <= latest:
        return start <= current <= latest
    return current >= start or current <= latest


def counts(root: Path) -> dict[str, int]:
    processed = load_json_strict(root / "library" / "processed.json", {})
    solutions = load_json_strict(root / "library" / "solutions.json", {})
    if not isinstance(processed, dict):
        raise RuntimeError("library/processed.json 结构不正确")
    if not isinstance(solutions, dict) or not isinstance(solutions.get("solutions", []), list):
        raise RuntimeError("library/solutions.json 结构不正确")
    result = {
        "papers": len(processed.get("papers", [])),
        "repos": len(processed.get("repos", [])),
        "cross": len(processed.get("cross", [])),
        "proposals": len(solutions.get("solutions", [])),
    }
    result["evidence"] = result["papers"] + result["repos"] + result["cross"]
    return result


def prepare(root: Path, date: str) -> Path:
    path = root / "logs" / f"nightly-{date}.state.json"
    if path.exists():
        state = load_json_strict(path, {})
        if not isinstance(state, dict):
            raise RuntimeError(f"夜间状态文件结构不正确：{path}")
        if state.get("status") not in {"DONE", "BELOW_STANDARD"}:
            state["status"] = "RUNNING"
            state["resumed_at"] = datetime.now(SHANGHAI).isoformat(timespec="seconds")
            state.pop("failure", None)
            atomic_write_json(path, state)
    else:
        atomic_write_json(
            path,
            {
                "date": date,
                "status": "RUNNING",
                "started_at": datetime.now(SHANGHAI).isoformat(timespec="seconds"),
                "before": counts(root),
            },
        )
    return path


def mark_failed(root: Path, date: str, stage: str, exit_code: int, reason: str) -> Path:
    path = root / "logs" / f"nightly-{date}.state.json"
    state = load_json_strict(path, {})
    if not isinstance(state, dict):
        state = {"date": date}
    state.update(
        {
            "status": "FAILED",
            "failed_at": datetime.now(SHANGHAI).isoformat(timespec="seconds"),
            "failure": {
                "stage": stage,
                "exit_code": exit_code,
                "reason": reason,
            },
        }
    )
    atomic_write_json(path, state)
    return path


def verify(root: Path, date: str, min_evidence: int, min_proposals: int) -> int:
    state_path = root / "logs" / f"nightly-{date}.state.json"
    state = load_json_strict(state_path, {})
    before = state.get("before") if isinstance(state, dict) else None
    if not isinstance(before, dict):
        raise RuntimeError(f"缺少夜间运行基线：{state_path}")
    after = counts(root)
    added = {key: after[key] - int(before.get(key, 0)) for key in after}
    problems: list[str] = []
    if added["evidence"] < min_evidence:
        problems.append(f"新增证据 {added['evidence']}，低于标准 {min_evidence}")
    if added["proposals"] < min_proposals:
        problems.append(f"新增方案 {added['proposals']}，低于标准 {min_proposals}")
    summary = {
        **state,
        "status": "DONE" if not problems else "BELOW_STANDARD",
        "finished_at": datetime.now(SHANGHAI).isoformat(timespec="seconds"),
        "after": after,
        "added": added,
        "standards": {
            "minimum_evidence": min_evidence,
            "minimum_proposals": min_proposals,
        },
        "problems": problems,
    }
    atomic_write_json(state_path, summary)
    atomic_write_json(root / "logs" / f"nightly-summary-{date}.json", summary)
    print(json.dumps(summary, ensure_ascii=False))
    return 0 if not problems else 6


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)

    policy = sub.add_parser("policy")
    policy.add_argument("--auto", action="store_true")
    policy.add_argument("--start", default="00:30")
    policy.add_argument("--latest", default="02:00")

    remaining = sub.add_parser("remaining")
    remaining.add_argument("--deadline", type=int, required=True)
    remaining.add_argument("--safety-seconds", type=int, default=60)

    prep = sub.add_parser("prepare")
    prep.add_argument("--date", required=True)

    check = sub.add_parser("verify")
    check.add_argument("--date", required=True)
    check.add_argument("--min-evidence", type=int, required=True)
    check.add_argument("--min-proposals", type=int, required=True)

    failed = sub.add_parser("fail")
    failed.add_argument("--date", required=True)
    failed.add_argument("--stage", required=True)
    failed.add_argument("--exit-code", type=int, required=True)
    failed.add_argument("--reason", required=True)

    args = parser.parse_args()
    root = Path.cwd()
    now = datetime.now(SHANGHAI)

    if args.command == "policy":
        if args.auto and not in_window(
            now.time().replace(tzinfo=None), parse_clock(args.start), parse_clock(args.latest)
        ):
            print("当前不在配置的夜间自动启动窗口。")
            return 8
        if is_deepseek_peak(now):
            print("当前是 DeepSeek 高峰计价时段，nightly 不启动。")
            return 7
        print("DeepSeek 谷时检查通过。")
        return 0
    if args.command == "remaining":
        allowed = min(args.deadline - int(now.timestamp()), seconds_until_peak(now) - args.safety_seconds)
        print(max(0, allowed))
        return 0
    if args.command == "prepare":
        print(prepare(root, args.date))
        return 0
    if args.command == "verify":
        return verify(root, args.date, args.min_evidence, args.min_proposals)
    if args.command == "fail":
        print(mark_failed(root, args.date, args.stage, args.exit_code, args.reason))
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
