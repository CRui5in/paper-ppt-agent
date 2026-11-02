"""OpenAI LLM provider using the native openai SDK."""

from __future__ import annotations

import base64
import json
import time
from collections.abc import AsyncIterator
from types import SimpleNamespace
from urllib.parse import urlsplit

import httpx
from openai import AsyncOpenAI
from openai.lib._parsing import type_to_response_format_param
from pydantic import BaseModel

from backend.config import settings
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
        artifact_thinking_mode: str = "disabled",
        deepseek_settings: dict | None = None,
        openai_settings: dict | None = None,
    ) -> None:
        normalized_base_url = normalize_openai_base_url(base_url)
        # Explicit timeout + max_retries=0: the project's call_with_retry owns
        # retry/backoff, so leaving the SDK's default 2 internal retries on
        # would double-retry and, combined with the 600s default timeout,
        # let a hung upstream block a single call for up to 30 min.
        http_client = httpx.AsyncClient(
            timeout=settings.llm_request_timeout,
            trust_env=False,
        )
        self._client = AsyncOpenAI(
            api_key=api_key,
            base_url=normalized_base_url,
            timeout=settings.llm_request_timeout,
            max_retries=0,
            http_client=http_client,
        )
        self._api_key = api_key
        self._provider_name = provider_name
        self._base_url = (normalized_base_url or "").rstrip("/")
        self._artifact_thinking_mode = (
            artifact_thinking_mode
            if artifact_thinking_mode in {"disabled", "default"}
            else "disabled"
        )
        self._deepseek_settings = deepseek_settings
        self._openai_settings = openai_settings
        # Set once we learn an endpoint rejects streaming, so subsequent
        # calls skip the stream attempt and go straight to the buffered path.
        self._streaming_unsupported = False
        self._direct_artifact_controls_unsupported = False

    def _is_deepseek_request(self, model: str | None = None) -> bool:
        return (
            self._provider_name == "deepseek"
            or "api.deepseek.com" in self._base_url
            or (model or "").startswith("deepseek")
        )

    def _normalize_max_tokens(self, model: str, max_tokens: int | None) -> int | None:
        return max_tokens

    def artifact_max_tokens(self, model: str, visible_tokens: int) -> int | None:
        if self._is_deepseek_request(model) and urlsplit(self._base_url).hostname == "api.deepseek.com":
            if self._artifact_thinking_mode != "default" and not self._direct_artifact_controls_unsupported:
                return visible_tokens
            kwargs = self._build_chat_kwargs([], model, temperature=0.3, max_tokens=None)
            thinking = (kwargs.get("extra_body") or {}).get("thinking") or {}
            enabled = thinking.get("type") != "disabled" and kwargs.get("reasoning_effort") != "none"
            if enabled:
                # DeepSeek's limit covers reasoning plus the answer. Its native
                # thinking-mode default provides the total budget; visible SVG
                # complexity remains bounded in the artifact prompt.
                return None
        return visible_tokens

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
        if (
            self._is_direct_artifact_stage()
            and self._artifact_thinking_mode != "default"
            and not self._direct_artifact_controls_unsupported
        ):
            self._apply_direct_artifact_controls(
                kwargs,
                is_official_openai=is_official_openai,
                is_openai_gpt5=is_openai_gpt5,
            )
        if stream:
            kwargs["stream"] = True
        return kwargs

    def _is_direct_artifact_stage(self) -> bool:
        return str(current_usage_context().get("stage") or "") in {
            "strategy",
            "template_design_spec",
            "generation",
            "repair",
            "visual_qa",
        }

    def _apply_direct_artifact_controls(
        self,
        kwargs: dict,
        *,
        is_official_openai: bool,
        is_openai_gpt5: bool,
    ) -> None:
        """Ask the provider for direct output without hidden reasoning.

        This is stage-driven rather than model-name-driven. Official OpenAI
        uses its native reasoning control; OpenAI-compatible endpoints receive
        the common ``thinking.type`` extension used by reasoning-capable APIs.
        """
        if is_official_openai:
            if is_openai_gpt5:
                kwargs["reasoning_effort"] = "none"
            return
        extra_body = dict(kwargs.get("extra_body") or {})
        extra_body["thinking"] = {"type": "disabled"}
        kwargs["extra_body"] = extra_body
        kwargs.pop("reasoning_effort", None)

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

        without_direct_artifact_controls = dict(
            without_gpt_controls if removed_gpt_controls else kwargs
        )
        extra_body = without_direct_artifact_controls.get("extra_body")
        if isinstance(extra_body, dict) and "thinking" in extra_body:
            next_extra_body = dict(extra_body)
            next_extra_body.pop("thinking", None)
            if next_extra_body:
                without_direct_artifact_controls["extra_body"] = next_extra_body
            else:
                without_direct_artifact_controls.pop("extra_body", None)
            if without_direct_artifact_controls not in fallbacks:
                fallbacks.append(without_direct_artifact_controls)

        legacy_tokens = dict(without_direct_artifact_controls)
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
                "thinking",
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
            completion_details = usage_data.get("completion_tokens_details")
            usage = SimpleNamespace(
                prompt_tokens=int(usage_data.get("prompt_tokens") or 0),
                completion_tokens=int(usage_data.get("completion_tokens") or 0),
                completion_tokens_details=SimpleNamespace(
                    reasoning_tokens=int(
                        completion_details.get("reasoning_tokens") or 0
                    )
                )
                if isinstance(completion_details, dict)
                else None,
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
            async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
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
                lambda: self._consume_chat_stream(kwargs)
            )
        except BaseException as exc:
            if self._should_use_raw_compat_fallback(kwargs, exc):
                return await self._create_raw_chat_completion(kwargs)
            fallbacks = self._fallback_chat_kwargs(kwargs)
            if not fallbacks or not self._is_parameter_compat_error(exc):
                raise
            for index, fallback in enumerate(fallbacks):
                try:
                    response = await call_with_retry(
                        lambda: self._consume_chat_stream(fallback)
                    )
                    if kwargs.get("extra_body") != fallback.get("extra_body"):
                        self._direct_artifact_controls_unsupported = True
                    return response
                except BaseException as fallback_exc:
                    if (
                        index >= len(fallbacks) - 1
                        or not self._is_parameter_compat_error(fallback_exc)
                    ):
                        raise
            raise

    def _is_stream_options_error(self, exc: BaseException) -> bool:
        text = str(exc).lower()
        return "stream_options" in text or "include_usage" in text

    def _is_streaming_unsupported_error(self, exc: BaseException) -> bool:
        """Detect endpoints that reject streaming itself (not just
        ``stream_options``), e.g. a custom OpenAI-compatible gateway whose
        chat endpoint only works in buffered mode and returns a 400 like
        "streaming is not supported"."""
        text = str(exc).lower()
        if "stream_options" in text or "include_usage" in text:
            return False
        return "stream" in text and (
            "not support" in text
            or "unsupported" in text
            or "not allowed" in text
            or "is disabled" in text
        )

    async def _create_buffered_completion(self, kwargs: dict):
        """Original non-streaming path: let the SDK buffer the full response.

        Used for endpoints that only support non-streaming chat completions,
        preserving behavior for setups that worked before streaming was the
        default read mode.
        """
        buffered_kwargs = dict(kwargs)
        buffered_kwargs.pop("stream", None)
        buffered_kwargs.pop("stream_options", None)
        return await self._client.chat.completions.create(**buffered_kwargs)

    def _chat_kwargs_with_response_format(
        self,
        kwargs: dict,
        response_format: type[BaseModel],
    ) -> dict:
        structured_kwargs = dict(kwargs)
        structured_kwargs["response_format"] = type_to_response_format_param(
            response_format
        )
        return structured_kwargs

    def _attach_parsed_response_format(
        self,
        resp,
        response_format: type[BaseModel],
    ):
        content = resp.choices[0].message.content or ""
        parsed = response_format.model_validate_json(content)
        resp.choices[0].message.parsed = parsed
        return resp

    async def _consume_chat_stream(self, kwargs: dict):
        """Run a chat completion in streaming mode, accumulating it into a
        non-streaming response shape the rest of ``chat()`` already expects.

        A single buffered (non-streamed) response forces the upstream to
        withhold all bytes until the full completion is ready; a long
        generation then trips a proxy read timeout (e.g. Cloudflare 524 at
        120s) even though the model is still working. Streaming keeps bytes
        flowing so the timeout never fires. ``include_usage`` asks the server
        for a final usage chunk so token accounting survives the switch.
        """
        # Endpoint already proved it only does buffered responses — don't
        # waste a round trip re-attempting the stream.
        if self._streaming_unsupported:
            return await self._create_buffered_completion(kwargs)

        stream_kwargs = dict(kwargs)
        stream_kwargs["stream"] = True
        stream_kwargs.setdefault("stream_options", {"include_usage": True})

        async def run(create_kwargs: dict):
            content_parts: list[str] = []
            finish_reason = None
            usage = None
            response_id = None
            model_name = None
            stream = await self._client.chat.completions.create(**create_kwargs)
            async for chunk in stream:
                if response_id is None:
                    response_id = getattr(chunk, "id", None)
                if model_name is None:
                    model_name = getattr(chunk, "model", None)
                chunk_usage = getattr(chunk, "usage", None)
                if chunk_usage is not None:
                    usage = chunk_usage
                for choice in getattr(chunk, "choices", None) or []:
                    delta = getattr(choice, "delta", None)
                    delta_content = getattr(delta, "content", None) if delta else None
                    if delta_content:
                        content_parts.append(delta_content)
                    choice_finish = getattr(choice, "finish_reason", None)
                    if choice_finish:
                        finish_reason = choice_finish
            usage_ns = None
            if usage is not None:
                completion_details = getattr(
                    usage,
                    "completion_tokens_details",
                    None,
                )
                usage_ns = SimpleNamespace(
                    prompt_tokens=int(getattr(usage, "prompt_tokens", 0) or 0),
                    completion_tokens=int(getattr(usage, "completion_tokens", 0) or 0),
                    completion_tokens_details=SimpleNamespace(
                        reasoning_tokens=int(
                            getattr(completion_details, "reasoning_tokens", 0) or 0
                        )
                    )
                    if completion_details is not None
                    else None,
                )
            message = SimpleNamespace(role="assistant", content="".join(content_parts))
            return SimpleNamespace(
                id=response_id,
                object="chat.completion",
                model=model_name or kwargs.get("model"),
                choices=[
                    SimpleNamespace(index=0, finish_reason=finish_reason, message=message)
                ],
                usage=usage_ns,
            )

        try:
            return await run(stream_kwargs)
        except BaseException as exc:
            # Some OpenAI-compatible gateways reject stream_options. Retry
            # once without it: we lose usage accounting but keep streaming.
            if "stream_options" in stream_kwargs and self._is_stream_options_error(exc):
                retry_kwargs = dict(stream_kwargs)
                retry_kwargs.pop("stream_options", None)
                try:
                    return await run(retry_kwargs)
                except BaseException as retry_exc:
                    if self._is_streaming_unsupported_error(retry_exc):
                        self._streaming_unsupported = True
                        return await self._create_buffered_completion(kwargs)
                    raise
            # Endpoint rejects streaming entirely (not just stream_options):
            # fall back to the original buffered completion so non-streaming
            # endpoints that worked before keep working.
            if self._is_streaming_unsupported_error(exc):
                self._streaming_unsupported = True
                return await self._create_buffered_completion(kwargs)
            raise

    async def _parse_chat_completion(
        self,
        kwargs: dict,
        response_format: type[BaseModel],
    ):
        structured_kwargs = self._chat_kwargs_with_response_format(
            kwargs,
            response_format,
        )
        try:
            resp = await call_with_retry(
                lambda: self._consume_chat_stream(structured_kwargs)
            )
            return self._attach_parsed_response_format(resp, response_format)
        except BaseException as exc:
            if self._should_use_raw_compat_fallback(structured_kwargs, exc):
                resp = await self._create_raw_chat_completion(structured_kwargs)
                return self._attach_parsed_response_format(resp, response_format)
            fallbacks = self._fallback_chat_kwargs(structured_kwargs)
            if not fallbacks or not self._is_parameter_compat_error(exc):
                raise
            for index, fallback in enumerate(fallbacks):
                try:
                    resp = await call_with_retry(
                        lambda: self._consume_chat_stream(fallback)
                    )
                    if structured_kwargs.get("extra_body") != fallback.get("extra_body"):
                        self._direct_artifact_controls_unsupported = True
                    return self._attach_parsed_response_format(resp, response_format)
                except BaseException as fallback_exc:
                    if (
                        index >= len(fallbacks) - 1
                        or not self._is_parameter_compat_error(fallback_exc)
                    ):
                        raise
            raise

    async def _create_stream(self, kwargs: dict):
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
        finish_reason = str(getattr(resp.choices[0], "finish_reason", "") or "") or None
        reasoning_tokens = 0
        usage = None
        if resp.usage:
            completion_details = getattr(
                resp.usage,
                "completion_tokens_details",
                None,
            )
            reasoning_tokens = int(
                getattr(completion_details, "reasoning_tokens", 0) or 0
            )
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
                reasoning_tokens=reasoning_tokens,
                finish_reason=finish_reason,
                output_chars=len(content),
            )
        return LLMResponse(content=content, usage=usage, raw=resp,
                           finish_reason=finish_reason, reasoning_tokens=reasoning_tokens)

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
            stream = await self._create_stream(kwargs)
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
