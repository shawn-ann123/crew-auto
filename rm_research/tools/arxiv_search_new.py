"""arXiv 增量检索工具：自动排除 library/processed.json 中已消化的论文。"""

import json
import re
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

from crewai.tools import BaseTool
from pydantic import BaseModel, Field

ARXIV_API = "https://export.arxiv.org/api/query"
ATOM_NS = {"a": "http://www.w3.org/2005/Atom"}


def _normalize_arxiv_id(raw: str) -> str:
    value = (raw or "").strip()
    if "/abs/" in value:
        value = value.split("/abs/", 1)[1]
    value = value.strip("/")
    return re.sub(r"v\d+$", "", value)


def _processed_paper_ids() -> set[str]:
    path = Path.cwd() / "library" / "processed.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return set()
    return set((data.get("papers") or {}).keys()) | set(
        (data.get("cross") or {}).keys()
    )


class ArxivSearchNewInput(BaseModel):
    query: str = Field(
        ...,
        description='arXiv 检索式，支持其语法，例如 all:"spinning target" AND all:prediction',
    )
    max_results: int = Field(6, description="本次返回条数上限，1-10，默认 6")


class ArxivSearchNewTool(BaseTool):
    name: str = "arxiv_search_new"
    description: str = (
        "在 arXiv 上做增量检索（免费、无需 API Key）：自动排除 library/processed.json "
        "中已经消化过的论文，只返回新论文。输入英文检索式与条数，"
        "返回 arXiv ID、标题、作者、日期、摘要与链接。"
    )
    args_schema: type[BaseModel] = ArxivSearchNewInput

    def _run(self, query: str, max_results: int = 6) -> str:
        count = max(1, min(int(max_results or 6), 10))
        params = urllib.parse.urlencode(
            {
                "search_query": query,
                "start": 0,
                "max_results": count,
                "sortBy": "relevance",
            }
        )
        req = urllib.request.Request(
            f"{ARXIV_API}?{params}", headers={"User-Agent": "rm-research/0.1"}
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                xml_text = resp.read().decode("utf-8", "replace")
        except Exception as e:  # noqa: BLE001
            return f"arXiv 检索失败（网络或服务问题）: {e}。可稍后重试或改用更简单的检索式。"

        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError as e:
            return f"arXiv 返回内容无法解析: {e}"

        processed = _processed_paper_ids()
        seen: set[str] = set()
        fresh = []
        excluded = 0
        for entry in root.findall("a:entry", ATOM_NS):
            raw_id = entry.findtext("a:id", "", ATOM_NS) or ""
            arxiv_id = _normalize_arxiv_id(raw_id)
            if not arxiv_id or arxiv_id in seen:
                continue
            seen.add(arxiv_id)
            if arxiv_id in processed:
                excluded += 1
                continue
            title = " ".join((entry.findtext("a:title", "", ATOM_NS) or "").split())
            date = (entry.findtext("a:published", "", ATOM_NS) or "")[:10]
            authors = [
                a.findtext("a:name", "", ATOM_NS) or ""
                for a in entry.findall("a:author", ATOM_NS)
            ][:4]
            summary = " ".join((entry.findtext("a:summary", "", ATOM_NS) or "").split())
            if len(summary) > 480:
                summary = summary[:480] + "..."
            fresh.append(
                f"[{len(fresh) + 1}] {title}\n"
                f"arxiv_id: {arxiv_id}\n"
                f"作者: {', '.join(authors)}\n"
                f"日期: {date}\n"
                f"链接: https://arxiv.org/abs/{arxiv_id}\n"
                f"摘要: {summary}\n"
            )

        header = f"新增 {len(fresh)} 条（自动排除已处理 {excluded} 条）。"
        if not fresh:
            return header + "\n未检索到未处理的新论文。请换更通用的英文检索式，或换一个模块。"
        return header + "\n\n" + "\n".join(fresh)
