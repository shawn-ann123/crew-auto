"""方案库维护工具：为一篇文献中的多条方法分配稳定编号、去重合并并重生成方案库。"""

import re
from datetime import datetime
from pathlib import Path

from crewai.tools import BaseTool
from pydantic import BaseModel, Field

from tools.state_io import atomic_write_json, atomic_write_text, load_json_strict

EVIDENCE_RANK = {"A": 3, "B": 2, "C": 1}


class SolutionItem(BaseModel):
    module: str = Field("", description="归属模块号，例如 M2；合并已有方案时留空")
    name: str = Field("", description="方法名，简短、可辨识；合并已有方案时留空")
    core: str = Field("", description="核心做法，2-4 句，达到可复现粒度；合并时留空")
    source_type: str = Field("", description="来源类型：paper、repo 或 cross")
    source_ref: str = Field("", description="来源标识：arXiv ID 或 owner/repo")
    evidence: str = Field(
        "B",
        description="证据等级：A=可运行代码或完整实验且已读正文/核查代码，B=论文有实验且已读正文，C=仅摘要/仅描述/仅 README",
    )
    note: str = Field("", description="补充说明：工程代价、风险、适用条件")
    existing_id: str = Field("", description="若并入已有方案，填已有编号例如 M2-S5")


class AddSolutionsInput(BaseModel):
    solutions: list[SolutionItem] = Field(
        ..., description="从一个或多个来源提取的方法，逐条传入；同一篇文献可以有多条"
    )


def _normalize_name(name: str) -> str:
    return re.sub(r"[\s\-_：:（）()，,。.]+", "", name.lower())


def _cell(value: str) -> str:
    return (value or "").replace("|", "\\|").replace("\n", " ").strip()


def _load(path: Path) -> list[dict]:
    data = load_json_strict(path, [])
    if isinstance(data, dict):
        data = data.get("solutions", [])
    if not isinstance(data, list):
        raise ValueError(f"{path} 根结构必须是列表或包含 solutions 列表的对象")
    return data


def _source_record(root: Path, source_type: str, source_ref: str) -> dict | None:
    processed = load_json_strict(root / "library" / "processed.json", {})
    if not isinstance(processed, dict):
        return None
    bucket_name = {"paper": "papers", "repo": "repos", "cross": "cross"}.get(
        source_type
    )
    if not bucket_name:
        return None
    bucket = processed.get(bucket_name) or {}
    if source_ref in bucket:
        return bucket[source_ref]
    if source_type == "repo":
        lowered = source_ref.lower()
        for key, record in bucket.items():
            if key.lower() == lowered:
                return record
    return None


def _evidence_ceiling(record: dict) -> str:
    verification = record.get("verification", "")
    if verification == "code_checked":
        return "A"
    if verification == "full_text":
        return "B"
    return "C"


def _known_modules(root: Path) -> set[str]:
    path = root / "baseline" / "模块基线.md"
    if not path.exists():
        return set()
    return set(re.findall(r"^##\s+(M\d+)\b", path.read_text(encoding="utf-8"), re.M))


def _next_index(solutions: list[dict], module: str) -> int:
    used = 0
    for item in solutions:
        if item.get("module") != module:
            continue
        match = re.fullmatch(rf"{re.escape(module)}-S(\d+)", item.get("id", ""))
        if match:
            used = max(used, int(match.group(1)))
    return used + 1


def _merge_sources(target: dict, source: str, note: str, evidence: str) -> None:
    if not source:
        return
    sources = target.setdefault("sources", [])
    if source not in sources:
        sources.append(source)
    if note:
        existing_note = target.get("note", "")
        if note not in existing_note:
            target["note"] = f"{existing_note}；{note}".strip("；")
    if EVIDENCE_RANK.get(evidence, 0) > EVIDENCE_RANK.get(target.get("evidence", "B"), 0):
        target["evidence"] = evidence


def _render(solutions: list[dict], updated_at: str) -> str:
    lines = [
        "# 方案库（自动生成，勿手改）",
        "",
        f"> 更新时间：{updated_at} ｜ 总方案数：{len(solutions)} ｜ 数据源：library/solutions.json",
        "> 编号规则：M<模块号>-S<序号>。证据等级：A=可运行代码或完整实验且已读正文；B=论文有实验且已读正文；C=仅摘要/仅描述/仅 README。",
        "",
    ]
    if not solutions:
        lines.append("_暂无方案。C 阶段会从每晚的证据笔记中提取方法卡并分配编号。_")
        return "\n".join(lines) + "\n"

    modules = sorted(
        {item.get("module", "M?") for item in solutions},
        key=lambda m: (int(m[1:]) if m[1:].isdigit() else 999, m),
    )
    for module in modules:
        lines.append(f"## {module}")
        lines.append("")
        lines.append("| 编号 | 方案 | 核心做法 | 来源 | 证据 | 备注 |")
        lines.append("|---|---|---|---|---|---|")
        items = [item for item in solutions if item.get("module") == module]
        items.sort(key=lambda item: item.get("id", ""))
        for item in items:
            sources = "；".join(item.get("sources") or [item.get("source_ref", "")])
            lines.append(
                f"| {_cell(item.get('id'))} | {_cell(item.get('name'))} | "
                f"{_cell(item.get('core'))} | {_cell(sources)} | "
                f"{_cell(item.get('evidence'))} | {_cell(item.get('note'))} |"
            )
        lines.append("")
    return "\n".join(lines) + "\n"


