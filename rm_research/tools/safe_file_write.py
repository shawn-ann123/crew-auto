"""只写入自动生成的研究产物目录。"""

from pathlib import Path

from crewai.tools import BaseTool
from pydantic import BaseModel, Field

from tools.state_io import atomic_write_text


WRITABLE_ROOTS = {"baseline", "proposals", "code"}


class SafeFileWriteInput(BaseModel):
    file_path: str = Field(..., description="项目内相对路径")
    content: str = Field(..., description="完整文件内容")
    overwrite: bool = Field(False, description="已存在时是否覆盖")


class SafeFileWriteTool(BaseTool):
    name: str = "FileWriterTool"
    description: str = "原子写入基线、内部方案或实施文档，不得修改模型配置和密钥。"
    args_schema: type[BaseModel] = SafeFileWriteInput

    def _run(self, file_path: str, content: str, overwrite: bool = False) -> str:
        root = Path.cwd().resolve()
        target = (root / file_path).resolve()
        try:
            relative = target.relative_to(root)
        except ValueError:
            return "写入失败：路径不在项目内。"
        if not relative.parts or relative.parts[0] not in WRITABLE_ROOTS:
            return "写入失败：该路径不在自动产物白名单内。"
        if target.exists() and not overwrite:
            return f"写入失败：{relative} 已存在，需明确 overwrite=true。"
        if not content.strip():
            return "写入失败：content 为空。"
        atomic_write_text(target, content.rstrip() + "\n")
        return f"已写入 {relative}（{len(content)} 字符）"
