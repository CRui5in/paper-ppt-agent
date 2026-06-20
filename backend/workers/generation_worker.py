"""Isolated generation worker.

The API process launches this module in a separate Python process for normal
provider-backed generation. Progress is streamed back as JSON Lines on stdout;
all diagnostics go to stderr so the parent can keep parsing stdout safely.
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import fields
import json
import logging
import sys
from pathlib import Path
from typing import Any

from backend.config import settings
from backend.runtime.offload import init_offload, shutdown_offload


logger = logging.getLogger(__name__)


def _emit(message: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(message, ensure_ascii=False, default=str) + "\n")
    sys.stdout.flush()


def _event_payload(event: Any) -> dict[str, Any]:
    return {
        "stage": event.stage,
        "status": event.status,
        "message": event.message,
        "progress": event.progress,
        "data": event.data,
    }


def _request_from_payload(payload: dict[str, Any]) -> Any:
    from backend.api.schemas import ResearchConfig
    from backend.orchestrator.pipeline import GenerationRequest
    from backend.parser.source_model import Source

    data = dict(payload["request"])
    data["file_path"] = Path(data["file_path"])
    if isinstance(data.get("research_config"), dict):
        data["research_config"] = ResearchConfig(**data["research_config"])
    if isinstance(data.get("sources"), list):
        data["sources"] = [
            Source.from_dict(item) if isinstance(item, dict) else item
            for item in data["sources"]
        ]
    allowed = {field.name for field in fields(GenerationRequest)}
    ignored = sorted(set(data) - allowed)
    if ignored:
        logger.info("Ignoring deprecated/unknown generation request fields: %s", ", ".join(ignored))
    return GenerationRequest(**{key: value for key, value in data.items() if key in allowed})


async def _run(request_path: Path) -> int:
    from backend.orchestrator.pipeline import ProgressEvent, run_pipeline
    from backend.session.progress import describe_exception
    from backend.usage.tracker import set_usage_context

    payload = json.loads(request_path.read_text(encoding="utf-8"))
    job_id = str(payload["job_id"])
    request = _request_from_payload(payload)
    set_usage_context(job_id=job_id)

    try:
        async for event in run_pipeline(request):
            _emit({"type": "event", "event": _event_payload(event)})
        _emit({"type": "done"})
        return 0
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.exception("generation worker failed for job %s", job_id)
        _emit(
            {
                "type": "event",
                "event": _event_payload(
                    ProgressEvent(
                        "error",
                        "error",
                        describe_exception(exc, "Generation failed"),
                        0.0,
                    )
                ),
            }
        )
        return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--request", required=True, type=Path)
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s:%(name)s:%(message)s",
        stream=sys.stderr,
    )
    init_offload(settings.io_pool_workers)
    try:
        return asyncio.run(_run(args.request))
    finally:
        shutdown_offload(wait=True)


if __name__ == "__main__":
    raise SystemExit(main())
