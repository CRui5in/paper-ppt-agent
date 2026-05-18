from __future__ import annotations

import json
from pathlib import Path

from backend.config import settings
from backend.generator import template_agent
from backend.generator.template_agent import TemplateAgentConfig, TemplateAgentJob, TemplateAgentManager


def test_template_agent_prompt_uses_current_workspace_snapshot():
    import_id = "agent_chat"
    import_root = settings.workspaces_dir / "template_imports" / import_id
    import_root.mkdir(parents=True, exist_ok=True)
    (import_root / "review.json").write_text(
        json.dumps(
            {
                "import_id": import_id,
                "template_id": "demo",
                "label": "Demo",
                "status": "review_required",
                "slide_count": 3,
                "page_types": ["cover", "content"],
                "asset_roles": [],
                "page_type_candidates": {},
                "slides": [],
                "assets": [],
                "draft": {},
                "conversation": [
                    {"role": "user", "content": "Please keep the logo."},
                    {"role": "assistant", "content": "I will preserve the logo and continue."},
                    {"role": "user", "content": "Also keep the footer."},
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    prompt = template_agent._build_prompt(import_id, import_root, "Add more contrast", "en")

    assert "Conversation handling" not in prompt
    assert "[user] Please keep the logo." not in prompt
    assert "[assistant] I will preserve the logo and continue." not in prompt
    assert "Add more contrast" in prompt


def test_template_agent_records_usage_delta(workspace_tmp: Path, monkeypatch):
    job = TemplateAgentJob(
        id="agent-usage-1",
        import_id="agent_chat",
        feedback="Do the thing",
        config=TemplateAgentConfig(mode="claude_code", model="claude-sonnet-4"),
    )
    job.session_id = "session-usage-1"
    job.model_name = "claude-sonnet-4"
    job.input_tokens = 1200
    job.output_tokens = 240
    job.cache_read_tokens = 80
    job.cache_creation_tokens = 20
    job.duration_ms = 3400
    snapshot = {
        "input_tokens": 1000,
        "output_tokens": 200,
        "cache_read_tokens": 40,
        "cache_creation_tokens": 10,
        "duration_ms": 3000,
    }

    calls: list[dict[str, object]] = []

    def fake_record(**kwargs):
        calls.append(kwargs)
        return None

    monkeypatch.setattr(template_agent.usage_tracker, "record", fake_record)

    TemplateAgentManager()._record_usage(job, snapshot, attempt=3)

    assert calls == [
        {
            "provider": "anthropic",
            "model": "claude-sonnet-4",
            "prompt_tokens": 250,
            "completion_tokens": 40,
            "job_id": "session-usage-1",
            "stage": "agent",
            "attempt": 3,
            "duration_ms": 400,
        }
    ]


def test_template_agent_session_state_round_trip(workspace_tmp: Path):
    import_id = "agent_chat"
    import_root = settings.workspaces_dir / "template_imports" / import_id
    import_root.mkdir(parents=True, exist_ok=True)

    manager = TemplateAgentManager()
    state = manager._load_session_state(import_id)
    state.session_id = "session-abc"
    state.initialized = True
    state.input_tokens = 111
    state.output_tokens = 222
    state.cache_read_tokens = 33
    state.cache_creation_tokens = 44
    state.total_cost_usd = 1.23
    state.num_turns = 5
    state.duration_ms = 9876
    state.model_name = "claude-sonnet-4"
    state.model_usage = {"claude-sonnet-4": {"inputTokens": 111}}

    job = TemplateAgentJob(
        id="job-1",
        import_id=import_id,
        feedback="Hello",
        config=TemplateAgentConfig(),
        session_state=state,
    )
    manager._seed_job_from_session_state(job)
    manager._save_session_state(job)

    reloaded = TemplateAgentManager()._load_session_state(import_id)
    assert reloaded.session_id == "session-abc"
    assert reloaded.initialized is True
    assert reloaded.input_tokens == 111
    assert reloaded.output_tokens == 222
    assert reloaded.cache_read_tokens == 33
    assert reloaded.cache_creation_tokens == 44
    assert reloaded.total_cost_usd == 1.23
    assert reloaded.num_turns == 5
    assert reloaded.duration_ms == 9876
    assert reloaded.model_name == "claude-sonnet-4"
    assert reloaded.model_usage == {"claude-sonnet-4": {"inputTokens": 111}}
