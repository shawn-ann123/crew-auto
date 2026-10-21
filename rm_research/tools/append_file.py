"""在项目目录内安全追加文本（用于夜间记录的 C 阶段追加）。"""

from pathlib import Path

from crewai.tools import BaseTool
from pydantic import BaseModel, Field

from tools.state_io import atomic_write_text


class AppendFileInput(BaseModel):
    path: str = Field(..., description="相对项目根目录的文件路径，例如 nights/2026-10-18.md")
    content: str = Field(..., description="要追加的完整 Markdown 片段")


class AppendFileTool(BaseTool):
    name: str = "append_file"
    description: str = (
        "把内容追加到项目内的指定文件末尾（自动创建目录和文件，不覆盖已有内容）。"
        "仅允许写入当前项目目录内。"
    )
    args_schema: type[BaseModel] = AppendFileInput

    def _run(self, path: str, content: str) -> str:
        root = Path.cwd().resolve()
        target = (root / path).resolve()
        if not target.is_relative_to(root) or target == root:
            return "追加失败：路径必须位于项目目录内。"
        relative = target.relative_to(root)
        allowed = (
            relative.parts
            and relative.parts[0] == "nights"
            or str(relative) == "审核方案.md"
        )
        if not allowed:
            return "追加失败：只允许追加夜间记录或《审核方案.md》。"
        if not content.strip():
            return "追加失败：content 为空。"
        existing = target.read_text(encoding="utf-8") if target.exists() else ""
        if str(relative) == "审核方案.md":
            existing = existing.replace(
                "_尚无待审方案。完成一次夜间研究后，新方案会追加在下方。_",
                "",
            ).rstrip()
        atomic_write_text(target, existing.rstrip() + "\n\n" + content.strip() + "\n")
        return f"已追加 {len(content)} 字符到 {target.relative_to(root)}"
