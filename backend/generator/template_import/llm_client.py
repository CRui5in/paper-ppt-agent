"""LLM client for template-import assistance with caching + structured retry.

Hashes a canonicalized payload to avoid re-issuing equivalent calls,
performs one structured-repair retry on JSON / schema failure, and
appends an :class:`LLMTraceEntry` per call. ``feedback_history`` and
``conversation`` are bounded to the most recent ≥10 entries.

Implements task group 10 of the template-import-overhaul spec.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from typing import Any, Literal

from pydantic import ValidationError

from backend.config import settings

from .types import (
    ChatMessage,
    ElementActionRecord,
    LLMAssetDecision,
    LLMElementAction,
    LLMPageSelections,
    LLMPlaceholderDecision,
    LLMPlanError,
    LLMTemplateImportPlan,
    LLMTraceEntry,
    PipelineContext,
    ReviewDraft,
)

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Tunables
# ─────────────────────────────────────────────────────────────────────────────

_EXCERPT_MAX_BYTES = 4096
_FEEDBACK_HISTORY_LIMIT = 10
_CONVERSATION_LIMIT = 20  # 10 user + 10 assistant
_PAGE_TYPES: tuple[str, ...] = ("cover", "toc", "chapter", "content", "ending")


# ─────────────────────────────────────────────────────────────────────────────
# Canonicalization & hashing
# ─────────────────────────────────────────────────────────────────────────────

def canonicalize_payload(payload: dict[str, Any]) -> str:
    """Stable JSON dump for hashing.

    Equivalent to ``json.dumps(payload, sort_keys=True,
    ensure_ascii=False, separators=(',', ':'))`` — the canonical form
    used as input for SHA-1 caching.
    """
    return json.dumps(
        payload,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        default=_json_default,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Reply-language detection
# ─────────────────────────────────────────────────────────────────────────────

# CJK code-point ranges considered "Chinese-ish" for the purpose of
# choosing the LLM reply language. We only include the CJK Unified
# Ideographs block (U+3400-U+9FFF) and CJK Compatibility Ideographs
# (U+F900-U+FAFF) — punctuation and ASCII digits are intentionally
# excluded so things like "see chapter 3。" don't flip a mostly-English
# instruction into Chinese.
_CJK_RANGES: tuple[tuple[int, int], ...] = (
    (0x3400, 0x9FFF),
    (0xF900, 0xFAFF),
)
_CJK_THRESHOLD = 0.3


def detect_user_language(text: str | None) -> Literal["zh", "en"]:
    """Pick the LLM reply language based on the user's most recent message.

    Counts CJK code points (U+3400-U+9FFF, U+F900-U+FAFF) against the
    total non-whitespace character count. Returns ``"zh"`` when the
    ratio is ≥ 0.3, ``"en"`` otherwise (including for empty input).

    The threshold is intentionally lenient: most users mixing Chinese
    feedback with technical English nouns ("把第3页设成 cover") still
    want the response in Chinese.
    """
    if not text:
        return "en"
    cjk = 0
    total = 0
    for ch in text:
        if ch.isspace():
            continue
        total += 1
        cp = ord(ch)
        for lo, hi in _CJK_RANGES:
            if lo <= cp <= hi:
                cjk += 1
                break
    if total == 0:
        return "en"
    return "zh" if (cjk / total) >= _CJK_THRESHOLD else "en"


def compute_input_hash(payload: dict[str, Any]) -> str:
    """SHA-1 hex digest of the canonical JSON payload."""
    return hashlib.sha1(canonicalize_payload(payload).encode("utf-8")).hexdigest()


def _json_default(obj: Any) -> Any:
    """Best-effort fallback for non-JSON-native objects (Pydantic, dataclasses)."""
    if hasattr(obj, "model_dump"):
        return obj.model_dump()
    if hasattr(obj, "__dict__"):
        return obj.__dict__
    return str(obj)


def _truncate(text: str, max_bytes: int = _EXCERPT_MAX_BYTES) -> str:
    """Truncate ``text`` to at most ``max_bytes`` bytes (UTF-8)."""
    if not text:
        return ""
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    # Decode with errors='ignore' to avoid splitting a multi-byte char.
    return encoded[:max_bytes].decode("utf-8", errors="ignore")


# ─────────────────────────────────────────────────────────────────────────────
# Cache lookup
# ─────────────────────────────────────────────────────────────────────────────

def _find_cached_plan(
    review: ReviewDraft,
    input_hash: str,
) -> LLMTemplateImportPlan | None:
    """Return a previously-computed plan when ``input_hash`` is found in the trace."""
    trace = review.get("llm_trace") or []
    for entry in trace:
        if entry.get("input_hash") == input_hash:
            action_plan = entry.get("action_plan") or {}
            if not action_plan:
                continue
            try:
                return LLMTemplateImportPlan.model_validate(action_plan)
            except ValidationError:
                logger.debug("Cached action_plan failed validation; ignoring cache hit.")
                return None
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Payload construction
# ─────────────────────────────────────────────────────────────────────────────

def _build_payload(
    review: ReviewDraft,
    manifest: dict[str, Any],
    feedback: str | None,
) -> dict[str, Any]:
    """Build the payload sent to the LLM (and hashed for caching)."""
    return {
        "manifest_summary": _summarize_manifest(manifest),
        "review_summary": _summarize_review(review),
        "feedback": feedback or "",
        "user_annotations": _summarize_annotations(review),
        "reply_language": _resolve_reply_language(review, feedback),
        "design_spec_reference": _read_design_spec_reference(),
        "schema": json.dumps(LLMTemplateImportPlan.model_json_schema(), sort_keys=True),
    }


def _read_design_spec_reference(max_chars: int = 36000) -> str:
    path = settings.templates_dir / "design_spec_reference.md"
    try:
        return path.read_text(encoding="utf-8")[:max_chars]
    except OSError:
        return ""


def _resolve_reply_language(
    review: ReviewDraft,
    feedback: str | None,
) -> Literal["zh", "en"]:
    """Pick LLM reply language from the most recent user feedback.

    Priority:
      1. The current ``feedback`` argument (this turn's user message).
      2. The last entry in ``review.feedback_history``.
      3. Fall back to ``"en"``.
    """
    if feedback and feedback.strip():
        return detect_user_language(feedback)
    history = review.get("feedback_history") or []
    if history:
        last = history[-1]
        if isinstance(last, str):
            return detect_user_language(last)
        if isinstance(last, dict):
            text = last.get("feedback") or last.get("content")
            if isinstance(text, str):
                return detect_user_language(text)
    return "en"


def _summarize_annotations(review: ReviewDraft) -> list[str]:
    """Render user-drawn annotations as plain-text bullets for the LLM.

    Each annotation becomes a single line such as
    ``"On slide 3, region (x=10.0%, y=20.0%, w=30.0%, h=5.0%): this is the LOGO"``.
    The percent-format mirrors the spec's annotation-coordinate contract
    so that the human-readable LLM prompt and the visual overlay use the
    same normalized [0,1] units.

    Resolved annotations are skipped — the user explicitly marked them
    as no longer relevant. The list is sorted by ``(slide_index,
    created_at)`` for deterministic hashing.
    """
    raw = review.get("annotations") or []
    bullets: list[str] = []
    items: list[tuple[int, float, dict[str, Any]]] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        if entry.get("resolved"):
            continue
        slide_index = int(entry.get("slide_index") or 0)
        created_at = float(entry.get("created_at") or 0.0)
        items.append((slide_index, created_at, entry))
    items.sort(key=lambda t: (t[0], t[1]))
    for slide_index, _, entry in items:
        bbox = entry.get("bbox_norm") or {}
        x = float(bbox.get("x") or 0.0)
        y = float(bbox.get("y") or 0.0)
        w = float(bbox.get("width") or 0.0)
        h = float(bbox.get("height") or 0.0)
        note = str(entry.get("note") or "").strip()
        if not note:
            continue
        linked = entry.get("linked_element_id")
        link_part = f" [element={linked}]" if linked else ""
        bullets.append(
            f"On slide {slide_index}, region "
            f"(x={x * 100:.1f}%, y={y * 100:.1f}%, "
            f"w={w * 100:.1f}%, h={h * 100:.1f}%){link_part}: {note}"
        )
    return bullets


def _summarize_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
    """Compact, deterministic projection of the manifest for the LLM prompt."""
    if not manifest:
        return {}
    keys = (
        "template_id",
        "label",
        "slide_count",
        "canvas",
        "theme",
        "page_type_candidates",
        "page_selections",
        "common_assets",
        "content_area",
    )
    summary: dict[str, Any] = {}
    for key in keys:
        if key in manifest:
            summary[key] = manifest[key]
    return summary


def _summarize_review(review: ReviewDraft) -> dict[str, Any]:
    """Compact, deterministic projection of the draft for the LLM prompt."""
    if not review:
        return {}
    keys = (
        "import_id",
        "label",
        "slide_count",
        "page_selections",
        "page_type_candidates",
        "assets",
        "preserve_texts",
        "placeholder_hints",
        "element_actions",
        "feedback_history",
    )
    summary: dict[str, Any] = {}
    for key in keys:
        if key in review:
            summary[key] = review[key]
    return summary


# ─────────────────────────────────────────────────────────────────────────────
# LLMClient
# ─────────────────────────────────────────────────────────────────────────────

class LLMClient:
    """Thin wrapper around the underlying LLM with retry + trace logging."""

    def __init__(self, ctx: PipelineContext | None = None) -> None:
        self._ctx = ctx

    async def assist(
        self,
        ctx: PipelineContext,
        review: ReviewDraft,
        manifest: dict[str, Any],
        feedback: str | None,
        *,
        required: bool = False,
    ) -> tuple[LLMTemplateImportPlan, LLMTraceEntry]:
        """Produce a plan + trace entry for the current draft.

        On ``input_hash`` cache hit returns the previous plan and marks
        ``retried_no_change=True``. Skips the call (returns a
        skipped-marker entry) when the LLM is unavailable and
        ``required=False``.
        """
        self._ctx = ctx
        payload = _build_payload(review, manifest, feedback)
        input_hash = compute_input_hash(payload)
        iteration = len(review.get("llm_trace") or []) + 1
        now = time.time()

        # ── Cache lookup ────────────────────────────────────────────────
        cached = _find_cached_plan(review, input_hash)
        if cached is not None:
            entry: LLMTraceEntry = {
                "iteration": iteration,
                "updated_at": now,
                "input_hash": input_hash,
                "input_excerpt": _truncate(canonicalize_payload(payload)),
                "raw_response_excerpt": "",
                "action_plan": cached.model_dump(),
                "changed": False,
                "retried_no_change": True,
                "rule_patches": [],
            }
            self._record_trace(review, entry, feedback, cached)
            return cached, entry

        # ── Live call (with optional skip on availability failure) ─────
        prior_plan = self._latest_plan(review)
        try:
            plan = await self.call_with_retry(payload)
        except LLMPlanError as exc:
            if required:
                raise
            reason = exc.reason or str(exc)
            entry = {
                "iteration": iteration,
                "updated_at": now,
                "input_hash": input_hash,
                "input_excerpt": _truncate(canonicalize_payload(payload)),
                "raw_response_excerpt": "",
                "action_plan": {},
                "changed": False,
                "retried_no_change": False,
                "rule_patches": [f"llm.skipped: {reason}"],
            }
            empty_plan = LLMTemplateImportPlan()
            self._record_trace(review, entry, feedback, empty_plan, skipped=True)
            return empty_plan, entry

        raw_excerpt = self._last_raw_response or ""
        entry = {
            "iteration": iteration,
            "updated_at": now,
            "input_hash": input_hash,
            "input_excerpt": _truncate(canonicalize_payload(payload)),
            "raw_response_excerpt": _truncate(raw_excerpt),
            "action_plan": plan.model_dump(),
            "changed": prior_plan is None or plan.model_dump() != prior_plan.model_dump(),
            "retried_no_change": False,
            "rule_patches": [],
        }
        self._record_trace(review, entry, feedback, plan)
        return plan, entry

    # ── Retry loop ─────────────────────────────────────────────────────

    async def call_with_retry(self, payload: dict[str, Any]) -> LLMTemplateImportPlan:
        """Call the model with one structured-repair retry on failure.

        Raises :class:`LLMPlanError` (``error_kind="llm"``) when the
        second attempt still fails to produce a schema-valid plan.
        """
        schema_str = json.dumps(LLMTemplateImportPlan.model_json_schema(), sort_keys=True)
        lang = payload.get("reply_language") if isinstance(payload, dict) else None
        if lang not in ("zh", "en"):
            lang = "en"
        language_prompt = (
            f"You are helping a user import a PPTX template. The user's "
            f"interface language is {lang}. Reply (including all `reason` "
            f"fields on element_actions / placeholder_decisions / "
            f"asset_decisions) in {lang}."
        )
        system_prompt = (
            "You are a senior PPTX template-import analyst. Return exactly one JSON object "
            "conforming to the supplied schema. No markdown, no prose, no code fences. "
            "Every `reason` field MUST be human-readable text in the user's language so the "
            "frontend can show it on suggestion cards. `design_spec_md` MUST be a non-empty "
            "Markdown document that follows the supplied design_spec_reference structure exactly, "
            "including sections I through XI."
        )
        user_prompt = (
            "Analyze this PPTX template-import draft and return a plan as a single JSON "
            f"object that validates against this schema:\n{schema_str}\n\n"
            "For `design_spec_md`, adapt the design_spec_reference to this imported template pack: "
            "describe canvas, visual theme, typography, the selected page types, placeholder/content "
            "guidance, image/assets, speaker notes, and technical constraints.\n\n"
            f"Evidence:\n{json.dumps(payload, ensure_ascii=False, sort_keys=True)}"
        )
        messages: list[dict[str, str]] = [
            {"role": "system", "content": language_prompt},
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]

        # First attempt
        raw = await self._call_model(messages)
        self._last_raw_response = raw
        try:
            return _parse_plan(raw)
        except (ValidationError, ValueError, json.JSONDecodeError) as first_error:
            logger.warning(
                "Template import LLM returned malformed plan; attempting structured repair: %s",
                first_error,
            )

        # Repair attempt
        repair_messages = list(messages) + [
            {"role": "assistant", "content": _truncate(raw or "", 8192)},
            {
                "role": "user",
                "content": (
                    "Your previous response was not valid JSON or did not match the schema. "
                    f"Error: {first_error}. Please retry. The schema is: {schema_str}. "
                    f"Your previous raw response was: {_truncate(raw or '', 8192)}"
                ),
            },
        ]
        try:
            repaired = await self._call_model(repair_messages)
            self._last_raw_response = repaired
            return _parse_plan(repaired)
        except (ValidationError, ValueError, json.JSONDecodeError) as second_error:
            raise LLMPlanError(
                "valid JSON after structured retry",
                error_kind="llm",
                context={"first_error": str(first_error), "second_error": str(second_error)},
            ) from second_error
        except LLMPlanError:
            raise
        except Exception as exc:  # pragma: no cover - network/provider failures
            raise LLMPlanError(
                "valid JSON after structured retry",
                error_kind="llm",
                context={"first_error": str(first_error), "second_error": repr(exc)},
            ) from exc

    # ── Underlying transport ───────────────────────────────────────────

    _last_raw_response: str = ""

    async def _call_model(self, messages: list[dict[str, str]]) -> str:
        """Send ``messages`` to the configured chat model and return raw text.

        Resolution order:

        1. ``ctx.model_config["client"]`` — explicit injection (used in tests).
        2. ``backend.llm.registry.create_provider`` — production path.

        Raises :class:`LLMPlanError` when no transport is available.
        """
        ctx = self._ctx
        model_config: dict[str, Any] = {}
        if ctx is not None:
            model_config = dict(ctx.model_config or {})

        # 1) Explicit client injection (test seam)
        client = model_config.get("client")
        if client is not None:
            return await _invoke_injected_client(client, messages, model_config)

        # 2) Provider registry
        provider_name = model_config.get("provider")
        api_key = model_config.get("api_key")
        if not provider_name or not api_key:
            raise LLMPlanError(
                "no model client",
                error_kind="llm",
                context={"reason": "no provider/api_key configured"},
            )

        try:
            from backend.llm.registry import create_provider  # type: ignore[import-not-found]
            from backend.llm.types import LLMMessage  # type: ignore[import-not-found]
        except ImportError as exc:
            raise LLMPlanError(
                "no model client",
                error_kind="llm",
                context={"reason": f"backend.llm unavailable: {exc!r}"},
            ) from exc

        try:
            provider = create_provider(
                provider_name,
                api_key,
                base_url=model_config.get("base_url"),
                deepseek_settings=model_config.get("deepseek_settings"),
                openai_settings=model_config.get("openai_settings"),
            )
        except Exception as exc:  # pragma: no cover - misconfiguration
            raise LLMPlanError(
                "no model client",
                error_kind="llm",
                context={"reason": f"create_provider failed: {exc!r}"},
            ) from exc

        llm_messages = []
        for msg in messages:
            role = msg.get("role", "user")
            content = msg.get("content", "")
            if role == "system":
                llm_messages.append(LLMMessage.system(content))
            elif role == "assistant":
                llm_messages.append(LLMMessage.assistant(content))
            else:
                llm_messages.append(LLMMessage.user(content))

        try:
            response = await provider.chat(
                llm_messages,
                model_config.get("model", ""),
                temperature=float(model_config.get("temperature", 0.1)),
                max_tokens=int(model_config.get("max_tokens", 8192)),
            )
        except Exception as exc:  # pragma: no cover - network/provider failures
            raise LLMPlanError(
                "no model client",
                error_kind="llm",
                context={"reason": f"provider.chat failed: {exc!r}"},
            ) from exc

        return getattr(response, "content", "") or ""

    # ── Bookkeeping ────────────────────────────────────────────────────

    def _latest_plan(self, review: ReviewDraft) -> LLMTemplateImportPlan | None:
        trace = review.get("llm_trace") or []
        for entry in reversed(trace):
            action_plan = entry.get("action_plan") or {}
            if not action_plan:
                continue
            try:
                return LLMTemplateImportPlan.model_validate(action_plan)
            except ValidationError:
                continue
        return None

    def _record_trace(
        self,
        review: ReviewDraft,
        entry: LLMTraceEntry,
        feedback: str | None,
        plan: LLMTemplateImportPlan,
        *,
        skipped: bool = False,
    ) -> None:
        """Append ``entry`` to ``review.llm_trace`` and update conversation/feedback caps."""
        trace = review.setdefault("llm_trace", [])  # type: ignore[arg-type]
        trace.append(entry)

        # feedback_history cap: last 10 with feedback
        if feedback:
            history = list(review.get("feedback_history") or [])
            history.append(feedback)
            review["feedback_history"] = history[-_FEEDBACK_HISTORY_LIMIT:]

        # conversation: 10 user + 10 assistant, bounded by total 20 entries
        convo = list(review.get("conversation") or [])
        now = entry.get("updated_at") or time.time()
        if feedback:
            user_msg: ChatMessage = {
                "role": "user",
                "content": feedback,
                "created_at": now,
            }
            convo.append(user_msg)
        summary = _summarize_plan(plan, skipped=skipped)
        assistant_msg: ChatMessage = {
            "role": "assistant",
            "content": summary,
            "created_at": now,
        }
        convo.append(assistant_msg)
        review["conversation"] = convo[-_CONVERSATION_LIMIT:]


# ─────────────────────────────────────────────────────────────────────────────
# Helpers used by call_with_retry
# ─────────────────────────────────────────────────────────────────────────────

_FENCED_RE = re.compile(r"```(?:json)?\s*(.+?)\s*```", re.DOTALL | re.IGNORECASE)


def _extract_json_object(content: str) -> str:
    stripped = (content or "").strip()
    if not stripped:
        return ""
    fenced = _FENCED_RE.search(stripped)
    if fenced:
        return fenced.group(1).strip()
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start >= 0 and end > start:
        return stripped[start : end + 1]
    return stripped


def _parse_plan(raw: str) -> LLMTemplateImportPlan:
    text = _extract_json_object(raw or "")
    if not text:
        raise ValueError("empty response")
    if not text.lstrip().startswith("{"):
        raise ValueError("response did not contain a JSON object")
    try:
        return LLMTemplateImportPlan.model_validate_json(text)
    except (ValidationError, ValueError):
        return LLMTemplateImportPlan.model_validate(json.loads(text))


async def _invoke_injected_client(
    client: Any,
    messages: list[dict[str, str]],
    model_config: dict[str, Any],
) -> str:
    """Invoke a duck-typed client injected via ``ctx.model_config['client']``."""
    if hasattr(client, "chat"):
        try:
            from backend.llm.types import LLMMessage  # type: ignore[import-not-found]

            llm_messages = []
            for msg in messages:
                role = msg.get("role", "user")
                content = msg.get("content", "")
                if role == "system":
                    llm_messages.append(LLMMessage.system(content))
                elif role == "assistant":
                    llm_messages.append(LLMMessage.assistant(content))
                else:
                    llm_messages.append(LLMMessage.user(content))
            response = await client.chat(
                llm_messages,
                model_config.get("model", ""),
                temperature=float(model_config.get("temperature", 0.1)),
                max_tokens=int(model_config.get("max_tokens", 8192)),
            )
            return getattr(response, "content", "") or ""
        except ImportError:
            pass
    # Fall-through: assume the client is awaitable on (messages,)
    if callable(client):
        result = client(messages)
        if hasattr(result, "__await__"):
            result = await result
        return str(result or "")
    raise LLMPlanError(
        "no model client",
        error_kind="llm",
        context={"reason": "injected client is not callable"},
    )


def _summarize_plan(plan: LLMTemplateImportPlan, *, skipped: bool = False) -> str:
    """Short human-readable summary used as the assistant turn in conversation."""
    if skipped:
        return "LLM skipped (model unavailable); draft unchanged."
    bits: list[str] = []
    selections = plan.page_selections.model_dump(exclude_none=True)
    if selections:
        bits.append("selections=" + ",".join(f"{k}:{v}" for k, v in sorted(selections.items())))
    if plan.element_actions:
        bits.append(f"actions={len(plan.element_actions)}")
    if plan.asset_decisions:
        bits.append(f"assets={len(plan.asset_decisions)}")
    if plan.placeholder_decisions:
        bits.append(f"placeholders={len(plan.placeholder_decisions)}")
    if plan.preserve_texts:
        bits.append(f"preserve={len(plan.preserve_texts)}")
    if not bits:
        return "Plan produced (no changes)."
    return "Plan: " + "; ".join(bits)


# ─────────────────────────────────────────────────────────────────────────────
# merge_llm_plan / extract_plan
# ─────────────────────────────────────────────────────────────────────────────

def merge_llm_plan(draft: ReviewDraft, plan: LLMTemplateImportPlan) -> ReviewDraft:
    """Idempotently merge an LLM plan into the review draft.

    Updates ``page_selections`` / ``assets`` / ``element_actions`` /
    ``placeholder_hints`` / ``preserve_texts`` / ``design_spec_md`` so that
    ``merge_llm_plan(merge_llm_plan(d, p), p) == merge_llm_plan(d, p)``.

    Mutates and returns ``draft`` (callers may copy beforehand).
    """
    # Page selections — non-None values from the plan win.
    selections = dict(draft.get("page_selections") or {})
    plan_selections = plan.page_selections.model_dump(exclude_none=True)
    for page_type in _PAGE_TYPES:
        value = plan_selections.get(page_type)
        if value is not None:
            selections[page_type] = int(value)
    # Drop the confidence field — it is metadata, not a selection.
    selections.pop("confidence", None)
    draft["page_selections"] = selections  # type: ignore[typeddict-item]

    # Assets — overlay LLM decisions, marking role_source="llm".
    assets = dict(draft.get("assets") or {})
    for decision in plan.asset_decisions:
        entry: dict[str, Any] = {
            "asset_id": decision.asset_id,
            "role": decision.role,
            "role_source": "llm",
        }
        if decision.name:
            entry["name"] = decision.name
        assets[decision.asset_id] = entry  # type: ignore[assignment]
    draft["assets"] = assets  # type: ignore[typeddict-item]

    # Element actions — clear and rebuild from the plan, then sort+dedupe.
    rebuilt: list[ElementActionRecord] = []
    seen: set[tuple[str, str]] = set()
    for action in plan.element_actions:
        key = (action.page_type, action.element_id)
        if key in seen:
            continue
        seen.add(key)
        record: ElementActionRecord = {
            "page_type": action.page_type,
            "element_id": action.element_id,
            "action": action.action,
            "source": "llm",
        }
        if action.placeholder:
            record["placeholder"] = action.placeholder
        if action.reason:
            record["reason"] = action.reason
        rebuilt.append(record)
    rebuilt.sort(key=lambda r: (r.get("page_type", ""), r.get("element_id", "")))
    draft["element_actions"] = rebuilt

    # Placeholder hints — merge per-page-type.
    hints: dict[str, dict[str, str]] = {}
    raw_hints = draft.get("placeholder_hints") or {}
    for pt, mapping in raw_hints.items():
        if isinstance(mapping, dict):
            hints[pt] = dict(mapping)
    for decision in plan.placeholder_decisions:
        hints.setdefault(decision.page_type, {})[decision.name] = decision.text
    # Sort each inner dict for byte-stable round-trips.
    draft["placeholder_hints"] = {  # type: ignore[typeddict-item]
        pt: dict(sorted(mapping.items())) for pt, mapping in sorted(hints.items())
    }

    # Preserve texts — union (preserve user adds), sorted+deduped.
    preserve = set(draft.get("preserve_texts") or [])
    preserve.update(plan.preserve_texts)
    draft["preserve_texts"] = sorted(t for t in preserve if t)

    # design_spec_md — overwrite when the plan supplies one.
    if plan.design_spec_md is not None:
        draft["design_spec_md"] = plan.design_spec_md

    return draft


def extract_plan(draft: ReviewDraft) -> LLMTemplateImportPlan:
    """Reverse-extract a plan from a draft for round-trip parity.

    Satisfies ``extract_plan(merge_llm_plan(d0, p)) == p`` on the
    ``page_selections / asset_decisions / placeholder_decisions /
    element_actions`` axes.
    """
    # Page selections
    raw_selections = dict(draft.get("page_selections") or {})
    selection_kwargs: dict[str, Any] = {}
    for page_type in _PAGE_TYPES:
        if page_type in raw_selections and raw_selections[page_type] is not None:
            selection_kwargs[page_type] = int(raw_selections[page_type])
    page_selections = LLMPageSelections(**selection_kwargs)

    # Asset decisions — only entries that came from the LLM
    asset_decisions: list[LLMAssetDecision] = []
    for asset_id, entry in (draft.get("assets") or {}).items():
        if not isinstance(entry, dict):
            continue
        if entry.get("role_source") != "llm":
            continue
        role = entry.get("role")
        if not role:
            continue
        asset_decisions.append(
            LLMAssetDecision(
                asset_id=entry.get("asset_id", asset_id),
                role=role,
                name=entry.get("name"),
            )
        )
    asset_decisions.sort(key=lambda d: d.asset_id)

    # Placeholder decisions
    placeholder_decisions: list[LLMPlaceholderDecision] = []
    for page_type, mapping in (draft.get("placeholder_hints") or {}).items():
        if not isinstance(mapping, dict):
            continue
        for name, text in mapping.items():
            try:
                placeholder_decisions.append(
                    LLMPlaceholderDecision(
                        page_type=page_type,
                        name=name,
                        text=text,
                    )
                )
            except ValidationError:
                continue
    placeholder_decisions.sort(key=lambda d: (d.page_type, d.name))

    # Element actions — only those marked as LLM-sourced
    element_actions: list[LLMElementAction] = []
    for record in draft.get("element_actions") or []:
        if not isinstance(record, dict):
            continue
        if record.get("source") != "llm":
            continue
        try:
            element_actions.append(
                LLMElementAction(
                    page_type=record["page_type"],
                    element_id=record["element_id"],
                    action=record["action"],
                    placeholder=record.get("placeholder") or None,
                    reason=record.get("reason"),
                )
            )
        except (KeyError, ValidationError):
            continue
    element_actions.sort(key=lambda a: (a.page_type, a.element_id))

    preserve_texts = sorted(t for t in (draft.get("preserve_texts") or []) if t)
    design_spec_md = draft.get("design_spec_md")

    return LLMTemplateImportPlan(
        page_selections=page_selections,
        asset_decisions=asset_decisions,
        placeholder_decisions=placeholder_decisions,
        element_actions=element_actions,
        preserve_texts=preserve_texts,
        design_spec_md=design_spec_md,
    )


__all__ = [
    "canonicalize_payload",
    "compute_input_hash",
    "LLMClient",
    "merge_llm_plan",
    "extract_plan",
]
