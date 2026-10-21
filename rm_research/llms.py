"""全项目统一模型入口。

用户只需在项目根目录的 .env 中配置主模型、备用模型和独立审校模型。
角色配置不再分别保存地址或密钥。
"""

from __future__ import annotations

import os
from typing import Any

from crewai import LLM
from dotenv import load_dotenv

from tools.fallback_llm import make_fallback_llm


load_dotenv(override=True)


def _value(name: str, *legacy_names: str, default: str = "") -> str:
    for candidate in (name, *legacy_names):
        value = os.getenv(candidate, "").strip()
        if value:
            return value
    return default


def _model_config(prefix: str, *, default_model: str = "") -> dict[str, Any] | None:
    model = _value(f"{prefix}_MODEL_NAME", default=default_model)
    if not model:
        return None

    if prefix == "MODEL":
        api_key = _value("MODEL_API_KEY", "DEEPSEEK_API_KEY", "OPENAI_API_KEY")
        base_url = _value("MODEL_BASE_URL")
    elif prefix == "BACKUP":
        api_key = _value("BACKUP_MODEL_API_KEY", "DASHSCOPE_API_KEY")
        base_url = _value("BACKUP_MODEL_BASE_URL", "DASHSCOPE_BASE_URL")
    else:
        api_key = _value("REVIEW_MODEL_API_KEY")
        base_url = _value("REVIEW_MODEL_BASE_URL")

    if not api_key:
        raise RuntimeError(
            f".env 中已配置 {prefix}_MODEL_NAME={model}，但没有配置对应的 API Key。"
        )

    config: dict[str, Any] = {
        "model": model,
        "api_key": api_key,
        "temperature": 0.2,
    }
    if base_url:
        config["base_url"] = base_url
    return config


PRIMARY = _model_config("MODEL", default_model="deepseek/deepseek-flash")
BACKUP = _model_config("BACKUP")
REVIEW = _model_config("REVIEW")


def _build(config: dict[str, Any]) -> LLM:
    return LLM(**config)


def profile(*, reviewer: bool = False):
    """普通角色使用主模型+可选备用模型；审校角色优先使用独立模型。"""
    primary = REVIEW if reviewer and REVIEW else PRIMARY
    if primary is None:  # pragma: no cover
        raise RuntimeError("未配置主模型。")
    if BACKUP and BACKUP.get("model") != primary.get("model"):
        return make_fallback_llm(primary, BACKUP)
    return _build(primary)


PIPELINE_ARCHITECT_LLM = profile()
QUERY_PLANNER_LLM = profile()
LIT_RESEARCHER_LLM = profile()
OSS_SCOUT_LLM = profile()
CROSS_DOMAIN_SCOUT_LLM = profile()
PAPER_DIGESTER_LLM = profile()
NIGHT_LIBRARIAN_LLM = profile()
EVIDENCE_CURATOR_LLM = profile()
MODULE_DESIGNER_LLM = profile()
FEASIBILITY_ASSESSOR_LLM = profile()
COMPLIANCE_REVIEWER_LLM = profile(reviewer=True)
REPORT_WRITER_LLM = profile()
IMPLEMENTATION_PLANNER_LLM = profile()
TEST_PLANNER_LLM = profile()
