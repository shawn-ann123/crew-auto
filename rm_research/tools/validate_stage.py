"""阶段闸门：用确定性规则判断是否可以进入下一阶段。"""

from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
import re

from tools.state_io import atomic_write_json, load_json_strict


def _nonempty(path: Path, minimum: int = 20) -> None:
    if not path.is_file() or path.stat().st_size < minimum:
        raise RuntimeError(f"缺少或产物过短：{path}")


def validate(stage: str, date: str, root: Path) -> list[str]:
    artifacts: list[Path] = []
    processed = root / "library" / "processed.json"
    solutions = root / "library" / "solutions.json"

    if stage == "baseline":
        baseline = root / "baseline" / "模块基线.md"
        plan = root / "baseline" / "检索计划.md"
        _nonempty(baseline, 1000)
        _nonempty(plan, 500)
        module_count = len(re.findall(r"^##\s+M\d+\b", baseline.read_text(encoding="utf-8"), re.M))
        if not 10 <= module_count <= 20:
            raise RuntimeError(f"模块数异常：{module_count}")
        artifacts = [baseline, plan]
    elif stage == "digest-b":
        night = root / "nights" / f"{date}.md"
        _nonempty(night, 200)
        text = night.read_text(encoding="utf-8")
        if re.search(r"(?:总体)?状态\s*[:：]\s*(?:BLOCKED|NEEDS_CONTEXT)", text):
            raise RuntimeError("夜间记录标记为阻断，不进入综合阶段。")
        data = load_json_strict(processed, {})
        if not isinstance(data, dict) or not all(k in data for k in ("papers", "repos", "cross")):
            raise RuntimeError("证据账本结构不正确。")
        artifacts = [night, processed]
    elif stage in {"digest-c", "synthesis"}:
        review = root / "审核方案.md"
        _nonempty(review, 100)
        data = load_json_strict(solutions, {})
        if not isinstance(data, dict) or not isinstance(data.get("solutions"), list):
            raise RuntimeError("方案账本结构不正确。")
        artifacts = [review, solutions, root / "proposals" / "审校报告.md"]
    elif stage == "code":
        tasks = root / "code" / "实施任务书.md"
        tests = root / "code" / "验收用例.md"
        _nonempty(tasks, 100)
        _nonempty(tests, 100)
        artifacts = [tasks, tests]
    else:
        raise RuntimeError(f"未知阶段：{stage}")
    return [str(path.relative_to(root)) for path in artifacts if path.exists()]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--stage", required=True)
    parser.add_argument("--date", required=True)
    parser.add_argument("--duration", type=int, default=0)
    args = parser.parse_args()
    root = Path.cwd()
    manifest_path = root / "logs" / f"{args.stage}-{args.date}.json"
    try:
        artifacts = validate(args.stage, args.date, root)
        manifest = {
            "stage": args.stage,
            "date": args.date,
            "status": "DONE",
            "validated_at": datetime.now().isoformat(timespec="seconds"),
            "duration_sec": args.duration,
            "artifacts": artifacts,
        }
        atomic_write_json(manifest_path, manifest)
        print(f"阶段校验通过：{args.stage}")
        return 0
    except Exception as exc:
        atomic_write_json(
            manifest_path,
            {
                "stage": args.stage,
                "date": args.date,
                "status": "BLOCKED",
                "validated_at": datetime.now().isoformat(timespec="seconds"),
                "duration_sec": args.duration,
                "error": str(exc),
            },
        )
        print(f"阶段校验失败：{exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
