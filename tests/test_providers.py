from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest

from backend.llm import registry as registry_module
from backend.llm.provider_openai import OpenAIProvider, normalize_openai_base_url
from backend.llm.types import ModelInfo, ProviderInfo


def test_providers_endpoint_lists_four_backends(client, monkeypatch):
    monkeypatch.setattr(
        "backend.api.endpoints.providers.list_providers",
        lambda: [
            ProviderInfo(name="openai", display_name="OpenAI", models=[ModelInfo(id="gpt-4o", display_name="GPT-4o")]),
            ProviderInfo(
                name="deepseek",
                display_name="DeepSeek",
                default_base_url="https://api.deepseek.com",
                models=[ModelInfo(id="deepseek-v4-flash", display_name="DeepSeek V4 Flash")],
            ),
            ProviderInfo(name="anthropic", display_name="Anthropic", models=[ModelInfo(id="claude-sonnet", display_name="Claude Sonnet")]),
            ProviderInfo(name="gemini", display_name="Gemini", models=[ModelInfo(id="gemini-2.5-flash", display_name="Gemini Flash")]),
        ],
    )

    response = client.get("/api/providers")

    assert response.status_code == 200
    payload = response.json()
    names = {provider["name"] for provider in payload["providers"]}
    assert names == {"openai", "deepseek", "anthropic", "gemini"}
    deepseek = next(provider for provider in payload["providers"] if provider["name"] == "deepseek")
    assert deepseek["default_base_url"] == "https://api.deepseek.com"


def test_create_provider_defaults_deepseek_base_url(monkeypatch):
    captured: dict[str, str | None] = {}

    class FakeProvider:
        def __init__(
            self,
            api_key: str,
            base_url: str | None = None,
            provider_name: str = "openai",
        ) -> None:
            captured["api_key"] = api_key
            captured["base_url"] = base_url
            captured["provider_name"] = provider_name

    monkeypatch.setattr(registry_module, "_load_provider_class", lambda name: FakeProvider)

    provider = registry_module.create_provider("deepseek", "sk-test")

    assert isinstance(provider, FakeProvider)
    assert captured == {
        "api_key": "sk-test",
        "base_url": "https://api.deepseek.com",
        "provider_name": "deepseek",
    }


def test_openai_base_url_accepts_full_chat_completions_endpoint():
    assert (
        normalize_openai_base_url("https://proxy.example.com/v1/chat/completions")
        == "https://proxy.example.com/v1"
    )
    assert (
        normalize_openai_base_url("https://proxy.example.com/openai/v1/chat/completions/")
        == "https://proxy.example.com/openai/v1"
    )
    assert (
        normalize_openai_base_url("https://proxy.example.com/v1")
        == "https://proxy.example.com/v1"
    )


def test_openai_default_models_use_gpt55_not_gpt53():
    registry_openai = next(
        provider for provider in registry_module.list_providers() if provider.name == "openai"
    )
    provider = object.__new__(OpenAIProvider)
    provider._provider_name = "openai"

    registry_models = [model.id for model in registry_openai.models]
    runtime_models = [model.id for model in provider.get_provider_info().models]

    assert registry_models == ["gpt-5.5", "gpt-5.4"]
    assert runtime_models == ["gpt-5.5", "gpt-5.4"]
    assert "gpt-5.3" not in registry_models
    assert "gpt-5.3" not in runtime_models


@pytest.mark.asyncio
async def test_openai_provider_adds_deepseek_reasoning_kwargs(monkeypatch):
    captured: dict[str, object] = {}

    async def fake_create(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))],
            usage=SimpleNamespace(prompt_tokens=11, completion_tokens=7),
        )

    class FakeAsyncOpenAI:
        def __init__(self, api_key: str, base_url: str | None = None) -> None:
            self.chat = SimpleNamespace(
                completions=SimpleNamespace(create=fake_create),
            )
            self.beta = SimpleNamespace(
                chat=SimpleNamespace(
                    completions=SimpleNamespace(parse=fake_create),
                ),
            )
            self.models = SimpleNamespace(list=lambda: fake_create())

    async def passthrough_retry(func):
        return await func()

    monkeypatch.setattr("backend.llm.provider_openai.AsyncOpenAI", FakeAsyncOpenAI)
    monkeypatch.setattr("backend.llm.provider_openai.call_with_retry", passthrough_retry)

    provider = OpenAIProvider(
        api_key="sk-test",
        base_url="https://api.deepseek.com",
        provider_name="deepseek",
    )
    response = await provider.chat(
        messages=[],
        model="deepseek-v4-pro",
        max_tokens=16384,
    )

    assert response.content == "ok"
    assert captured["reasoning_effort"] == "max"
    assert captured["extra_body"] == {"thinking": {"type": "enabled"}}
    assert captured["max_tokens"] == 16384


