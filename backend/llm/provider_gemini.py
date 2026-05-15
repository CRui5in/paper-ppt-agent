"""Google Gemini LLM provider using the native google-genai SDK."""

from __future__ import annotations

import base64
import time
from collections.abc import AsyncIterator

from google import genai
from google.genai import types as genai_types
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


class GeminiProvider(LLMProvider):
    """Google Gemini provider wrapping google.genai.Client."""

    def __init__(self, api_key: str) -> None:
        self._client = genai.Client(api_key=api_key)

    def _convert_messages(
        self, messages: list[LLMMessage]
    ) -> tuple[str | None, list[genai_types.Content]]:
        """Convert to Gemini format, extracting system instruction."""
        system_text = None
        contents = []

        for msg in messages:
            if msg.role == "system":
                if isinstance(msg.content, str):
                    system_text = msg.content
                continue

            role = "user" if msg.role == "user" else "model"

            if isinstance(msg.content, str):
                contents.append(
                    genai_types.Content(
                        role=role,
                        parts=[genai_types.Part(text=msg.content)],
                    )
                )
            else:
                parts = []
                for block in msg.content:
                    if block.type == "text" and block.text:
                        parts.append(genai_types.Part(text=block.text))
                    elif block.type == "image" and block.image_data:
                        media = block.image_media_type or "image/png"
                        parts.append(
                            genai_types.Part(
                                inline_data=genai_types.Blob(
                                    mime_type=media,
                                    data=block.image_data,
                                )
                            )
                        )
                contents.append(genai_types.Content(role=role, parts=parts))

        return system_text, contents

    async def chat(
        self,
        messages: list[LLMMessage],
        model: str,
        *,
        temperature: float = 0.7,
        max_tokens: int | None = None,
        response_format: type[BaseModel] | None = None,
    ) -> LLMResponse:
        system_text, contents = self._convert_messages(messages)

        config = genai_types.GenerateContentConfig(
            temperature=temperature,
        )
        if max_tokens:
            config.max_output_tokens = max_tokens
        if system_text:
            config.system_instruction = system_text

        t0 = time.monotonic()
        async with llm_request_slot():
            resp = await call_with_retry(
                lambda: self._client.aio.models.generate_content(
                    model=model,
                    contents=contents,
                    config=config,
                )
            )
        duration_ms = int((time.monotonic() - t0) * 1000)

        content = resp.text or ""
        usage = None
        if resp.usage_metadata:
            prompt_tokens = resp.usage_metadata.prompt_token_count or 0
            completion_tokens = resp.usage_metadata.candidates_token_count or 0
            usage = TokenUsage(
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
            )
            ctx = current_usage_context()
            usage_tracker.record(
                provider="gemini",
                model=model,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
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
        system_text, contents = self._convert_messages(messages)

        config = genai_types.GenerateContentConfig(
            temperature=temperature,
        )
        if max_tokens:
            config.max_output_tokens = max_tokens
        if system_text:
            config.system_instruction = system_text

        async with llm_request_slot():
            async for chunk in await self._client.aio.models.generate_content_stream(
                model=model,
                contents=contents,
                config=config,
            ):
                if chunk.text:
                    yield LLMStreamChunk(delta=chunk.text)

    async def validate(self) -> bool:
        try:
            resp = await self._client.aio.models.generate_content(
                model="gemini-3.1-flash-preview",
                contents="hi",
                config=genai_types.GenerateContentConfig(max_output_tokens=10),
            )
            return bool(resp.text)
        except Exception:
            return False

    def get_provider_info(self) -> ProviderInfo:
        return ProviderInfo(
            name="gemini",
            display_name="Google Gemini",
            models=[
                ModelInfo(
                    id="gemini-3.1-pro-preview",
                    display_name="Gemini 3.1 Pro Preview",
                    supports_vision=True,
                    supports_structured_output=True,
                    context_window=1048576,
                ),
                ModelInfo(
                    id="gemini-3.1-flash-preview",
                    display_name="Gemini 3.1 Flash Preview",
                    supports_vision=True,
                    supports_structured_output=True,
                    context_window=1048576,
                ),
            ],
        )
