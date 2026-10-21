"""多库论文检索：OpenAlex（免 Key）+ Semantic Scholar（可选 Key）。"""

import json
import os
import re
import urllib.parse
import urllib.request
from pathlib import Path

from crewai.tools import BaseTool
from pydantic import BaseModel, Field

OPENALEX_API = "https://api.openalex.org/works"
S2_API = "https://api.semanticscholar.org/graph/v1/paper/search"
S2_FIELDS = (
    "title,abstract,year,venue,authors,externalIds,citationCount,"
    "openAccessPdf,publicationTypes"
)


def _normalize_doi(raw: str) -> str:
    value = (raw or "").strip().lower()
    for prefix in ("https://doi.org/", "http://doi.org/", "doi:"):
        if value.startswith(prefix):
            value = value[len(prefix):]
    return value.strip()


def _normalize_arxiv(raw: str) -> str:
    value = (raw or "").strip()
    if "/abs/" in value:
        value = value.split("/abs/", 1)[1]
    value = value.strip("/")
    return re.sub(r"v\d+$", "", value)


def _normalize_title(raw: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (raw or "").lower()).strip()


def _arxiv_from_doi(doi: str) -> str:
    match = re.fullmatch(r"10\.48550/arxiv\.(.+)", doi or "", flags=re.I)
    return _normalize_arxiv(match.group(1)) if match else ""


def _processed_items() -> tuple[set[str], set[str]]:
    path = Path.cwd() / "library" / "processed.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return set(), set()
    ids: set[str] = set()
    titles: set[str] = set()
    for bucket_name in ("papers", "cross"):
        for key, record in (data.get(bucket_name) or {}).items():
            normalized = _normalize_doi(_normalize_arxiv(key))
            if normalized:
                ids.add(normalized)
            title = _normalize_title((record or {}).get("title", ""))
            if title:
                titles.add(title)
    return ids, titles


