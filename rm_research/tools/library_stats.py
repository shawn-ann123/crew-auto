"""文献库统计工具：给检索员提供模块覆盖情况，避免每晚重复搜同一批模块。"""

import json
import re
from datetime import datetime
from pathlib import Path

from crewai.tools import BaseTool
from pydantic import BaseModel, Field


class LibraryStatsInput(BaseModel):
    modules: str = Field(
        "",
        description='可选：模块号列表，逗号分隔，例如 "M1,M2,M3"；用于计算未覆盖模块',
    )


def _load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


class LibraryStatsTool(BaseTool):
    name: str = "library_stats"
    description: str = (
        "统计文献库现状：论文/仓库/跨域数量、每个模块的覆盖度、最近保存的证据、当前方案数，"
        "并给出论文覆盖最少的模块建议。用于决定今晚检索哪个模块。"
    )
    args_schema: type[BaseModel] = LibraryStatsInput

    def _run(self, modules: str = "") -> str:
        root = Path.cwd()
        processed = _load_json(root / "library" / "processed.json", {})
        store = _load_json(root / "library" / "solutions.json", [])
        if isinstance(store, dict):
            store = store.get("solutions", [])

        papers = processed.get("papers") or {}
        repos = processed.get("repos") or {}
        cross = processed.get("cross") or {}

        requested = [m.strip().upper() for m in re.split(r"[,，;；\s]+", modules or "") if m.strip()]
        found = set(requested)
        for bucket in (papers, repos, cross, store):
            if isinstance(bucket, dict):
                for record in bucket.values():
                    found.update(record.get("modules") or [])
            else:
                for record in bucket:
                    module = record.get("module")
                    if module:
                        found.add(module)

        counts: dict[str, dict[str, int]] = {
            m: {"papers": 0, "repos": 0, "cross": 0, "solutions": 0} for m in found
        }
        for kind, bucket in (("papers", papers), ("repos", repos), ("cross", cross)):
            for record in bucket.values():
                for module in record.get("modules") or []:
                    counts.setdefault(module, {"papers": 0, "repos": 0, "cross": 0, "solutions": 0})
                    counts[module][kind] += 1
        for record in store:
            module = record.get("module")
            if module:
                counts.setdefault(module, {"papers": 0, "repos": 0, "cross": 0, "solutions": 0})
                counts[module]["solutions"] += 1

        lines = [
            f"# 文献库统计（{datetime.now():%Y-%m-%d %H:%M}）",
            "",
            f"论文 {len(papers)} 篇 ｜ 仓库 {len(repos)} 个 ｜ 跨域 {len(cross)} 篇 ｜ 方案 {len(store)} 条",
            "",
            "## 模块覆盖（论文 / 仓库 / 跨域 / 方案）",
        ]
        if counts:
            ordered = sorted(
                counts.items(),
                key=lambda kv: (int(kv[0][1:]) if kv[0][1:].isdigit() else 999, kv[0]),
            )
            for module, stat in ordered:
                lines.append(
                    f"- {module}: {stat['papers']} / {stat['repos']} / "
                    f"{stat['cross']} / {stat['solutions']}"
                )
            weakest = sorted(
                counts.items(),
                key=lambda kv: (kv[1]["papers"] + kv[1]["cross"], kv[0]),
            )[:3]
            lines.append("")
            lines.append(
                "论文覆盖最少的模块（建议今晚优先）："
                + "、".join(f"{m}（{s['papers'] + s['cross']} 篇）" for m, s in weakest)
            )
        else:
            lines.append("- 尚无任何证据；先跑 A 阶段建立基线，再从检索计划中的模块开始。")

        recent = []
        for bucket_name, bucket in (("paper", papers), ("repo", repos), ("cross", cross)):
            for key, record in bucket.items():
                recent.append((record.get("saved_at", ""), bucket_name, key, record.get("title", "")))
        recent.sort(reverse=True)
        if recent:
            lines.append("")
            lines.append("## 最近保存")
            for saved_at, kind, key, title in recent[:10]:
                lines.append(f"- {saved_at[:16]} [{kind}] {key} — {title}")
        return "\n".join(lines)
