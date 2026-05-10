"""Token usage tracker.

Central singleton that the LLM layer calls into on every completion.
Persists records to ``.runtime/usage.jsonl`` (append-only, one JSON per line)
and maintains in-memory aggregations for the logs page.
"""

from __future__ import annotations

import asyncio
import contextvars
import json
import threading
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from backend.config import settings


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _today_key() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


@dataclass
class UsageRecord:
    """A single LLM-call record."""

    ts: str                  # ISO8601 UTC timestamp
    day: str                 # YYYY-MM-DD
    provider: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    job_id: str | None = None
    stage: str | None = None  # e.g. "research", "strategy", "generation", "repair"
    page: int | None = None
    attempt: int = 1          # 1 = initial, 2+ = repair / retry
    duration_ms: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class UsageSummary:
    total_calls: int = 0
    total_prompt: int = 0
    total_completion: int = 0
    total_tokens: int = 0


@dataclass
class UsageEvent:
    """Wrapper emitted to realtime subscribers."""

    type: str                 # "usage"
    record: UsageRecord


class _UsageTracker:
    """In-process usage log. Thread-safe; async-subscriber friendly."""

    def __init__(self) -> None:
        self._path: Path = settings.runtime_dir / "usage.jsonl"
        self._lock = threading.Lock()
        self._records: list[UsageRecord] = []
        self._subscribers: list[asyncio.Queue[UsageEvent]] = []
        self._loaded = False

    # ── persistence ─────────────────────────────────────────────────────

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        self._path.parent.mkdir(parents=True, exist_ok=True)
        if not self._path.exists():
            return
        try:
            with self._path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        data = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    # Drop legacy fields (e.g. cost_usd) so old logs still load.
                    data.pop("cost_usd", None)
                    try:
                        self._records.append(UsageRecord(**data))
                    except TypeError:
                        continue
        except OSError:
            return

    def _persist(self, record: UsageRecord) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record.to_dict(), ensure_ascii=False) + "\n")
        except OSError:
            # Persistence is best-effort — never block an LLM call on disk IO.
            pass

    # ── public API ──────────────────────────────────────────────────────

    def record(
        self,
        *,
        provider: str,
        model: str,
        prompt_tokens: int,
        completion_tokens: int,
        job_id: str | None = None,
        stage: str | None = None,
        page: int | None = None,
        attempt: int = 1,
        duration_ms: int = 0,
    ) -> UsageRecord:
        with self._lock:
            self._ensure_loaded()
            total = prompt_tokens + completion_tokens
            rec = UsageRecord(
                ts=_now_iso(),
                day=_today_key(),
                provider=provider,
                model=model,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=total,
                job_id=job_id,
                stage=stage,
                page=page,
                attempt=attempt,
                duration_ms=duration_ms,
            )
            self._records.append(rec)
            self._persist(rec)

        # Fan out to subscribers outside the lock.
        self._broadcast(rec)
        return rec

    # ── subscriptions for realtime UI ───────────────────────────────────

    def subscribe(self) -> asyncio.Queue[UsageEvent]:
        queue: asyncio.Queue[UsageEvent] = asyncio.Queue(maxsize=256)
        with self._lock:
            self._subscribers.append(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[UsageEvent]) -> None:
        with self._lock:
            self._subscribers = [q for q in self._subscribers if q is not queue]

    def _broadcast(self, record: UsageRecord) -> None:
        event = UsageEvent(type="usage", record=record)
        with self._lock:
            subs = list(self._subscribers)
        for q in subs:
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                # Drop the oldest so realtime views stay lively.
                try:
                    q.get_nowait()
                    q.put_nowait(event)
                except Exception:
                    pass

    # ── queries / aggregations ──────────────────────────────────────────

    def all_records(
        self,
        *,
        job_id: str | None = None,
        day: str | None = None,
        limit: int | None = None,
    ) -> list[UsageRecord]:
        with self._lock:
            self._ensure_loaded()
            items = list(self._records)
        if job_id is not None:
            items = [r for r in items if r.job_id == job_id]
        if day is not None:
            items = [r for r in items if r.day == day]
        items.sort(key=lambda r: r.ts, reverse=True)
        if limit is not None:
            items = items[:limit]
        return items

    def summary(
        self,
        records: list[UsageRecord] | None = None,
    ) -> UsageSummary:
        items = records if records is not None else self.all_records()
        s = UsageSummary()
        for r in items:
            s.total_calls += 1
            s.total_prompt += r.prompt_tokens
            s.total_completion += r.completion_tokens
            s.total_tokens += r.total_tokens
        return s

    def group_by(
        self,
        key_fn: Callable[[UsageRecord], str],
        *,
        records: list[UsageRecord] | None = None,
    ) -> dict[str, UsageSummary]:
        items = records if records is not None else self.all_records()
        buckets: dict[str, list[UsageRecord]] = {}
        for r in items:
            buckets.setdefault(key_fn(r), []).append(r)
        return {k: self.summary(v) for k, v in buckets.items()}

    def daily_series(self, *, days: int = 30) -> list[dict[str, Any]]:
        """Return up to `days` daily rollups, most recent first."""
        by_day = self.group_by(lambda r: r.day)
        rows: list[dict[str, Any]] = []
        for day, s in by_day.items():
            rows.append({
                "day": day,
                "calls": s.total_calls,
                "prompt_tokens": s.total_prompt,
                "completion_tokens": s.total_completion,
                "total_tokens": s.total_tokens,
            })
        rows.sort(key=lambda row: row["day"], reverse=True)
        return rows[:days]

    def per_model(self) -> list[dict[str, Any]]:
        by_model = self.group_by(lambda r: r.model)
        rows: list[dict[str, Any]] = []
        for model, s in by_model.items():
            rows.append({
                "model": model,
                "calls": s.total_calls,
                "prompt_tokens": s.total_prompt,
                "completion_tokens": s.total_completion,
                "total_tokens": s.total_tokens,
            })
        rows.sort(key=lambda row: row["total_tokens"], reverse=True)
        return rows

    def per_job(self, *, limit: int = 50) -> list[dict[str, Any]]:
        by_job = self.group_by(
            lambda r: r.job_id or "(unassigned)",
        )
        rows: list[dict[str, Any]] = []
        for job_id, s in by_job.items():
            rows.append({
                "job_id": job_id,
                "calls": s.total_calls,
                "prompt_tokens": s.total_prompt,
                "completion_tokens": s.total_completion,
                "total_tokens": s.total_tokens,
            })
        rows.sort(key=lambda row: row["total_tokens"], reverse=True)
        return rows[:limit]

    def per_stage(self) -> list[dict[str, Any]]:
        by_stage = self.group_by(lambda r: r.stage or "(unknown)")
        rows: list[dict[str, Any]] = []
        for stage, s in by_stage.items():
            rows.append({
                "stage": stage,
                "calls": s.total_calls,
                "prompt_tokens": s.total_prompt,
                "completion_tokens": s.total_completion,
                "total_tokens": s.total_tokens,
            })
        rows.sort(key=lambda row: row["total_tokens"], reverse=True)
        return rows


