"""GitHub 增量检索工具：自动排除 library/processed.json 中已消化的仓库。"""

import json
import urllib.parse
import urllib.request
from pathlib import Path
from urllib.error import HTTPError, URLError

from crewai.tools import BaseTool
from pydantic import BaseModel, Field

GITHUB_API = "https://api.github.com"


def _processed_repos() -> set[str]:
    path = Path.cwd() / "library" / "processed.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return set()
    return {name.lower() for name in (data.get("repos") or {})}


class GithubSearchNewInput(BaseModel):
    query: str = Field(
        ...,
        description="GitHub 搜索关键词（英文、简短、不加引号），例如 rune detection",
    )
    max_results: int = Field(6, description="本次返回条数上限，1-10，默认 6")
    sort: str = Field("stars", description="排序方式：stars（默认）或 updated")


class GithubSearchNewTool(BaseTool):
    name: str = "github_search_new"
    description: str = (
        "在 GitHub 上做增量检索（免费、无需 API Key）：自动排除 library/processed.json "
        "中已消化的仓库，只返回新仓库。未认证接口限速约 10 次/分钟，请勿短时间大量调用。"
    )
    args_schema: type[BaseModel] = GithubSearchNewInput

    def _run(self, query: str, max_results: int = 6, sort: str = "stars") -> str:
        count = max(1, min(int(max_results or 6), 10))
        sort_key = sort if sort in {"stars", "updated"} else "stars"
        params = urllib.parse.urlencode(
            {"q": query, "sort": sort_key, "order": "desc", "per_page": count * 2}
        )
        req = urllib.request.Request(
            f"{GITHUB_API}/search/repositories?{params}",
            headers={
                "User-Agent": "rm-research/0.1",
                "Accept": "application/vnd.github+json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode("utf-8", "replace"))
        except HTTPError as e:
            if e.code in {403, 429}:
                return "GitHub 未认证接口限速已触发（约 10 次/分钟）。请等待约 1 分钟后继续，或减少连续调用。"
            return f"GitHub 检索失败（HTTP {e.code}）。"
        except URLError as e:
            return f"网络错误: {e.reason}"

        processed = _processed_repos()
        fresh = []
        excluded = 0
        seen: set[str] = set()
        for item in data.get("items", []):
            full_name = item.get("full_name") or ""
            key = full_name.lower()
            if not key or key in seen:
                continue
            seen.add(key)
            if key in processed:
                excluded += 1
                continue
            fresh.append(
                f"[{len(fresh) + 1}] {full_name}  ⭐{item.get('stargazers_count')}  "
                f"{item.get('language') or '未知语言'}\n"
                f"更新: {(item.get('pushed_at') or '')[:10]}  "
                f"链接: {item.get('html_url')}\n"
                f"描述: {item.get('description') or '(无描述)'}\n"
            )
            if len(fresh) >= count:
                break

        header = f"新增 {len(fresh)} 条（自动排除已处理 {excluded} 条）。"
        if not fresh:
            return header + "\n未检索到未处理的新仓库。请换关键词或摘要式检索。"
        return header + "\n\n" + "\n".join(fresh)