@pytest.mark.asyncio
async def test_official_openai_uses_max_completion_tokens_and_gpt5_settings(monkeypatch):
    captured: dict[str, object] = {}

    async def fake_create(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))],
            usage=None,
        )

    class FakeAsyncOpenAI:
        def __init__(self, api_key: str, base_url: str | None = None) -> None:
            captured["base_url"] = base_url
            self.chat = SimpleNamespace(
                completions=SimpleNamespace(create=fake_create),
            )
            self.beta = SimpleNamespace(
                chat=SimpleNamespace(
                    completions=SimpleNamespace(parse=fake_create),
                ),
            )
            self.models = SimpleNamespace(list=lambda: fake_create())

    async def passthrough_retry(func):
        return await func()

    monkeypatch.setattr("backend.llm.provider_openai.AsyncOpenAI", FakeAsyncOpenAI)
    monkeypatch.setattr("backend.llm.provider_openai.call_with_retry", passthrough_retry)

    provider = OpenAIProvider(
        api_key="sk-test",
        provider_name="openai",
        openai_settings={
            "reasoning_effort": "high",
            "verbosity": "medium",
        },
    )
    response = await provider.chat(
        messages=[],
        model="gpt-5.5",
        temperature=0.2,
        max_tokens=4096,
    )

    assert response.content == "ok"
    assert captured["base_url"] is None
    assert captured["max_completion_tokens"] == 4096
    assert "max_tokens" not in captured
    assert "temperature" not in captured
    assert captured["reasoning_effort"] == "high"
    assert captured["verbosity"] == "medium"


@pytest.mark.asyncio
async def test_official_openai_falls_back_to_max_tokens(monkeypatch):
    captured_calls: list[dict[str, object]] = []

    async def fake_create(**kwargs):
        captured_calls.append(dict(kwargs))
        if "max_completion_tokens" in kwargs:
            raise TypeError("unexpected keyword argument 'max_completion_tokens'")
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))],
            usage=None,
        )

    class FakeAsyncOpenAI:
        def __init__(self, api_key: str, base_url: str | None = None) -> None:
            self.chat = SimpleNamespace(
                completions=SimpleNamespace(create=fake_create),
            )
            self.beta = SimpleNamespace(
                chat=SimpleNamespace(
                    completions=SimpleNamespace(parse=fake_create),
                ),
            )
            self.models = SimpleNamespace(list=lambda: fake_create())

    async def passthrough_retry(func):
        return await func()

    monkeypatch.setattr("backend.llm.provider_openai.AsyncOpenAI", FakeAsyncOpenAI)
    monkeypatch.setattr("backend.llm.provider_openai.call_with_retry", passthrough_retry)

    provider = OpenAIProvider(api_key="sk-test", provider_name="openai")
    response = await provider.chat(messages=[], model="gpt-5.5", max_tokens=2048)

    assert response.content == "ok"
    assert captured_calls[0]["max_completion_tokens"] == 2048
    assert captured_calls[0]["reasoning_effort"] == "medium"
    assert captured_calls[0]["verbosity"] == "high"
    assert captured_calls[1]["max_completion_tokens"] == 2048
    assert "reasoning_effort" not in captured_calls[1]
    assert "verbosity" not in captured_calls[1]
    assert captured_calls[2]["max_tokens"] == 2048
    assert "max_completion_tokens" not in captured_calls[2]
    assert "reasoning_effort" not in captured_calls[2]
    assert "verbosity" not in captured_calls[2]


