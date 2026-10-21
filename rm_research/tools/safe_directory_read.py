"""只列出研究流程白名单目录。"""

from pathlib import Path

from crewai.tools import BaseTool
from pydantic import BaseModel, Field

from tools.safe_file_read import ALLOWED_ROOTS


class SafeDirectoryReadInput(BaseModel):
    directory: str = Field(..., description="项目内相对目录")


class SafeDirectoryReadTool(BaseTool):
    name: str = "DirectoryReadTool"
    description: str = "列出项目内的资料、证据或产物目录，不访问配置与密钥。"
    args_schema: type[BaseModel] = SafeDirectoryReadInput

    def _run(self, directory: str) -> str:
        root = Path.cwd().resolve()
        target = (root / directory).resolve()
        try:
            relative = target.relative_to(root)
        except ValueError:
            return "列目录失败：路径不在项目内。"
        if not relative.parts or relative.parts[0] not in ALLOWED_ROOTS:
            return "列目录失败：该路径不在研究资料白名单内。"
        if not target.is_dir():
            return f"列目录失败：目录不存在：{relative}"
        entries = sorted(p.relative_to(root) for p in target.rglob("*") if p.is_file())
        return "\n".join(str(item) for item in entries[:500]) or "（空目录）"
