"""Shared types for the LLM abstraction layer."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


@dataclass
class ContentBlock:
    """A single content block within a message."""

    type: Literal["text", "image"]
    text: str | None = None
    image_data: bytes | None = None
    image_media_type: str | None = None  # e.g. "image/png"


@dataclass
class LLMMessage:
    """A single message in a conversation."""

    role: Literal["system", "user", "assistant"]
    content: str | list[ContentBlock]

    @staticmethod
    def system(text: str) -> LLMMessage:
        return LLMMessage(role="system", content=text)

    @staticmethod
    def user(text: str) -> LLMMessage:
        return LLMMessage(role="user", content=text)

    @staticmethod
    def assistant(text: str) -> LLMMessage:
        return LLMMessage(role="assistant", content=text)

    @staticmethod
    def user_with_image(
        text: str, image_data: bytes, media_type: str = "image/png"
    ) -> LLMMessage:
        return LLMMessage(
            role="user",
            content=[
                ContentBlock(type="text", text=text),
                ContentBlock(
                    type="image",
                    image_data=image_data,
                    image_media_type=media_type,
                ),
            ],
        )


@dataclass
class TokenUsage:
    """Token usage statistics for a response."""

    prompt_tokens: int = 0
    completion_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


@dataclass
class LLMResponse:
    """Response from an LLM provider."""

    content: str
    usage: TokenUsage | None = None
    raw: Any = None  # Provider-specific raw response
    finish_reason: str | None = None
    reasoning_tokens: int = 0


@dataclass
class LLMStreamChunk:
    """A single chunk from a streaming response."""

    delta: str  # Incremental text content
    finish_reason: str | None = None


@dataclass
class ModelInfo:
    """Information about an available model."""

    id: str
    display_name: str
    supports_vision: bool = False
    supports_structured_output: bool = False
    context_window: int | None = None


@dataclass
class ProviderInfo:
    """Information about an LLM provider."""

    name: str
    display_name: str
    default_base_url: str | None = None
    models: list[ModelInfo] = field(default_factory=list)
