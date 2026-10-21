"""将论文候选按范围概念做可重复的初排，供文献检索角色缩小精读范围。"""

import json
import re
from typing import Any

from crewai.tools import BaseTool
from pydantic import BaseModel, Field


def _groups(value: Any) -> list[list[str]]:
    """把字符串或同义词数组统一为分组形式。"""
    result: list[list[str]] = []
    for item in value or []:
        values = item if isinstance(item, list) else [item]
        group = [str(term).strip().lower() for term in values if str(term).strip()]
        if group:
            result.append(group)
    return result


def _contains(text: str, group: list[str]) -> bool:
    return any(term in text for term in group)


def rerank(payload: dict[str, Any], top: int = 10) -> dict[str, Any]:
    candidates = payload.get("candidates")
    if not isinstance(candidates, list):
        raise ValueError("candidates 必须是数组")

    must = _groups(payload.get("must_have"))
    should = _groups(payload.get("should_have"))
    excluded = _groups(payload.get("exclude"))
    query_terms = re.findall(r"[a-z0-9][a-z0-9_-]+", str(payload.get("query", "")).lower())
    ranked: list[dict[str, Any]] = []

    for index, raw in enumerate(candidates):
        item = raw if isinstance(raw, dict) else {"title": str(raw)}
        title = str(item.get("title", ""))
        abstract = str(item.get("abstract", ""))
        text = f"{title} {abstract}".lower()
        title_text = title.lower()
        missing = [group for group in must if not _contains(text, group)]
        must_hits = len(must) - len(missing)
        should_hits = sum(_contains(text, group) for group in should)
        exclude_hits = sum(_contains(text, group) for group in excluded)
        query_hits = sum(term in text for term in query_terms)
        title_hits = sum(_contains(title_text, group) for group in must + should)
        score = must_hits * 20 + should_hits * 6 + title_hits * 3 + query_hits - exclude_hits * 40

        if exclude_hits:
            category = "exclude"
        elif not missing:
            category = "core"
        elif len(missing) == 1 and should_hits:
            category = "adjacent-core"
        else:
            category = "boundary"

        ranked.append(
            {
                **item,
                "classification": category,
                "rerank_score": score,
                "matched_must_groups": must_hits,
                "missing_must_groups": missing,
                "matched_should_groups": should_hits,
                "exclude_hits": exclude_hits,
                "original_order": index + 1,
            }
        )

    order = {"core": 0, "adjacent-core": 1, "boundary": 2, "exclude": 3}
    ranked.sort(key=lambda item: (order[item["classification"]], -item["rerank_score"], item["original_order"]))
    limit = max(1, min(int(top or 10), 50))
    return {"returned": min(limit, len(ranked)), "total": len(ranked), "items": ranked[:limit]}


class LiteratureRerankInput(BaseModel):
    payload_json: str = Field(
        ...,
        description=(
            "JSON 字符串，包含 candidates，must_have/should_have/exclude（每项可为字符串"
            "或同义词数组），可选 query。"
        ),
    )
    top: int = Field(10, description="返回前 N 篇，范围 1-50")


class LiteratureRerankTool(BaseTool):
    name: str = "literature_rerank"
    description: str = (
        "在论文候选较多或边界不清时，按必须概念、加分概念和排除概念"
        "做稳定的初排，并标记 core / adjacent-core / boundary / exclude。"
    )
    args_schema: type[BaseModel] = LiteratureRerankInput

    def _run(self, payload_json: str, top: int = 10) -> str:
        try:
            payload = json.loads(payload_json)
            if not isinstance(payload, dict):
                raise ValueError("顶层必须是 JSON 对象")
            return json.dumps(rerank(payload, top), ensure_ascii=False, indent=2)
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            return f"候选重排失败：{exc}"
