"""OpenAI LLM provider using the native openai SDK."""

from __future__ import annotations

import base64
import json
import time
from collections.abc import AsyncIterator
from types import SimpleNamespace

import httpx
from openai import AsyncOpenAI
from pydantic import BaseModel

from backend.runtime.resource_gates import llm_request_slot
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


class _RetryableRawChatError(RuntimeError):
    """Raw compatibility error that should use the shared retry budget."""

    def __init__(self, message: str, *, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code


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
        self._api_key = api_key
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

    def _should_use_raw_compat_fallback(
        self,
        kwargs: dict,
        exc: BaseException,
    ) -> bool:
        model = str(kwargs.get("model") or "")
        if not self._base_url:
            return False
        if self._provider_name != "openai":
            return False
        if self._is_official_openai_request(model) or self._is_deepseek_request(model):
            return False

        status = getattr(exc, "status_code", None) or getattr(exc, "status", None)
        response = getattr(exc, "response", None)
        if response is not None:
            status = status or getattr(response, "status_code", None)
        text = str(exc).lower()
        return (
            "your request was blocked" in text
            or "request was blocked" in text
            or "instructions are required" in text
            or "non-json" in text
            or "<!doctype html" in text
            or "<html" in text
            or status in {403, 406}
            or (isinstance(status, int) and 500 <= status < 600)
        )

    def _raw_chat_endpoint_candidates(self) -> list[str]:
        base_url = self._base_url.rstrip("/")
        if not base_url:
            return []
        normalized = base_url.lower()
        if normalized.endswith("/chat/completions"):
            return [base_url]
        if normalized.endswith("/v1"):
            return [f"{base_url}/chat/completions"]
        return [
            f"{base_url}/v1/chat/completions",
            f"{base_url}/chat/completions",
        ]

    def _raw_chat_payload_variants(self, kwargs: dict) -> list[dict]:
        payload = dict(kwargs)
        extra_body = payload.pop("extra_body", None)
        if isinstance(extra_body, dict):
            payload.update(extra_body)
        payload.pop("stream", None)

        variants = [payload]
        without_token_limit = dict(payload)
        removed_token_limit = False
        for key in ("max_tokens", "max_completion_tokens"):
            if key in without_token_limit:
                without_token_limit.pop(key, None)
                removed_token_limit = True
        if removed_token_limit:
            variants.append(without_token_limit)

        without_sampling = dict(without_token_limit if removed_token_limit else payload)
        if "temperature" in without_sampling:
            without_sampling.pop("temperature", None)
            if without_sampling not in variants:
                variants.append(without_sampling)
        return variants

    def _content_from_raw_message(self, message: dict) -> str:
        content = message.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts: list[str] = []
            for part in content:
                if isinstance(part, str):
                    parts.append(part)
                elif isinstance(part, dict) and isinstance(part.get("text"), str):
                    parts.append(part["text"])
            return "".join(parts)
        return ""

    def _raw_chat_response_to_namespace(self, data: dict):
        choices = []
        for choice in data.get("choices") or []:
            if not isinstance(choice, dict):
                continue
            message = choice.get("message")
            if not isinstance(message, dict):
                continue
            choices.append(
                SimpleNamespace(
                    index=choice.get("index", len(choices)),
                    finish_reason=choice.get("finish_reason"),
                    message=SimpleNamespace(
                        role=message.get("role") or "assistant",
                        content=self._content_from_raw_message(message),
                    ),
                )
            )

        if not choices and isinstance(data.get("output_text"), str):
            choices.append(
                SimpleNamespace(
                    index=0,
                    finish_reason=None,
                    message=SimpleNamespace(
                        role="assistant",
                        content=data["output_text"],
                    ),
                )
            )

        usage = None
        usage_data = data.get("usage")
        if isinstance(usage_data, dict):
            usage = SimpleNamespace(
                prompt_tokens=int(usage_data.get("prompt_tokens") or 0),
                completion_tokens=int(usage_data.get("completion_tokens") or 0),
            )

        return SimpleNamespace(
            id=data.get("id"),
            object=data.get("object"),
            model=data.get("model"),
            choices=choices,
            usage=usage,
            raw=data,
        )

    def _raw_chat_error_priority(self, exc: BaseException) -> int:
        text = str(exc).lower()
        if "non-json response" in text and (
            "<!doctype html" in text or "<html" in text
        ):
            return 0
        if "returned no message content" in text:
            return 1
        return 2

    def _remember_raw_chat_error(
        self,
        current: BaseException | None,
        candidate: BaseException,
    ) -> BaseException:
        if current is None:
            return candidate
        if self._raw_chat_error_priority(candidate) >= self._raw_chat_error_priority(current):
            return candidate
        return current

    async def _create_raw_chat_completion(self, kwargs: dict):
        endpoints = self._raw_chat_endpoint_candidates()
        payloads = self._raw_chat_payload_variants(kwargs)
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "Mozilla/5.0",
        }

        async def attempt():
            last_error: BaseException | None = None
            timeout = httpx.Timeout(120.0, connect=30.0)
            async with httpx.AsyncClient(timeout=timeout) as client:
                for endpoint in endpoints:
                    endpoint_retryable_error: _RetryableRawChatError | None = None
                    for payload in payloads:
                        try:
                            response = await client.post(
                                endpoint,
                                headers=headers,
                                json=payload,
                            )
                            if response.status_code >= 400:
                                message = (
                                    f"OpenAI-compatible endpoint returned "
                                    f"{response.status_code} from {endpoint}: "
                                    f"{response.text[:500]}"
                                )
                                if response.status_code == 429 or response.status_code >= 500:
                                    endpoint_retryable_error = _RetryableRawChatError(
                                        message,
                                        status_code=response.status_code,
                                    )
                                    last_error = self._remember_raw_chat_error(
                                        last_error,
                                        endpoint_retryable_error,
                                    )
                                    continue
                                last_error = self._remember_raw_chat_error(
                                    last_error,
                                    RuntimeError(message),
                                )
                                continue
                            try:
                                data = response.json()
                            except json.JSONDecodeError:
                                preview = response.text[:500].strip()
                                if not preview:
                                    preview = "<empty response body>"
                                last_error = self._remember_raw_chat_error(
                                    last_error,
                                    RuntimeError(
                                        "OpenAI-compatible endpoint returned non-JSON "
                                        f"response from {endpoint}: {preview}"
                                    ),
                                )
                                continue
                            resp = self._raw_chat_response_to_namespace(data)
                            if resp.choices and (
                                resp.choices[0].message.content or ""
                            ).strip():
                                return resp
                            endpoint_retryable_error = _RetryableRawChatError(
                                "OpenAI-compatible endpoint returned no message "
                                f"content from {endpoint}",
                                status_code=502,
                            )
                            last_error = self._remember_raw_chat_error(
                                last_error,
                                endpoint_retryable_error,
                            )
                        except httpx.TimeoutException as exc:
                            endpoint_retryable_error = _RetryableRawChatError(
                                "OpenAI-compatible endpoint timed out after "
                                f"120s from {endpoint}. The gateway or upstream "
                                "model did not finish the request in time; try "
                                "again with a smaller prompt, a faster model, or "
                                "a gateway with a longer upstream timeout.",
                                status_code=504,
                            )
                            last_error = self._remember_raw_chat_error(
                                last_error,
                                endpoint_retryable_error,
                            )
                            break
                        except BaseException as exc:
                            last_error = self._remember_raw_chat_error(last_error, exc)
                    if endpoint_retryable_error is not None:
                        raise endpoint_retryable_error
            if last_error:
                raise last_error
            raise RuntimeError("OpenAI-compatible endpoint did not return a response")

        return await call_with_retry(attempt)

    async def _create_chat_completion(self, kwargs: dict):
        try:
            return await call_with_retry(
                lambda: self._client.chat.completions.create(**kwargs)
            )
        except BaseException as exc:
            if self._should_use_raw_compat_fallback(kwargs, exc):
                return await self._create_raw_chat_completion(kwargs)
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
        async with llm_request_slot():
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

        async with llm_request_slot():
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
