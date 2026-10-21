"""读取 GitHub 仓库 README 正文（免费、无需 API Key）。"""

import urllib.request
from urllib.error import HTTPError, URLError

from crewai.tools import BaseTool
from pydantic import BaseModel, Field

MAX_CHARS = 12000


class GithubReadmeInput(BaseModel):
    repo: str = Field(
        ...,
        description="仓库全名 owner/repo（也可直接给 https://github.com/owner/repo 链接）",
    )


class GithubReadmeTool(BaseTool):
    name: str = "github_readme"
    description: str = (
        "读取指定 GitHub 仓库的 README 正文（免费、无需 API Key）。"
        "输入 owner/repo，返回 README 文本（过长会截断）。用于了解仓库的真实做法。"
    )
    args_schema: type[BaseModel] = GithubReadmeInput

    def _run(self, repo: str) -> str:
        repo = (repo or "").strip().strip("/")
        for prefix in ("https://github.com/", "http://github.com/", "github.com/"):
            if repo.startswith(prefix):
                repo = repo[len(prefix):]
        repo = repo.removesuffix(".git").strip("/")
        if repo.count("/") != 1:
            return f"参数格式应为 owner/repo，收到: {repo}"

        req = urllib.request.Request(
            f"https://api.github.com/repos/{repo}/readme",
            headers={
                "User-Agent": "rm-research/0.1",
                "Accept": "application/vnd.github.raw",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                text = resp.read().decode("utf-8", "replace")
        except HTTPError as e:
            if e.code == 404:
                return f"未找到仓库 {repo} 的 README（404）。请确认仓库全名。"
            if e.code in {403, 429}:
                return "GitHub 接口限速已触发。请等待约 1 分钟后重试。"
            return f"读取失败（HTTP {e.code}）。"
        except URLError as e:
            return f"网络错误: {e.reason}"

        text = text.strip()
        if not text:
            return "(README 为空)"
        if len(text) > MAX_CHARS:
            text = text[:MAX_CHARS] + f"\n\n...(已截断，全文约 {len(text)} 字符)"
        return text