# Global singleton — import and use directly.
usage_tracker = _UsageTracker()


# ── Context helpers for async code ──────────────────────────────────────


_DEFAULT_CTX: dict[str, Any] = {
    "job_id": None,
    "stage": None,
    "page": None,
    "attempt": 1,
}
_ctx_var: contextvars.ContextVar[dict[str, Any]] = contextvars.ContextVar(
    "usage_context",
    default=_DEFAULT_CTX,
)
_UNSET = object()


def set_usage_context(
    *,
    job_id: str | None | object = _UNSET,
    stage: str | None | object = _UNSET,
    page: int | None | object = _UNSET,
    attempt: int | None | object = _UNSET,
) -> dict[str, Any]:
    """Set tracker context for subsequent LLM calls in this asyncio context.

    Returns a snapshot of the previous context so callers can restore it.
    Pass ``None`` explicitly to clear nullable fields such as ``page``.
    """
    prev = current_usage_context()
    next_ctx = dict(prev)
    if job_id is not _UNSET:
        next_ctx["job_id"] = job_id
    if stage is not _UNSET:
        next_ctx["stage"] = stage
    if page is not _UNSET:
        next_ctx["page"] = page
    if attempt is not _UNSET:
        next_ctx["attempt"] = attempt if attempt is not None else 1
    _ctx_var.set(next_ctx)
    return prev


def reset_usage_context(snapshot: dict[str, Any]) -> None:
    _ctx_var.set(dict(snapshot))


def current_usage_context() -> dict[str, Any]:
    return dict(_ctx_var.get())
