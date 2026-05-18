"""Structured logging events + slow-step thresholds for template import.

Event names are namespaced under ``template_import.*`` (see
:data:`EVENTS`). Slow-step thresholds: ``rendering=30s``,
``detecting_assets=15s``, ``llm_review=30s``, ``total_pipeline=90s``;
crossing 5× the threshold emits ``template_import.slow_step``.

Each function performs exactly one ``logger`` call with structured
``extra={'event': ..., 'import_id': ..., 'step_id': ..., ...}`` so a
JSON / OpenTelemetry handler can pick the fields up without parsing the
human-readable message.
"""

from __future__ import annotations

import logging
import math
import time
from contextlib import contextmanager
from typing import Iterator


logger = logging.getLogger("template_import")


# Structured event names emitted by this module / the pipeline.
EVENTS: tuple[str, ...] = (
    "template_import.upload.received",
    "template_import.step.started",
    "template_import.step.completed",
    "template_import.step.failed",
    "template_import.slow_step",
    "template_import.llm.call",
    "template_import.llm.retry",
    "template_import.llm.placeholder_rejected",
    "template_import.confirm.tx_begin",
    "template_import.confirm.tx_commit",
    "template_import.confirm.tx_rollback",
)


# Per-step soft-deadline thresholds (ms). Crossing 5× the threshold emits a
# ``template_import.slow_step`` warning event. Steps not present here have
# no soft deadline (treated as ``+inf``).
STEP_THRESHOLDS_MS: dict[str, int] = {
    "rendering": 30_000,
    "detecting_assets": 15_000,
    "llm_review": 30_000,
    "total_pipeline": 90_000,
}


def step_started(import_id: str, step_id: str) -> None:
    """Emit ``template_import.step.started`` for ``(import_id, step_id)``."""
    logger.info(
        "step started: %s/%s",
        import_id,
        step_id,
        extra={
            "event": "template_import.step.started",
            "import_id": import_id,
            "step_id": step_id,
        },
    )


def step_succeeded(import_id: str, step_id: str, *, duration_ms: int) -> None:
    """Emit ``template_import.step.completed`` with measured duration."""
    logger.info(
        "step completed: %s/%s in %dms",
        import_id,
        step_id,
        duration_ms,
        extra={
            "event": "template_import.step.completed",
            "import_id": import_id,
            "step_id": step_id,
            "duration_ms": int(duration_ms),
        },
    )


def step_failed(
    import_id: str,
    step_id: str,
    *,
    error_kind: str,
    reason: str,
    duration_ms: int,
) -> None:
    """Emit ``template_import.step.failed`` with classified error_kind."""
    logger.error(
        "step failed: %s/%s [%s] %s (%dms)",
        import_id,
        step_id,
        error_kind,
        reason,
        duration_ms,
        extra={
            "event": "template_import.step.failed",
            "import_id": import_id,
            "step_id": step_id,
            "error_kind": error_kind,
            "reason": reason,
            "duration_ms": int(duration_ms),
        },
    )


def slow_step_warning(
    import_id: str,
    step_id: str,
    *,
    duration_ms: int,
    threshold_ms: int,
) -> None:
    """Emit ``template_import.slow_step`` when a step exceeds 5× threshold."""
    logger.warning(
        "slow step: %s/%s took %dms (threshold %dms)",
        import_id,
        step_id,
        duration_ms,
        threshold_ms,
        extra={
            "event": "template_import.slow_step",
            "import_id": import_id,
            "step_id": step_id,
            "duration_ms": int(duration_ms),
            "threshold_ms": int(threshold_ms),
        },
    )


def check_slow_step(import_id: str, step_id: str, duration_ms: int) -> None:
    """Emit a ``slow_step`` warning when ``duration_ms`` exceeds 5× threshold.

    Steps not registered in :data:`STEP_THRESHOLDS_MS` have an effective
    threshold of ``+inf`` and never trigger.
    """
    threshold = STEP_THRESHOLDS_MS.get(step_id, math.inf)
    if threshold is math.inf:
        return
    if duration_ms > 5 * threshold:
        slow_step_warning(
            import_id,
            step_id,
            duration_ms=int(duration_ms),
            threshold_ms=int(threshold),
        )


@contextmanager
def step_timing(import_id: str, step_id: str) -> Iterator[None]:
    """Time a pipeline step and emit the matching structured events.

    On enter: emits ``template_import.step.started``.
    On clean exit: emits ``template_import.step.completed`` and
    :func:`check_slow_step`.
    On exception: emits ``template_import.step.failed`` (re-raising), with
    ``error_kind`` extracted from the exception's ``error_kind`` attribute
    when present, falling back to ``"unknown"``. Slow-step check still
    runs against the elapsed duration so a hung-then-failed step is still
    visible.
    """
    step_started(import_id, step_id)
    started = time.perf_counter()
    try:
        yield
    except BaseException as exc:  # noqa: BLE001 — re-raised below
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        error_kind = getattr(exc, "error_kind", None) or "unknown"
        reason = str(exc) or exc.__class__.__name__
        step_failed(
            import_id,
            step_id,
            error_kind=str(error_kind),
            reason=reason,
            duration_ms=elapsed_ms,
        )
        check_slow_step(import_id, step_id, elapsed_ms)
        raise
    else:
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        step_succeeded(import_id, step_id, duration_ms=elapsed_ms)
        check_slow_step(import_id, step_id, elapsed_ms)
