"""Abstract base class for LLM providers."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING

from pydantic import BaseModel

from .types import LLMMessage, LLMResponse, LLMStreamChunk, ProviderInfo

if TYPE_CHECKING:
    pass


class LLMProvider(ABC):
    """Abstract interface for LLM providers.

    Each provider wraps its native SDK to provide a uniform interface
    while preserving provider-specific capabilities.
    """

    def artifact_max_tokens(self, model: str, visible_tokens: int) -> int | None:
        """Translate a visible artifact budget into the provider's total limit."""
        return visible_tokens

    @abstractmethod
    async def chat(
        self,
        messages: list[LLMMessage],
        model: str,
        *,
        temperature: float = 0.7,
        max_tokens: int | None = None,
        response_format: type[BaseModel] | None = None,
    ) -> LLMResponse:
        """Send a chat completion request and return the full response."""
        ...

    @abstractmethod
    async def chat_stream(
        self,
        messages: list[LLMMessage],
        model: str,
        *,
        temperature: float = 0.7,
        max_tokens: int | None = None,
    ) -> AsyncIterator[LLMStreamChunk]:
        """Send a chat completion request and stream the response."""
        ...

    @abstractmethod
    async def validate(self) -> bool:
        """Validate that the provider is properly configured (e.g. API key works)."""
        ...

    @abstractmethod
    def get_provider_info(self) -> ProviderInfo:
        """Return information about this provider and its available models."""
        ...
