"""按明确指定的 JSONC 工作流运行 CrewAI，避免被 pyproject 默认阶段覆盖。"""

from __future__ import annotations

import argparse
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--crew", required=True, help="要运行的 crew JSON/JSONC 文件")
    parser.add_argument("--inputs", help="传给工作流的 JSON 输入")
    parser.add_argument("--check", action="store_true", help="只校验配置，不调用模型")
    args = parser.parse_args()

    crew_path = Path(args.crew).resolve()
    if args.check:
        from crewai.project.json_loader import validate_crew_project

        validate_crew_project(crew_path)
        print(f"工作流配置有效：{crew_path.name}")
        return 0

    # CrewAI CLI 的通用 run 命令会重新读取 pyproject 默认定义。
    # 这里显式传入当前阶段，并在项目环境内直接运行。
    from crewai_cli.run_crew import _run_json_crew

    _run_json_crew(crew_path=crew_path, inputs=args.inputs)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
