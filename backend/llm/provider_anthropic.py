"""Anthropic LLM provider using the native anthropic SDK."""

from __future__ import annotations

import base64
import time
from collections.abc import AsyncIterator

from anthropic import AsyncAnthropic
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


class AnthropicProvider(LLMProvider):
    """Anthropic provider wrapping AsyncAnthropic."""

    def __init__(self, api_key: str) -> None:
        self._client = AsyncAnthropic(api_key=api_key)

    def _split_system_and_messages(
        self, messages: list[LLMMessage]
    ) -> tuple[str | None, list[dict]]:
        """Split system message from conversation messages.

        Anthropic requires system prompt as a separate top-level parameter.
        """
        system_text = None
        api_messages = []

        for msg in messages:
            if msg.role == "system":
                if isinstance(msg.content, str):
                    system_text = msg.content
                continue

            if isinstance(msg.content, str):
                api_messages.append({"role": msg.role, "content": msg.content})
            else:
                parts = []
                for block in msg.content:
                    if block.type == "text" and block.text:
                        parts.append({"type": "text", "text": block.text})
                    elif block.type == "image" and block.image_data:
                        b64 = base64.b64encode(block.image_data).decode()
                        media = block.image_media_type or "image/png"
                        parts.append({
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": media,
                                "data": b64,
                            },
                        })
                api_messages.append({"role": msg.role, "content": parts})

        return system_text, api_messages

    async def chat(
        self,
        messages: list[LLMMessage],
        model: str,
        *,
        temperature: float = 0.7,
        max_tokens: int | None = None,
        response_format: type[BaseModel] | None = None,
    ) -> LLMResponse:
        system_text, api_messages = self._split_system_and_messages(messages)

        kwargs: dict = {
            "model": model,
            "messages": api_messages,
            "temperature": temperature,
            "max_tokens": max_tokens or 8192,
        }
        if system_text:
            kwargs["system"] = system_text

        t0 = time.monotonic()
        async with llm_request_slot():
            resp = await call_with_retry(lambda: self._client.messages.create(**kwargs))
        duration_ms = int((time.monotonic() - t0) * 1000)

        content = ""
        for block in resp.content:
            if block.type == "text":
                content += block.text

        usage = TokenUsage(
            prompt_tokens=resp.usage.input_tokens,
            completion_tokens=resp.usage.output_tokens,
        )
        ctx = current_usage_context()
        usage_tracker.record(
            provider="anthropic",
            model=model,
            prompt_tokens=resp.usage.input_tokens,
            completion_tokens=resp.usage.output_tokens,
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
        system_text, api_messages = self._split_system_and_messages(messages)

        kwargs: dict = {
            "model": model,
            "messages": api_messages,
            "temperature": temperature,
            "max_tokens": max_tokens or 8192,
        }
        if system_text:
            kwargs["system"] = system_text

        async with llm_request_slot():
            async with self._client.messages.stream(**kwargs) as stream:
                async for text in stream.text_stream:
                    yield LLMStreamChunk(delta=text)

    async def validate(self) -> bool:
        try:
            await self._client.messages.create(
                model="claude-sonnet-4.6",
                max_tokens=10,
                messages=[{"role": "user", "content": "hi"}],
            )
            return True
        except Exception:
            return False

    def get_provider_info(self) -> ProviderInfo:
        return ProviderInfo(
            name="anthropic",
            display_name="Anthropic",
            models=[
                ModelInfo(
                    id="claude-opus-4.6",
                    display_name="Claude Opus 4.6",
                    supports_vision=True,
                    supports_structured_output=True,
                    context_window=200000,
                ),
                ModelInfo(
                    id="claude-sonnet-4.6",
                    display_name="Claude Sonnet 4.6",
                    supports_vision=True,
                    supports_structured_output=True,
                    context_window=200000,
                ),
                ModelInfo(
                    id="claude-haiku-4.6",
                    display_name="Claude Haiku 4.6",
                    supports_vision=True,
                    supports_structured_output=True,
                    context_window=200000,
                ),
            ],
        )
