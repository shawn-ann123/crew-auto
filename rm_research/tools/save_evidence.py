"""保存一条证据笔记，并维护 library/processed.json 与 INDEX.md。"""

import re
from datetime import datetime
from pathlib import Path
from typing import Literal

from crewai.tools import BaseTool
from pydantic import BaseModel, Field

from tools.state_io import atomic_write_json, atomic_write_text, load_json_strict

KIND_BUCKETS = {"paper": "papers", "repo": "repos", "cross": "cross"}
KIND_DIRS = {"paper": "papers", "repo": "repos", "cross": "cross"}


def _normalize_arxiv_id(raw: str) -> str:
    value = (raw or "").strip()
    if "/abs/" in value:
        value = value.split("/abs/", 1)[1]
    value = value.strip("/")
    return re.sub(r"v\d+$", "", value)


def _safe_name(value: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9._-]+", "__", value).strip("_")
    return safe or "item"


class SaveEvidenceInput(BaseModel):
    kind: Literal["paper", "repo", "cross"] = Field(
        ..., description="证据类型：paper=论文，repo=GitHub 仓库，cross=跨域文献"
    )
    item_id: str = Field(
        ..., description="唯一标识：论文用 arXiv ID（如 2401.12345），仓库用 owner/repo"
    )
    title: str = Field(..., description="标题或仓库全名")
    modules: str = Field(
        "", description='归属模块，逗号分隔，例如 "M2,M5"；不确定可留空'
    )
    summary: str = Field("", description="一句话摘要，不超过 120 字，写入索引")
    verification: Literal[
        "auto", "full_text", "abstract_only", "code_checked", "readme_only", "page_only"
    ] = Field(
        "auto",
        description=(
            "阅读核验程度：full_text=已读正文关键段，abstract_only=仅摘要，"
            "code_checked=已核对具体代码，readme_only=仅 README，page_only=仅页面介绍"
        ),
    )
    content: str = Field(
        ..., description="完整 Markdown 笔记正文，必须包含问题、方法卡、证据与局限"
    )


class SaveEvidenceTool(BaseTool):
    name: str = "save_evidence"
    description: str = (
        "把一篇论文/一个仓库/一篇跨域文献的完整笔记落盘到 library/ 对应目录，"
        "自动维护已处理账本 library/processed.json 和索引 library/INDEX.md。"
        "同一 ID 重复保存会覆盖笔记。笔记必须包含可独立实现的方法卡。"
    )
    args_schema: type[BaseModel] = SaveEvidenceInput

    def _run(
        self,
        kind: str,
        item_id: str,
        title: str,
        modules: str = "",
        summary: str = "",
        verification: str = "auto",
        content: str = "",
    ) -> str:
        if kind not in KIND_DIRS:
            return "保存失败：kind 只能是 paper、repo 或 cross。"

        root = Path.cwd()
        library = root / "library"
        library.mkdir(parents=True, exist_ok=True)

        key = _normalize_arxiv_id(item_id) if kind in {"paper", "cross"} else item_id.strip()
        if not key:
            return "保存失败：item_id 不能为空。"
        note_path = library / KIND_DIRS[kind] / f"{_safe_name(key)}.md"
        note_path.parent.mkdir(parents=True, exist_ok=True)

        body = content.strip()
        if not body:
            return "保存失败：content 不能为空。"
        if not body.startswith("#"):
            body = f"# {title.strip()}\n\n{body}"
        if verification == "auto":
            if kind == "repo":
                verification = "readme_only"
            elif "仅摘要" in body or "未读正文" in body:
                verification = "abstract_only"
            else:
                verification = "full_text"
        module_list = [
            item.upper()
            for item in re.split(r"[,，;；\s]+", modules or "")
            if re.fullmatch(r"[Mm]\d+", item.strip())
        ]
        now = datetime.now()
        processed_path = library / "processed.json"
        data = load_json_strict(processed_path, {})
        if not isinstance(data, dict):
            return "保存失败：library/processed.json 根结构必须是对象，原文件未覆盖。"
        for bucket in KIND_BUCKETS.values():
            data.setdefault(bucket, {})

        bucket = data.setdefault(KIND_BUCKETS[kind], {})
        if key in bucket:
            return (
                f"已处理 {kind} {key}：{bucket[key].get('path', '')}；"
                "为保留原始证据未覆盖旧笔记。"
            )

        # 论文与跨域文献共用同一作品身份，避免换个分类重复消化。
        if kind in {"paper", "cross"}:
            other_bucket = data["cross" if kind == "paper" else "papers"]
            if key in other_bucket:
                return (
                    f"已处理同一文献 {key}：{other_bucket[key].get('path', '')}；"
                    "未按新分类重复保存。"
                )

        atomic_write_text(note_path, body + "\n")
        bucket[key] = {
            "title": title.strip(),
            "modules": module_list,
            "summary": summary.strip(),
            "verification": verification,
            "path": str(note_path.relative_to(root)),
            "date": now.strftime("%Y-%m-%d"),
            "saved_at": now.isoformat(timespec="seconds"),
        }
        atomic_write_json(processed_path, data)

        index_path = library / "INDEX.md"
        if index_path.exists():
            index_text = index_path.read_text(encoding="utf-8").rstrip()
        else:
            index_text = "# 证据索引\n\n按保存顺序追加；详细笔记见 library/ 子目录。"
        marker = f"[{kind}] {key} —"
        if marker not in index_text:
            line = f"- {now:%Y-%m-%d} [{kind}] {key} — {title.strip()} — {summary.strip()}"
            atomic_write_text(index_path, index_text + "\n" + line + "\n")

        total = sum(len(data.get(b, {})) for b in KIND_BUCKETS.values())
        return (
            f"已保存 {kind} {key} -> {note_path.relative_to(root)}；"
            f"模块 {','.join(module_list) if module_list else '未标注'}；证据总数 {total}"
        )