def _get_json(url: str, headers: dict | None = None):
    req = urllib.request.Request(
        url, headers={"User-Agent": "rm-research/0.1", **(headers or {})}
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def _abstract_from_inverted(inverted) -> str:
    if not isinstance(inverted, dict):
        return ""
    pairs = []
    for word, positions in inverted.items():
        if isinstance(positions, list):
            for pos in positions:
                if isinstance(pos, int):
                    pairs.append((pos, word))
    pairs.sort()
    text = " ".join(word for _, word in pairs)
    return text[:480] + "..." if len(text) > 480 else text


def _s2_search(query: str, limit: int, year_from: int) -> list[dict]:
    key = os.getenv("SEMANTIC_SCHOLAR_API_KEY", "").strip()
    if not key:
        raise RuntimeError("未配置 SEMANTIC_SCHOLAR_API_KEY")
    params = {"query": query, "limit": limit, "fields": S2_FIELDS}
    if year_from:
        params["year"] = f"{year_from}-"
    data = _get_json(
        f"{S2_API}?{urllib.parse.urlencode(params)}", {"x-api-key": key}
    )
    items = []
    for paper in data.get("data") or []:
        ext = paper.get("externalIds") or {}
        arxiv_id = _normalize_arxiv(str(ext.get("ArXiv") or ""))
        doi = _normalize_doi(str(ext.get("DOI") or ""))
        title = (paper.get("title") or "").strip()
        paper_id = paper.get("paperId") or ""
        if arxiv_id:
            item_id, id_kind = arxiv_id, "arXiv"
            link = f"https://arxiv.org/abs/{arxiv_id}"
        elif doi:
            item_id, id_kind = doi, "DOI"
            link = f"https://doi.org/{doi}"
        else:
            item_id, id_kind = f"s2:{paper_id}", "S2"
            link = f"https://www.semanticscholar.org/paper/{paper_id}"
        items.append(
            {
                "source": "Semantic Scholar",
                "item_id": item_id,
                "id_kind": id_kind,
                "title": title,
                "year": paper.get("year") or "",
                "venue": paper.get("venue") or "",
                "authors": [
                    a.get("name", "") for a in (paper.get("authors") or [])
                ][:4],
                "citations": paper.get("citationCount") or 0,
                "link": link,
                "pdf": (paper.get("openAccessPdf") or {}).get("url") or "",
                "abstract": (paper.get("abstract") or "").strip(),
                "key": arxiv_id or doi or _normalize_title(title),
            }
        )
    return items


def _openalex_search(query: str, limit: int, year_from: int) -> list[dict]:
    params = {"search": query, "per-page": limit}
    mailto = os.getenv("OPENALEX_MAILTO", "").strip()
    if mailto:
        params["mailto"] = mailto
    if year_from:
        params["filter"] = f"from_publication_date:{year_from}-01-01"
    data = _get_json(f"{OPENALEX_API}?{urllib.parse.urlencode(params)}")
    items = []
    for work in data.get("results") or []:
        doi = _normalize_doi(str(work.get("doi") or ""))
        arxiv_id = _arxiv_from_doi(doi)
        title = (work.get("title") or work.get("display_name") or "").strip()
        source = (work.get("primary_location") or {}).get("source") or {}
        work_id = (work.get("id") or "").rsplit("/", 1)[-1]
        if arxiv_id:
            item_id, id_kind = arxiv_id, "arXiv"
            link = f"https://arxiv.org/abs/{arxiv_id}"
        elif doi:
            item_id, id_kind = doi, "DOI"
            link = f"https://doi.org/{doi}"
        else:
            item_id, id_kind = f"openalex:{work_id}", "OpenAlex"
            link = work.get("id") or ""
        items.append(
            {
                "source": "OpenAlex",
                "item_id": item_id,
                "id_kind": id_kind,
                "title": title,
                "year": work.get("publication_year") or "",
                "venue": source.get("display_name") or "",
                "authors": [
                    (a.get("author") or {}).get("display_name") or ""
                    for a in (work.get("authorships") or [])
                ][:4],
                "citations": work.get("cited_by_count") or 0,
                "link": link,
                "pdf": (work.get("open_access") or {}).get("oa_url")
                or (work.get("primary_location") or {}).get("pdf_url")
                or "",
                "abstract": _abstract_from_inverted(
                    work.get("abstract_inverted_index")
                ),
                "key": arxiv_id or doi or _normalize_title(title),
            }
        )
    return items


class ScholarSearchInput(BaseModel):
    query: str = Field(
        ...,
        description="英文检索词或关键词组合，例如 armor plate detection robot",
    )
    max_results: int = Field(6, description="返回条数上限，1-10，默认 6")
    year_from: int = Field(0, description="可选：只检索该年份及以后，0 表示不限")


class ScholarSearchTool(BaseTool):
    name: str = "scholar_search"
    description: str = (
        "多库论文检索：OpenAlex（免费、无需 Key）+ Semantic Scholar（在 .env 配置 "
        "SEMANTIC_SCHOLAR_API_KEY 后启用）。自动排除 library/processed.json 中已处理的论文，"
        "返回标题、DOI/arXiv ID、年份、venue、引用数、OA PDF 与摘要。"
        "注意：非 arXiv 且无 OA PDF 的条目只能读摘要，精读时必须标注「仅摘要」。"
    )
    args_schema: type[BaseModel] = ScholarSearchInput

    def _run(self, query: str, max_results: int = 6, year_from: int = 0) -> str:
        count = max(1, min(int(max_results or 6), 10))
        limit = max(count, 4)
        year = max(0, int(year_from or 0))
        errors: list[str] = []
        s2_items: list[dict] = []
        oa_items: list[dict] = []

        if os.getenv("SEMANTIC_SCHOLAR_API_KEY", "").strip():
            try:
                s2_items = _s2_search(query, limit, year)
            except Exception as e:  # noqa: BLE001
                errors.append(f"Semantic Scholar 失败：{e}")
        else:
            errors.append("未配置 SEMANTIC_SCHOLAR_API_KEY，本次只查 OpenAlex")

        try:
            oa_items = _openalex_search(query, limit, year)
        except Exception as e:  # noqa: BLE001
            errors.append(f"OpenAlex 失败：{e}")

        processed_ids, processed_titles = _processed_items()
        seen_keys: set[str] = set()
        seen_titles: set[str] = set()
        fresh: list[dict] = []
        excluded = 0
        s2_count = 0
        oa_count = 0
        for item in s2_items + oa_items:
            title_key = _normalize_title(item["title"])
            item_key = item["key"] or title_key
            if (
                item_key in processed_ids
                or title_key in processed_titles
                or _normalize_doi(item["item_id"]) in processed_ids
            ):
                excluded += 1
                continue
            if item_key in seen_keys or (title_key and title_key in seen_titles):
                continue
            seen_keys.add(item_key)
            if title_key:
                seen_titles.add(title_key)
            fresh.append(item)
            if item["source"] == "Semantic Scholar":
                s2_count += 1
            else:
                oa_count += 1
            if len(fresh) >= count:
                break

        header = (
            f"新增 {len(fresh)} 条（Semantic Scholar {s2_count} 条 + "
            f"OpenAlex {oa_count} 条；自动排除已处理 {excluded} 条）。"
        )
        lines = [header]
        if errors:
            lines.append("检索提示：" + "；".join(errors))
        if not fresh:
            lines.append(
                "未检索到未处理的新论文。请换更通用的英文检索词，或换一个模块。"
            )
            return "\n".join(lines)
        lines.append(
            "注意：非 arXiv 且无 OA PDF 的条目只能读摘要；精读时必须标注「仅摘要」，"
            "不要编造实验数字。"
        )
        for index, item in enumerate(fresh, 1):
            lines.append(f"[{index}] {item['title']}")
            lines.append(f"推荐 item_id: {item['item_id']}（{item['id_kind']}）")
            lines.append(f"来源: {item['source']}")
            lines.append(
                f"年份 / venue: {item['year'] or '未知'} / {item['venue'] or '未知'}"
            )
            lines.append(f"作者: {', '.join(item['authors']) or '未知'}")
            lines.append(f"引用: {item['citations']}")
            lines.append(f"链接: {item['link']}")
            if item["pdf"]:
                lines.append(f"OA PDF: {item['pdf']}")
            lines.append(f"摘要: {item['abstract'] or '（无摘要）'}")
            lines.append("")
        return "\n".join(lines)