class AddSolutionsTool(BaseTool):
    name: str = "add_solutions"
    description: str = (
        "把从文献/仓库中提取的方法写入方案库，自动分配稳定编号（M2-S3）、按名称去重、"
        "把重复来源合并到已有方案，并重生成 proposals/方案库.md。"
        "同一篇论文提取出多条可独立实现的方法时，应分别提交多条。"
        "仅摘要、仅描述或仅 README 的方法，证据等级最多为 C。"
    )
    args_schema: type[BaseModel] = AddSolutionsInput

    def _run(self, solutions: list[SolutionItem]) -> str:
        root = Path.cwd()
        library = root / "library"
        proposals = root / "proposals"
        library.mkdir(parents=True, exist_ok=True)
        proposals.mkdir(parents=True, exist_ok=True)

        store_path = library / "solutions.json"
        store = _load(store_path)
        by_id = {item.get("id"): item for item in store}
        by_name = {
            (item.get("module"), _normalize_name(item.get("name", ""))): item
            for item in store
        }
        now = datetime.now()
        assigned: list[str] = []
        merged: list[str] = []
        errors: list[str] = []
        known_modules = _known_modules(root)

        for item in solutions:
            evidence = item.evidence.upper()
            if evidence not in EVIDENCE_RANK:
                evidence = "B"
            source = (
                f"{item.source_type}:{item.source_ref}"
                if item.source_type and item.source_ref
                else item.source_ref
            )
            source_record = _source_record(root, item.source_type, item.source_ref)
            if source_record is None:
                errors.append(
                    f"{item.name or item.existing_id or '(未命名)'}：来源 {source or '(空)'} 未在证据账本登记"
                )
                continue
            ceiling = _evidence_ceiling(source_record)
            if EVIDENCE_RANK[evidence] > EVIDENCE_RANK[ceiling]:
                evidence = ceiling

            if item.existing_id:
                target = by_id.get(item.existing_id)
                if target is None:
                    errors.append(f"{item.existing_id} 不存在，未合并")
                    continue
                _merge_sources(target, source, item.note, evidence)
                target["updated"] = now.strftime("%Y-%m-%d")
                merged.append(target["id"])
                continue

            module = (item.module or "").strip().upper()
            if not re.fullmatch(r"M\d+", module):
                errors.append(f"{item.name or '(未命名)'}：module 不合法（应为 M2 这类编号）")
                continue
            if known_modules and module not in known_modules:
                errors.append(f"{item.name or '(未命名)'}：{module} 不在已冻结的模块基线中")
                continue
            if not item.name.strip() or not item.core.strip():
                errors.append(f"{module}：缺少方法名或核心做法，未写入")
                continue
            if not source:
                errors.append(f"{module}-{item.name}：缺少来源，未写入")
                continue

            key = (module, _normalize_name(item.name))
            duplicate = by_name.get(key)
            if duplicate is not None:
                _merge_sources(duplicate, source, item.note, evidence)
                duplicate["updated"] = now.strftime("%Y-%m-%d")
                merged.append(duplicate["id"])
                continue

            new_id = f"{module}-S{_next_index(store, module)}"
            record = {
                "id": new_id,
                "module": module,
                "name": item.name.strip(),
                "core": item.core.strip(),
                "sources": [source],
                "source_ref": item.source_ref,
                "source_type": item.source_type,
                "evidence": evidence,
                "note": item.note.strip(),
                "added": now.strftime("%Y-%m-%d"),
                "updated": now.strftime("%Y-%m-%d"),
            }
            store.append(record)
            by_id[new_id] = record
            by_name[key] = record
            assigned.append(new_id)

        atomic_write_json(store_path, {"solutions": store})
        atomic_write_text(
            proposals / "方案库.md", _render(store, now.strftime("%Y-%m-%d %H:%M"))
        )

        parts = [
            f"新增 {len(assigned)} 条" + (f"：{('、'.join(assigned))}" if assigned else ""),
            f"合并 {len(merged)} 条" + (f"：{('、'.join(merged))}" if merged else ""),
        ]
        if errors:
            parts.append(f"跳过 {len(errors)} 条：" + "；".join(errors))
        parts.append(f"当前方案总数 {len(store)}；已重写 proposals/方案库.md")
        return "\n".join(parts)