@pytest.mark.asyncio
async def test_openai_provider_respects_configured_deepseek_thinking(monkeypatch):
    captured: dict[str, object] = {}

    async def fake_create(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))],
            usage=None,
        )

    class FakeAsyncOpenAI:
        def __init__(self, api_key: str, base_url: str | None = None) -> None:
            self.chat = SimpleNamespace(
                completions=SimpleNamespace(create=fake_create),
            )
            self.beta = SimpleNamespace(
                chat=SimpleNamespace(
                    completions=SimpleNamespace(parse=fake_create),
                ),
            )
            self.models = SimpleNamespace(list=lambda: fake_create())

    async def passthrough_retry(func):
        return await func()

    monkeypatch.setattr("backend.llm.provider_openai.AsyncOpenAI", FakeAsyncOpenAI)
    monkeypatch.setattr("backend.llm.provider_openai.call_with_retry", passthrough_retry)

    provider = OpenAIProvider(
        api_key="sk-test",
        base_url="https://api.deepseek.com",
        provider_name="deepseek",
        deepseek_settings={
            "thinking_enabled": False,
            "reasoning_effort": "high",
        },
    )
    response = await provider.chat(
        messages=[],
        model="deepseek-v4-pro",
        temperature=0.2,
    )

    assert response.content == "ok"
    assert captured["extra_body"] == {"thinking": {"type": "disabled"}}
    assert "reasoning_effort" not in captured
    assert captured["temperature"] == 0.2


@pytest.mark.asyncio
async def test_custom_openai_proxy_uses_sdk_when_compatible(monkeypatch):
    captured: dict[str, object] = {}

    async def fake_create(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="sdk ok"))],
            usage=None,
        )

    class FakeAsyncOpenAI:
        def __init__(self, api_key: str, base_url: str | None = None) -> None:
            captured["base_url"] = base_url
            self.chat = SimpleNamespace(
                completions=SimpleNamespace(create=fake_create),
            )
            self.beta = SimpleNamespace(
                chat=SimpleNamespace(
                    completions=SimpleNamespace(parse=fake_create),
                ),
            )
            self.models = SimpleNamespace(list=lambda: fake_create())

    async def forbidden_raw_completion(self, kwargs):
        raise AssertionError("raw fallback should not be used")

    async def passthrough_retry(func):
        return await func()

    monkeypatch.setattr("backend.llm.provider_openai.AsyncOpenAI", FakeAsyncOpenAI)
    monkeypatch.setattr("backend.llm.provider_openai.call_with_retry", passthrough_retry)
    monkeypatch.setattr(
        "backend.llm.provider_openai.OpenAIProvider._create_raw_chat_completion",
        forbidden_raw_completion,
    )

    provider = OpenAIProvider(
        api_key="sk-test",
        base_url="https://proxy.example.com/v1",
        provider_name="openai",
    )
    response = await provider.chat(messages=[], model="gpt-5.5", max_tokens=2048)

    assert response.content == "sdk ok"
    assert captured["base_url"] == "https://proxy.example.com/v1"
    assert captured["max_tokens"] == 2048


@pytest.mark.asyncio
async def test_custom_openai_proxy_falls_back_to_raw_when_sdk_is_blocked(monkeypatch):
    captured_posts: list[dict[str, object]] = []

    class BlockedRequestError(Exception):
        status_code = 403

    async def blocked_create(**kwargs):
        raise BlockedRequestError("Your request was blocked.")

    class FakeAsyncOpenAI:
        def __init__(self, api_key: str, base_url: str | None = None) -> None:
            self.chat = SimpleNamespace(
                completions=SimpleNamespace(create=blocked_create),
            )
            self.beta = SimpleNamespace(
                chat=SimpleNamespace(
                    completions=SimpleNamespace(parse=blocked_create),
                ),
            )
            self.models = SimpleNamespace(list=lambda: blocked_create())

    class FakeResponse:
        def __init__(self, payload: dict) -> None:
            self.status_code = 200
            self.text = "{}"
            self._payload = payload

        def json(self):
            if "max_tokens" in self._payload:
                return {
                    "choices": [{"message": {"role": "assistant"}}],
                    "usage": {"prompt_tokens": 5, "completion_tokens": 1},
                }
            return {
                "choices": [
                    {"message": {"role": "assistant", "content": "raw ok"}}
                ],
                "usage": {"prompt_tokens": 5, "completion_tokens": 2},
            }

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

        async def post(self, url: str, *, headers: dict, json: dict):
            captured_posts.append({"url": url, "headers": headers, "json": json})
            return FakeResponse(json)

    async def passthrough_retry(func):
        return await func()

    monkeypatch.setattr("backend.llm.provider_openai.AsyncOpenAI", FakeAsyncOpenAI)
    monkeypatch.setattr("backend.llm.provider_openai.httpx.AsyncClient", FakeAsyncClient)
    monkeypatch.setattr("backend.llm.provider_openai.call_with_retry", passthrough_retry)

    provider = OpenAIProvider(
        api_key="sk-test",
        base_url="https://proxy.example.com",
        provider_name="openai",
    )
    response = await provider.chat(messages=[], model="gpt-5.5", max_tokens=2048)

    assert response.content == "raw ok"
    assert response.usage is not None
    assert response.usage.prompt_tokens == 5
    assert response.usage.completion_tokens == 2
    assert captured_posts[0]["url"] == "https://proxy.example.com/v1/chat/completions"
    assert captured_posts[0]["json"]["max_tokens"] == 2048
    assert "max_tokens" not in captured_posts[1]["json"]
    assert captured_posts[1]["headers"]["Authorization"] == "Bearer sk-test"


