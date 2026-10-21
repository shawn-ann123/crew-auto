"""主模型额度耗尽或不可用时，自动切换到预备模型的 LLM 包装器。"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any

from crewai import LLM
from crewai.llms.base_llm import BaseLLM
from crewai.types.usage_metrics import UsageMetrics
from pydantic import Field, PrivateAttr

logger = logging.getLogger("rm_research.fallback_llm")

_SWITCH_STATUS = {401, 402, 403, 429, 500, 502, 503, 504}
_SWITCH_MARKERS = (
    "insufficient quota",
    "insufficient_quota",
    "exceeded your current quota",
    "quota exceeded",
    "insufficient balance",
    "payment required",
    "arrearage",
    "account is in arrears",
    "billing",
    "余额不足",
    "欠费",
    "配额",
    "额度",
    "rate limit",
    "rate-limit",
    "too many requests",
    "resource exhausted",
    "throttl",
    "invalid api key",
    "incorrect api key",
    "unauthorized",
    "authentication",
    "认证失败",
    "密钥无效",
)


def _positive_int(name: str, default: int) -> int:
    try:
        return max(1, int(os.getenv(name, str(default))))
    except ValueError:
        return default


def _nonnegative_float(name: str, default: float) -> float:
    try:
        return max(0.0, float(os.getenv(name, str(default))))
    except ValueError:
        return default


def _build_llm(config: Any) -> BaseLLM:
    if isinstance(config, BaseLLM):
        return config
    if isinstance(config, str):
        return LLM(model=config)
    if isinstance(config, dict):
        cfg = dict(config)
        model = (
            cfg.get("model") or cfg.get("model_name") or cfg.get("deployment_name")
        )
        if not model:
            raise ValueError("模型配置必须包含 model")
        cfg["model"] = model
        cfg.pop("model_name", None)
        cfg.pop("deployment_name", None)
        return LLM(**cfg)
    raise TypeError("模型必须是 LLM 实例、模型名字符串或配置字典")


def _status_code(exc: BaseException) -> int | None:
    for obj in (exc, getattr(exc, "response", None)):
        code = getattr(obj, "status_code", None)
        if isinstance(code, int):
            return code
    return None


def _error_text(exc: BaseException) -> str:
    parts = [type(exc).__name__, str(exc)]
    for attr in ("message", "code", "body"):
        value = getattr(exc, attr, None)
        if value:
            parts.append(str(value))
    return " ".join(parts).lower()


def should_switch(exc: BaseException) -> bool:
    if isinstance(exc, (KeyboardInterrupt, SystemExit)):
        return False
    if _status_code(exc) in _SWITCH_STATUS:
        return True
    text = _error_text(exc)
    return any(marker in text for marker in _SWITCH_MARKERS)


class FallbackLLM(BaseLLM):
    """主模型失败一次后，本实例后续调用固定走预备模型。"""

    primary: Any = Field(exclude=True, repr=False)
    fallback_config: Any = Field(default=None, exclude=True, repr=False)

    _using_fallback: bool = PrivateAttr(default=False)
    _fallback_llm: BaseLLM | None = PrivateAttr(default=None)
    _last_primary_failure: float = PrivateAttr(default=0.0)

    def __init__(self, primary: Any, fallback_config: Any, **kwargs: Any) -> None:
        super().__init__(
            model=getattr(primary, "model", "fallback"),
            provider=getattr(primary, "provider", "openai"),
            primary=primary,
            fallback_config=fallback_config,
            **kwargs,
        )

    def _fallback_name(self) -> str:
        config = self.fallback_config
        if isinstance(config, BaseLLM):
            return str(getattr(config, "model", type(config).__name__))
        if isinstance(config, str):
            return config
        if isinstance(config, dict):
            return str(config.get("model") or config.get("model_name") or "?")
        return "?"

    def _get_fallback(self) -> BaseLLM:
        if self._fallback_llm is None:
            try:
                self._fallback_llm = _build_llm(self.fallback_config)
            except Exception as exc:
                raise RuntimeError(
                    f"主模型不可用，且预备模型 {self._fallback_name()} 初始化失败：{exc}"
                ) from exc
        return self._fallback_llm

    def _active(self) -> BaseLLM:
        return self._get_fallback() if self._using_fallback else self.primary

    def _switch(self, exc: BaseException) -> None:
        self._using_fallback = True
        self._last_primary_failure = time.monotonic()
        logger.warning(
            "主模型 %s 调用失败（%s），切换到预备模型 %s；本实例后续调用不再尝试主模型。",
            self.model,
            exc,
            self._fallback_name(),
        )

    def _primary_recheck_due(self) -> bool:
        interval = _nonnegative_float("MODEL_PRIMARY_RECHECK_SECONDS", 300.0)
        return self._using_fallback and time.monotonic() - self._last_primary_failure >= interval

    def _call_with_retry(self, llm: BaseLLM, messages: Any, **kwargs: Any) -> Any:
        attempts = _positive_int("MODEL_RETRY_ATTEMPTS", 3)
        delay = _nonnegative_float("MODEL_RETRY_DELAY_SECONDS", 10.0)
        for attempt in range(1, attempts + 1):
            try:
                return llm.call(messages, **kwargs)
            except Exception as exc:
                if not should_switch(exc) or attempt == attempts:
                    raise
                logger.warning("模型调用暂时失败，第 %s/%s 次自动重连：%s", attempt, attempts, exc)
                if delay:
                    time.sleep(delay)
        raise RuntimeError("模型重试状态异常")  # pragma: no cover

    async def _acall_with_retry(self, llm: BaseLLM, messages: Any, **kwargs: Any) -> Any:
        attempts = _positive_int("MODEL_RETRY_ATTEMPTS", 3)
        delay = _nonnegative_float("MODEL_RETRY_DELAY_SECONDS", 10.0)
        for attempt in range(1, attempts + 1):
            try:
                return await llm.acall(messages, **kwargs)
            except Exception as exc:
                if not should_switch(exc) or attempt == attempts:
                    raise
                logger.warning("模型调用暂时失败，第 %s/%s 次自动重连：%s", attempt, attempts, exc)
                if delay:
                    await asyncio.sleep(delay)
        raise RuntimeError("模型重试状态异常")  # pragma: no cover

    def call(
        self,
        messages: Any,
        tools: Any = None,
        callbacks: Any = None,
        available_functions: Any = None,
        from_task: Any = None,
        from_agent: Any = None,
        response_model: Any = None,
    ) -> Any:
        call_kwargs = {
            "tools": tools,
            "callbacks": callbacks,
            "available_functions": available_functions,
            "from_task": from_task,
            "from_agent": from_agent,
            "response_model": response_model,
        }
        if not self._using_fallback or self._primary_recheck_due():
            try:
                result = self._call_with_retry(self.primary, messages, **call_kwargs)
                self._using_fallback = False
                return result
            except Exception as exc:
                if not should_switch(exc):
                    raise
                self._switch(exc)
        return self._call_with_retry(self._get_fallback(), messages, **call_kwargs)

    async def acall(
        self,
        messages: Any,
        tools: Any = None,
        callbacks: Any = None,
        available_functions: Any = None,
        from_task: Any = None,
        from_agent: Any = None,
        response_model: Any = None,
    ) -> Any:
        call_kwargs = {
            "tools": tools,
            "callbacks": callbacks,
            "available_functions": available_functions,
            "from_task": from_task,
            "from_agent": from_agent,
            "response_model": response_model,
        }
        if not self._using_fallback or self._primary_recheck_due():
            try:
                result = await self._acall_with_retry(self.primary, messages, **call_kwargs)
                self._using_fallback = False
                return result
            except Exception as exc:
                if not should_switch(exc):
                    raise
                self._switch(exc)
        return await self._acall_with_retry(self._get_fallback(), messages, **call_kwargs)

    def supports_function_calling(self) -> bool:
        method = getattr(self._active(), "supports_function_calling", None)
        return bool(method()) if callable(method) else True

    def supports_stop_words(self) -> bool:
        method = getattr(self._active(), "supports_stop_words", None)
        return bool(method()) if callable(method) else True

    def supports_multimodal(self) -> bool:
        method = getattr(self._active(), "supports_multimodal", None)
        return bool(method()) if callable(method) else False

    def get_context_window_size(self) -> int:
        method = getattr(self._active(), "get_context_window_size", None)
        return int(method()) if callable(method) else 4096

    def get_token_usage_summary(self) -> UsageMetrics:
        total = UsageMetrics()
        for llm in (self.primary, self._fallback_llm):
            if llm is not None:
                total.add_usage_metrics(llm.get_token_usage_summary())
        return total

    def to_config_dict(self) -> dict[str, Any]:
        return self.primary.to_config_dict()


def make_fallback_llm(primary: Any, fallback: Any) -> FallbackLLM:
    """主模型立即构建；预备模型在第一次需要切换时才构建。"""
    return FallbackLLM(primary=_build_llm(primary), fallback_config=fallback)
