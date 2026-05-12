"""OpenAI LLM provider using the native openai SDK."""

from __future__ import annotations

import base64
import time
from collections.abc import AsyncIterator

from openai import AsyncOpenAI
from pydantic import BaseModel

from backend.usage.tracker import current_usage_context, usage_tracker

from .base import LLMProvider
from .retry import call_with_retry
from .types import (
    ContentBlock,
    LLMMessage,
    LLMResponse,
    LLMStreamChunk,
    ModelInfo,
    ProviderInfo,
    TokenUsage,
)

OPENAI_GPT_REASONING_EFFORTS = {"none", "low", "medium", "high", "xhigh"}
OPENAI_GPT_VERBOSITIES = {"low", "medium", "high"}
DEFAULT_OPENAI_GPT_SETTINGS = {
    "reasoning_effort": "medium",
    "verbosity": "high",
}


def normalize_openai_base_url(base_url: str | None) -> str | None:
    """Return an SDK base URL from a user-entered OpenAI-compatible URL.

    The OpenAI SDK expects the API root, for example ``https://host/v1``.
    Users often paste the full chat-completions endpoint; if passed through
    unchanged the SDK appends ``/chat/completions`` again.
    """
    if not base_url:
        return None
    normalized = base_url.strip().rstrip("/")
    suffix = "/chat/completions"
    if normalized.lower().endswith(suffix):
        normalized = normalized[: -len(suffix)].rstrip("/")
    return normalized or None


