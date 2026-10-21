"""arXiv 正文读取工具：抓取 ar5iv 正文，截取摘要/方法/实验关键段落，控制上下文长度。"""

import html as html_lib
import re
import urllib.error
import urllib.request
from html.parser import HTMLParser

from crewai.tools import BaseTool
from pydantic import BaseModel, Field

BLOCK_TAGS = {
    "p", "div", "section", "article", "h1", "h2", "h3", "h4", "h5", "h6",
    "li", "tr", "br", "figcaption", "table", "ul", "ol", "blockquote", "pre",
}
SKIP_TAGS = {"script", "style", "noscript", "svg", "nav", "footer"}


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        tag = tag.lower()
        if tag in SKIP_TAGS:
            self._skip_depth += 1
        elif tag in BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in SKIP_TAGS and self._skip_depth:
            self._skip_depth -= 1
        elif tag in BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip_depth and data:
            self.parts.append(data)


def _html_to_text(raw: str) -> str:
    parser = _TextExtractor()
    parser.feed(raw)
    text = html_lib.unescape("".join(parser.parts))
    lines = [re.sub(r"[ \t\u00a0]+", " ", line).strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line)


def _find(text: str, pattern: str, start: int = 0) -> int:
    match = re.search(pattern, text[start:], flags=re.IGNORECASE)
    return start + match.start() if match else -1


def _focused_excerpt(text: str, max_chars: int) -> str:
    intro_budget = max(1500, int(max_chars * 0.32))
    method_budget = max(2000, int(max_chars * 0.40))
    exp_budget = max(1500, max_chars - intro_budget - method_budget)

    intro = text[:intro_budget]
    method_at = _find(text, r"\b(?:method|methodology|approach|proposed\s+(?:method|framework|system))\b", 1200)
    method_start = max(0, method_at - 400) if method_at >= 0 else -1
    method = text[method_start:method_start + method_budget] if method_start >= 0 else ""

    search_from = (method_start + method_budget) if method_start >= 0 else 1200
    exp_at = _find(text, r"\b(?:experiments?|evaluation|results|ablation(?:\s+stud(?:y|ies))?)\b", search_from)
    if exp_at < 0:
        exp_at = _find(text, r"\b(?:experiments?|evaluation|results|ablation)\b", 1200)
    exp_start = max(0, exp_at - 300) if exp_at >= 0 else -1
    exp = text[exp_start:exp_start + exp_budget] if exp_start >= 0 else ""

    sections = [("开头（标题/摘要/引言）", intro)]
    if method:
        sections.append(("方法相关段落", method))
    if exp:
        sections.append(("实验/结果相关段落", exp))
    body = "\n\n".join(f"【{label}】\n{chunk}" for label, chunk in sections if chunk)
    if len(body) > max_chars:
        body = body[:max_chars]
    return body


class ArxivReadInput(BaseModel):
    arxiv_id: str = Field(..., description="arXiv ID，例如 2502.00749（不带版本号）")
    max_chars: int = Field(
        14000, description="返回正文的最大字符数，4000-30000，默认 14000"
    )


class ArxivReadTool(BaseTool):
    name: str = "arxiv_read"
    description: str = (
        "读取 arXiv 论文正文：优先抓取 ar5iv HTML，截取开头、方法与实验关键段落（限长），"
        "失败时回退到摘要页。返回文本会标注「仅摘要」或「已截取」。用于精读，不用于检索。"
    )
    args_schema: type[BaseModel] = ArxivReadInput

    def _run(self, arxiv_id: str, max_chars: int = 14000) -> str:
        aid = re.sub(r"v\d+$", "", (arxiv_id or "").strip())
        if not re.fullmatch(r"\d{4}\.\d{4,5}|[a-z\-]+/\d{7}", aid):
            return f"arxiv_id 格式不合法：{arxiv_id}。请传不带版本号的 arXiv ID。"

        limit = max(4000, min(int(max_chars or 14000), 30000))
        headers = {"User-Agent": "rm-research/0.1"}

        try:
            req = urllib.request.Request(
                f"https://ar5iv.labs.arxiv.org/html/{aid}", headers=headers
            )
            with urllib.request.urlopen(req, timeout=40) as resp:
                raw = resp.read().decode("utf-8", "replace")
            text = _html_to_text(raw)
            if len(text) < 1500:
                raise ValueError("正文过短")
            excerpt = _focused_excerpt(text, limit)
            return (
                f"arxiv_id: {aid}\n来源: ar5iv 正文（已截取关键段落，原文约 {len(text)} 字符）\n\n"
                f"{excerpt}\n\n[提示] 需要更多细节时可调大 max_chars 重新读取（上限 30000）。"
            )
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ValueError):
            pass

        try:
            req = urllib.request.Request(
                f"https://arxiv.org/abs/{aid}", headers=headers
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read().decode("utf-8", "replace")
            text = _html_to_text(raw)
            return (
                f"arxiv_id: {aid}\n来源: arXiv 摘要页（仅摘要，未读到正文）\n\n"
                f"{text[:limit]}\n\n[提示] 笔记中必须标注「仅摘要，未读正文」。"
            )
        except Exception as e:  # noqa: BLE001
            return f"读取失败：{e}。请尝试稍后重试，或在笔记中标注「仅摘要，未读正文」。"