@pytest.mark.asyncio
async def test_custom_openai_proxy_does_not_mask_v1_error_with_html_dashboard(monkeypatch):
    class BlockedRequestError(Exception):
        status_code = 403

    async def blocked_create(**kwargs):
        raise BlockedRequestError("Your request was blocked.")

    class FakeAsyncOpenAI:
        def __init__(self, api_key: str, base_url: str | None = None) -> None:
            self.chat = SimpleNamespace(
                completions=SimpleNamespace(create=blocked_create),
            )
            self.beta = SimpleNamespace(
                chat=SimpleNamespace(
                    completions=SimpleNamespace(parse=blocked_create),
                ),
            )
            self.models = SimpleNamespace(list=lambda: blocked_create())

    class FakeResponse:
        def __init__(self, url: str) -> None:
            self.status_code = 200
            self.text = "<!doctype html><html><title>Gateway UI</title></html>"
            self._url = url

        def json(self):
            if self._url.endswith("/v1/chat/completions"):
                return {"choices": [{"message": {"role": "assistant"}}]}
            raise json.JSONDecodeError("Expecting value", "", 0)

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

        async def post(self, url: str, *, headers: dict, json: dict):
            return FakeResponse(url)

    async def passthrough_retry(func):
        return await func()

    monkeypatch.setattr("backend.llm.provider_openai.AsyncOpenAI", FakeAsyncOpenAI)
    monkeypatch.setattr("backend.llm.provider_openai.httpx.AsyncClient", FakeAsyncClient)
    monkeypatch.setattr("backend.llm.provider_openai.call_with_retry", passthrough_retry)

    provider = OpenAIProvider(
        api_key="sk-test",
        base_url="https://proxy.example.com",
        provider_name="openai",
    )

    with pytest.raises(RuntimeError, match="no message content from https://proxy.example.com/v1/chat/completions"):
        await provider.chat(messages=[], model="gpt-5.5", max_tokens=2048)


@pytest.mark.asyncio
async def test_custom_openai_proxy_reports_raw_timeout(monkeypatch):
    class BlockedRequestError(Exception):
        status_code = 403

    async def blocked_create(**kwargs):
        raise BlockedRequestError("Your request was blocked.")

    class FakeAsyncOpenAI:
        def __init__(self, api_key: str, base_url: str | None = None) -> None:
            self.chat = SimpleNamespace(
                completions=SimpleNamespace(create=blocked_create),
            )
            self.beta = SimpleNamespace(
                chat=SimpleNamespace(
                    completions=SimpleNamespace(parse=blocked_create),
                ),
            )
            self.models = SimpleNamespace(list=lambda: blocked_create())

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

        async def post(self, url: str, *, headers: dict, json: dict):
            raise httpx.ReadTimeout("timed out")

    async def passthrough_retry(func):
        return await func()

    monkeypatch.setattr("backend.llm.provider_openai.AsyncOpenAI", FakeAsyncOpenAI)
    monkeypatch.setattr("backend.llm.provider_openai.httpx.AsyncClient", FakeAsyncClient)
    monkeypatch.setattr("backend.llm.provider_openai.call_with_retry", passthrough_retry)

    provider = OpenAIProvider(
        api_key="sk-test",
        base_url="https://proxy.example.com",
        provider_name="openai",
    )

    with pytest.raises(RuntimeError, match="timed out after 120s from https://proxy.example.com/v1/chat/completions"):
        await provider.chat(messages=[], model="gpt-5.5", max_tokens=2048)