class OpenAIProvider(LLMProvider):
    """OpenAI provider wrapping AsyncOpenAI."""

    def __init__(
        self,
        api_key: str,
        base_url: str | None = None,
        provider_name: str = "openai",
        deepseek_settings: dict | None = None,
        openai_settings: dict | None = None,
    ) -> None:
        normalized_base_url = normalize_openai_base_url(base_url)
        self._client = AsyncOpenAI(api_key=api_key, base_url=normalized_base_url)
        self._provider_name = provider_name
        self._base_url = (normalized_base_url or "").rstrip("/")
        self._deepseek_settings = deepseek_settings
        self._openai_settings = openai_settings

    def _is_deepseek_request(self, model: str | None = None) -> bool:
        return (
            self._provider_name == "deepseek"
            or "api.deepseek.com" in self._base_url
            or (model or "").startswith("deepseek")
        )

    def _normalize_max_tokens(self, model: str, max_tokens: int | None) -> int | None:
        return max_tokens

    def _is_official_openai_request(self, model: str | None = None) -> bool:
        if self._is_deepseek_request(model):
            return False
        if self._provider_name != "openai":
            return False
        return not self._base_url or "api.openai.com" in self._base_url

    def _is_openai_gpt5_or_newer(self, model: str | None) -> bool:
        normalized = (model or "").lower().strip()
        if not normalized.startswith("gpt-"):
            return False
        version = normalized[4:].split("-", 1)[0]
        try:
            return float(version) >= 5
        except ValueError:
            return normalized.startswith("gpt-5")

    def _normalized_openai_settings(self) -> dict[str, str]:
        raw = self._openai_settings or {}
        reasoning_effort = str(
            raw.get("reasoning_effort")
            or DEFAULT_OPENAI_GPT_SETTINGS["reasoning_effort"]
        )
        verbosity = str(raw.get("verbosity") or DEFAULT_OPENAI_GPT_SETTINGS["verbosity"])
        if reasoning_effort not in OPENAI_GPT_REASONING_EFFORTS:
            reasoning_effort = DEFAULT_OPENAI_GPT_SETTINGS["reasoning_effort"]
        if verbosity not in OPENAI_GPT_VERBOSITIES:
            verbosity = DEFAULT_OPENAI_GPT_SETTINGS["verbosity"]
        return {
            "reasoning_effort": reasoning_effort,
            "verbosity": verbosity,
        }

    def _build_chat_kwargs(
        self,
        messages: list[LLMMessage],
        model: str,
        *,
        temperature: float,
        max_tokens: int | None,
        stream: bool = False,
    ) -> dict:
        normalized_max_tokens = self._normalize_max_tokens(model, max_tokens)
        is_deepseek = self._is_deepseek_request(model)
        is_official_openai = self._is_official_openai_request(model)
        is_openai_gpt5 = is_official_openai and self._is_openai_gpt5_or_newer(model)
        kwargs: dict = {
            "model": model,
            "messages": self._convert_messages(messages),
        }
        if not is_openai_gpt5:
            kwargs["temperature"] = temperature
        if normalized_max_tokens:
            if is_official_openai:
                kwargs["max_completion_tokens"] = normalized_max_tokens
            else:
                kwargs["max_tokens"] = normalized_max_tokens
        if is_openai_gpt5:
            kwargs.update(self._normalized_openai_settings())
        if is_deepseek:
            self._apply_deepseek_thinking_kwargs(kwargs, model)
        if stream:
            kwargs["stream"] = True
        return kwargs

    def _fallback_chat_kwargs(self, kwargs: dict) -> list[dict]:
        fallbacks: list[dict] = []

        without_gpt_controls = dict(kwargs)
        removed_gpt_controls = False
        for key in ("reasoning_effort", "verbosity"):
            if key in without_gpt_controls:
                without_gpt_controls.pop(key, None)
                removed_gpt_controls = True
        if removed_gpt_controls:
            fallbacks.append(without_gpt_controls)

        legacy_tokens = dict(without_gpt_controls if removed_gpt_controls else kwargs)
        if "max_completion_tokens" in legacy_tokens:
            legacy_tokens["max_tokens"] = legacy_tokens.pop("max_completion_tokens")
            if legacy_tokens not in fallbacks:
                fallbacks.append(legacy_tokens)

        return fallbacks

    def _is_parameter_compat_error(self, exc: BaseException) -> bool:
        if isinstance(exc, TypeError):
            return True
        status = getattr(exc, "status_code", None) or getattr(exc, "status", None)
        response = getattr(exc, "response", None)
        if response is not None:
            status = status or getattr(response, "status_code", None)
        if status != 400:
            return False
        text = str(exc).lower()
        return any(
            marker in text
            for marker in (
                "max_completion_tokens",
                "max_tokens",
                "reasoning_effort",
                "verbosity",
                "unsupported",
                "unrecognized",
                "unknown parameter",
                "unexpected keyword",
            )
        )

    async def _create_chat_completion(self, kwargs: dict):
        try:
            return await call_with_retry(
                lambda: self._client.chat.completions.create(**kwargs)
            )
        except BaseException as exc:
            fallbacks = self._fallback_chat_kwargs(kwargs)
            if not fallbacks or not self._is_parameter_compat_error(exc):
                raise
            for index, fallback in enumerate(fallbacks):
                try:
                    return await call_with_retry(
                        lambda: self._client.chat.completions.create(**fallback)
                    )
                except BaseException as fallback_exc:
                    if (
                        index >= len(fallbacks) - 1
                        or not self._is_parameter_compat_error(fallback_exc)
                    ):
                        raise
            raise

    async def _parse_chat_completion(
        self,
        kwargs: dict,
        response_format: type[BaseModel],
    ):
        try:
            return await call_with_retry(
                lambda: self._client.beta.chat.completions.parse(
                    **kwargs,
                    response_format=response_format,
                )
            )
        except BaseException as exc:
            fallbacks = self._fallback_chat_kwargs(kwargs)
            if not fallbacks or not self._is_parameter_compat_error(exc):
                raise
            for index, fallback in enumerate(fallbacks):
                try:
                    return await call_with_retry(
                        lambda: self._client.beta.chat.completions.parse(
                            **fallback,
                            response_format=response_format,
                        )
                    )
                except BaseException as fallback_exc:
                    if (
                        index >= len(fallbacks) - 1
                        or not self._is_parameter_compat_error(fallback_exc)
                    ):
                        raise
            raise

    def _apply_deepseek_thinking_kwargs(self, kwargs: dict, model: str) -> None:
        settings = self._deepseek_settings
        if settings is None:
            if model != "deepseek-v4-pro":
                return
            thinking_enabled = True
            reasoning_effort = "max"
        else:
            thinking_enabled = bool(settings.get("thinking_enabled", True))
            reasoning_effort = str(settings.get("reasoning_effort") or "max")
            if reasoning_effort not in {"high", "max"}:
                reasoning_effort = "max"

        kwargs["extra_body"] = {
            "thinking": {"type": "enabled" if thinking_enabled else "disabled"}
        }
        if thinking_enabled:
            kwargs["reasoning_effort"] = reasoning_effort
            # DeepSeek thinking mode ignores sampling params; omit them to
            # keep the request aligned with the documented API contract.
            kwargs.pop("temperature", None)

    def _convert_messages(self, messages: list[LLMMessage]) -> list[dict]:
        """Convert LLMMessage list to OpenAI message format."""
        result = []
        for msg in messages:
            if isinstance(msg.content, str):
                result.append({"role": msg.role, "content": msg.content})
            else:
                parts = []
                for block in msg.content:
                    if block.type == "text" and block.text:
                        parts.append({"type": "text", "text": block.text})
                    elif block.type == "image" and block.image_data:
                        b64 = base64.b64encode(block.image_data).decode()
                        media = block.image_media_type or "image/png"
                        parts.append({
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:{media};base64,{b64}",
                            },
                        })
                result.append({"role": msg.role, "content": parts})
        return result

    async def chat(
        self,
        messages: list[LLMMessage],
        model: str,
        *,
        temperature: float = 0.7,
        max_tokens: int | None = None,
        response_format: type[BaseModel] | None = None,
    ) -> LLMResponse:
        kwargs = self._build_chat_kwargs(
            messages,
            model,
            temperature=temperature,
            max_tokens=max_tokens,
        )

        t0 = time.monotonic()
        if response_format:
            resp = await self._parse_chat_completion(kwargs, response_format)
        else:
            resp = await self._create_chat_completion(kwargs)
        duration_ms = int((time.monotonic() - t0) * 1000)

        content = resp.choices[0].message.content or ""
        usage = None
        if resp.usage:
            usage = TokenUsage(
                prompt_tokens=resp.usage.prompt_tokens,
                completion_tokens=resp.usage.completion_tokens,
            )
            ctx = current_usage_context()
            provider_name = "deepseek" if self._is_deepseek_request(model) else "openai"
            usage_tracker.record(
                provider=provider_name,
                model=model,
                prompt_tokens=resp.usage.prompt_tokens,
                completion_tokens=resp.usage.completion_tokens,
                job_id=ctx.get("job_id"),
                stage=ctx.get("stage"),
                page=ctx.get("page"),
                attempt=ctx.get("attempt") or 1,
                duration_ms=duration_ms,
            )
        return LLMResponse(content=content, usage=usage, raw=resp)

    async def chat_stream(
        self,
        messages: list[LLMMessage],
        model: str,
        *,
        temperature: float = 0.7,
        max_tokens: int | None = None,
    ) -> AsyncIterator[LLMStreamChunk]:
        kwargs = self._build_chat_kwargs(
            messages,
            model,
            temperature=temperature,
            max_tokens=max_tokens,
            stream=True,
        )

        stream = None
        try:
            stream = await self._client.chat.completions.create(**kwargs)
        except BaseException as exc:
            fallbacks = self._fallback_chat_kwargs(kwargs)
            if not fallbacks or not self._is_parameter_compat_error(exc):
                raise
            for index, fallback in enumerate(fallbacks):
                try:
                    stream = await self._client.chat.completions.create(**fallback)
                    break
                except BaseException as fallback_exc:
                    if (
                        index >= len(fallbacks) - 1
                        or not self._is_parameter_compat_error(fallback_exc)
                    ):
                        raise
        assert stream is not None
        async for chunk in stream:
            delta = chunk.choices[0].delta
            if delta.content:
                yield LLMStreamChunk(
                    delta=delta.content,
                    finish_reason=chunk.choices[0].finish_reason,
                )

    async def validate(self) -> bool:
        try:
            await self._client.models.list()
            return True
        except Exception:
            return False

    def get_provider_info(self) -> ProviderInfo:
        if self._provider_name == "deepseek":
            return ProviderInfo(
                name="deepseek",
                display_name="DeepSeek",
                default_base_url="https://api.deepseek.com",
                models=[
                    ModelInfo(
                        id="deepseek-v4-flash",
                        display_name="DeepSeek V4 Flash",
                        supports_vision=True,
                        supports_structured_output=True,
                        context_window=128000,
                    ),
                    ModelInfo(
                        id="deepseek-v4-pro",
                        display_name="DeepSeek V4 Pro",
                        supports_vision=True,
                        supports_structured_output=True,
                        context_window=128000,
                    ),
                ],
            )
        return ProviderInfo(
            name="openai",
            display_name="OpenAI",
            models=[
                ModelInfo(
                    id="gpt-5.5",
                    display_name="GPT-5.5",
                    supports_vision=True,
                    supports_structured_output=True,
                    context_window=400000,
                ),
                ModelInfo(
                    id="gpt-5.4",
                    display_name="GPT-5.4",
                    supports_vision=True,
                    supports_structured_output=True,
                    context_window=400000,
                ),
            ],
        )
