"""Multi-source data model.

A ``Source`` is one piece of input material (a PDF, a pasted note, a webpage,
a markdown file). A generation session owns an ordered list of sources; the
:mod:`backend.parser.merger` parses each one and folds them into a single
:class:`~backend.parser.paper_model.ParsedPaper` so the rest of the pipeline
stays single-document.

The ``kind`` discriminator drives parser dispatch
(:mod:`backend.parser.dispatcher`) and is also surfaced to the frontend so the
Sources panel can render an appropriate badge per row.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Literal

SourceKind = Literal["pdf", "latex", "text", "markdown", "url"]
SourceRole = Literal["primary", "supplementary"]
SourceParseStatus = Literal["pending", "fetching", "ok", "error"]


@dataclass
class Source:
    """One input material within a multi-source session.

    File-backed sources (``pdf``/``latex``/``markdown``) populate ``file_path``;
    text sources carry their payload in ``raw_text``; url sources carry the
    target link in ``url`` and, after fetching, the extracted text in
    ``raw_text``.

    The dataclass is JSON-serializable via :meth:`to_dict` / :meth:`from_dict`
    so it can be persisted inside the session snapshot (see
    :mod:`backend.session.manager`).
    """

    id: str
    kind: SourceKind
    label: str
    role: SourceRole = "primary"
    order: int = 0
    # File-backed sources.
    file_path: Path | None = None
    file_size: int = 0
    # Text / url sources.
    raw_text: str | None = None
    url: str | None = None
    # Parse telemetry — updated by the dispatcher / merger.
    parse_status: SourceParseStatus = "pending"
    parse_error: str | None = None
    # Reported after a successful parse; used by the frontend to show how much
    # each source contributed (and to warn when the combined corpus is large).
    char_count: int = 0

    def __post_init__(self) -> None:
        # Normalize a few fields so equality / serialization stay stable.
        if self.file_path is not None and not isinstance(self.file_path, Path):
            self.file_path = Path(self.file_path)

    # ── (de)serialization for session_state.json ────────────────────────────

    def to_dict(self) -> dict:
        payload = asdict(self)
        # Paths are not JSON-native; store as str (None stays None).
        fp = payload.get("file_path")
        payload["file_path"] = str(fp) if fp is not None else None
        return payload

    @classmethod
    def from_dict(cls, raw: dict) -> "Source":
        known = {
            "id",
            "kind",
            "label",
            "role",
            "order",
            "file_path",
            "file_size",
            "raw_text",
            "url",
            "parse_status",
            "parse_error",
            "char_count",
        }
        kwargs: dict = {}
        for key in known:
            if key not in raw:
                continue
            value = raw[key]
            if key == "file_path" and value:
                value = Path(value)
            kwargs[key] = value
        # ``kind`` / ``role`` / ``parse_status`` are Literal — tolerate unknown
        # legacy values by coercing to str; dispatcher / merger handle the
        # ``pending`` default gracefully.
        return cls(**kwargs)  # type: ignore[arg-type]


def empty_source_id() -> str:
    """Placeholder id generator; real ids come from the API layer."""
    import uuid

    return uuid.uuid4().hex[:12]


@dataclass
class SourceGroup:
    """An ordered collection of sources for one session."""

    session_id: str
    sources: list[Source] = field(default_factory=list)

    def ordered(self) -> list[Source]:
        """Sources sorted by ``order`` then insertion (stable)."""
        return sorted(self.sources, key=lambda s: (s.order, s.id))

    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "sources": [s.to_dict() for s in self.sources],
        }

    @classmethod
    def from_dict(cls, raw: dict) -> "SourceGroup":
        return cls(
            session_id=str(raw.get("session_id", "")),
            sources=[Source.from_dict(item) for item in raw.get("sources", [])],
        )
