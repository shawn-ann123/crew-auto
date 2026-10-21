"""只读取研究流程明确允许的资料与产物。"""

from pathlib import Path

from crewai.tools import BaseTool
from pydantic import BaseModel, Field


ALLOWED_ROOTS = {
    "materials",
    "baseline",
    "library",
    "queue",
    "nights",
    "proposals",
    "code",
}
ALLOWED_ROOT_FILES = {"审核方案.md"}
MAX_CHARS = 80_000


class SafeFileReadInput(BaseModel):
    file_path: str = Field(..., description="项目内相对路径")


class SafeFileReadTool(BaseTool):
    name: str = "FileReadTool"
    description: str = (
        "读取项目内的资料、基线、证据、夜间记录、方案和实施文档。"
        "不允许读取 .env、Git 元数据或项目外文件。"
    )
    args_schema: type[BaseModel] = SafeFileReadInput

    def _run(self, file_path: str) -> str:
        root = Path.cwd().resolve()
        target = (root / file_path).resolve()
        try:
            relative = target.relative_to(root)
        except ValueError:
            return "读取失败：路径不在项目内。"
        if not relative.parts or (
            relative.name.startswith(".")
            or (
                relative.parts[0] not in ALLOWED_ROOTS
                and str(relative) not in ALLOWED_ROOT_FILES
            )
        ):
            return "读取失败：该路径不在研究资料白名单内。"
        if not target.is_file():
            return f"读取失败：文件不存在：{relative}"
        text = target.read_text(encoding="utf-8", errors="replace")
        if len(text) > MAX_CHARS:
            return text[:MAX_CHARS] + f"\n\n……（已截断，原文 {len(text)} 字符）"
        return text
