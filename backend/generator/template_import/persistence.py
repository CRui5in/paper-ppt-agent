"""Stable JSON serialization + transactional layout install.

All writes use ``json.dumps(sort_keys=True, ensure_ascii=False,
separators=(',', ':'))`` followed by a trailing newline so repeated
writes of equivalent objects produce byte-identical files (Requirement
14.2). Layout install is wrapped in a rename → write → drop_backup
transaction with rollback on any failure.

Skeleton only — concrete logic is implemented in task group 2.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import TYPE_CHECKING, Any

from backend.config import settings

from .types import InstallResult, LayoutPack, PersistenceError, TemplateManifest

if TYPE_CHECKING:  # pragma: no cover
    from .types import ReviewDraft


# Canonical filename for each page type in an installed layout pack
# (mirrors v1 ``template_importer.PAGE_TYPE_FILES`` so the v1 runtime
# template loader can read v2 packs without changes).
_PAGE_TYPE_FILES: dict[str, str] = {
    "cover": "01_cover.svg",
    "toc": "02_toc.svg",
    "chapter": "02_chapter.svg",
    "content": "03_content.svg",
    "ending": "04_ending.svg",
}


def _layouts_root() -> Path:
    """Directory holding all installed layout packs.

    Mirrors the v1 ``backend.generator.template_importer._templates_root``
    helper so the v2 pipeline writes to the exact same place the runtime
    template loader reads from.
    """
    return settings.templates_dir / "layouts"


# ─────────────────────────────────────────────────────────────────────────────
# Path helpers (mirror v1 ``template_importer`` conventions so v1/v2 share
# the same on-disk layout under ``workspaces/template_imports/<import_id>/``).
# ─────────────────────────────────────────────────────────────────────────────


def _imports_root() -> Path:
    return settings.workspaces_dir / "template_imports"


def _import_dir(import_id: str) -> Path:
    return _imports_root() / import_id


def _state_path(import_id: str) -> Path:
    return _import_dir(import_id) / "state.json"


def _review_path(import_id: str) -> Path:
    return _import_dir(import_id) / "review.json"


# ─────────────────────────────────────────────────────────────────────────────
# Stable JSON I/O
# ─────────────────────────────────────────────────────────────────────────────


def _dumps_stable(obj: Any) -> str:
    """Canonical JSON: sorted keys, no ASCII escaping, compact, trailing ``\\n``.

    Two equivalent objects produce byte-identical output (Requirement
    14.2), enabling round-trip stability across reads/writes.
    """
    return json.dumps(
        obj,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    ) + "\n"


def _atomic_write_text(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` atomically.

    Strategy: write to ``<path>.tmp`` then ``os.replace`` so concurrent
    readers never observe a partial file. Parent directories are
    created on demand. Bytes are written in binary mode (UTF-8, no
    newline translation) so output is byte-identical across platforms.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(text.encode("utf-8"))
    os.replace(tmp, path)


def _read_json_or_none(path: Path) -> dict[str, Any] | None:
    """Return parsed JSON dict or ``None`` if the file is absent."""
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def write_state(import_id: str, state: dict[str, Any]) -> None:
    """Persist ``state.json`` for an import task using stable JSON."""
    _atomic_write_text(_state_path(import_id), _dumps_stable(state))


def read_state(import_id: str) -> dict[str, Any] | None:
    """Read ``state.json`` for an import task, ``None`` if absent."""
    return _read_json_or_none(_state_path(import_id))


def write_review(import_id: str, draft: "ReviewDraft") -> None:
    """Persist ``review.json`` for an import task.

    ``ReviewDraft`` is a ``TypedDict(total=False)`` — at runtime it's a
    plain ``dict``, so we dump it as-is. Unknown / forward-compatible
    top-level fields are preserved untouched (Requirement 12.1).
    """
    _atomic_write_text(_review_path(import_id), _dumps_stable(draft))


def read_review(import_id: str) -> "ReviewDraft | None":
    """Read ``review.json``; returns ``None`` if absent.

    The result is a plain ``dict`` annotated as :class:`ReviewDraft`
    (TypedDicts are erased at runtime).
    """
    data = _read_json_or_none(_review_path(import_id))
    if data is None:
        return None
    return data  # type: ignore[return-value]


def transactional_install_layout(
    template_id: str,
    pack: "LayoutPack",
) -> "InstallResult":
    """Atomically install a ``LayoutPack`` under ``layouts/<template_id>``.

    Sequence (per design.md §persistence):

      1. ``backup = layouts/<template_id>.bak`` — rmtree if a stale
         backup from a prior crash exists.
      2. If ``target = layouts/<template_id>`` exists, rename it to
         ``backup``.
      3. Idempotency short-circuit: if the existing target's
         ``manifest.json`` is byte-identical to the new pack's manifest
         (under the same canonical encoding used to write it), restore
         the rename and return ``InstallResult(already_complete=True)``
         without touching the disk.
      4. Otherwise write the new pack: 5 page-type SVGs,
         ``design_spec.md``, ``manifest.json``, optional
         ``import_trace.json``, and an ``assets/`` subdirectory.
      5. On success: ``rmtree(backup)``.
      6. On any failure: ``rmtree(target)`` + rename ``backup`` → target,
         then raise :class:`PersistenceError`.
    """
    layouts_root = _layouts_root()
    layouts_root.mkdir(parents=True, exist_ok=True)

    target = layouts_root / template_id
    backup = layouts_root / f"{template_id}.bak"

    canonical_manifest = _dumps_stable(pack.manifest).encode("utf-8")

    # ── Idempotency: existing identical pack → no-op ──────────────────────
    existing_manifest_path = target / "manifest.json"
    if target.exists() and existing_manifest_path.exists():
        try:
            existing_bytes = existing_manifest_path.read_bytes()
        except OSError:
            existing_bytes = b""
        if existing_bytes == canonical_manifest:
            return InstallResult(
                template_id=template_id,
                layout_dir=target,
                already_complete=True,
            )

    # ── Clear stale backup left by a previous crashed install ─────────────
    if backup.exists():
        shutil.rmtree(backup, ignore_errors=True)

    backed_up = False
    if target.exists():
        # ``shutil.move`` works across platforms (incl. Windows) when the
        # source and destination live on the same volume, which they do
        # here (both are children of ``layouts_root``).
        shutil.move(str(target), str(backup))
        backed_up = True

    try:
        target.mkdir(parents=True, exist_ok=False)

        # 1) Page-type SVGs
        for page_type, file_name in _PAGE_TYPE_FILES.items():
            svg_text = pack.svgs.get(page_type)
            if svg_text is None:
                # ``toc`` is the only optional page in v1; skip cleanly
                # rather than write an empty file.
                continue
            (target / file_name).write_text(svg_text, encoding="utf-8")

        # 2) design_spec.md
        (target / "design_spec.md").write_text(pack.design_spec, encoding="utf-8")

        # 3) manifest.json (canonical bytes — identical to the bytes used
        #    above for the idempotency check).
        (target / "manifest.json").write_bytes(canonical_manifest)

        # 4) import_trace.json (only when there is something to record).
        if pack.import_trace:
            (target / "import_trace.json").write_bytes(
                _dumps_stable(list(pack.import_trace)).encode("utf-8")
            )

        # 5) assets/<file_name> — payload is raw bytes already.
        if pack.assets:
            assets_dir = target / "assets"
            assets_dir.mkdir(parents=True, exist_ok=True)
            for file_name, payload in pack.assets.items():
                (assets_dir / file_name).write_bytes(payload)

    except Exception as exc:  # noqa: BLE001 — broad on purpose: rollback is the contract
        # Roll back: drop the half-written target, then restore the backup.
        if target.exists():
            shutil.rmtree(target, ignore_errors=True)
        if backed_up and backup.exists():
            try:
                shutil.move(str(backup), str(target))
            except OSError:
                # If even the rollback fails, leave the backup in place
                # so an operator can recover manually.
                pass
        raise PersistenceError(
            reason=str(exc) or exc.__class__.__name__,
            error_kind="persistence",
            context={"template_id": template_id},
        ) from exc

    # ── Success: drop the backup ──────────────────────────────────────────
    if backup.exists():
        shutil.rmtree(backup, ignore_errors=True)

    return InstallResult(
        template_id=template_id,
        layout_dir=target,
        already_complete=False,
    )


def update_user_index(
    template_id: str,
    label: str,
    manifest: TemplateManifest,
) -> None:
    """Upsert a row in ``user_templates.json`` after a successful import.

    Behaviour:

    * The row keyed by ``template_id`` is created if absent, otherwise
      its known fields are refreshed in-place; pre-existing extra
      fields on that row are preserved (Requirement 9.1, 9.2).
    * Derived fields (``slideCount``, ``imported_at``) are extracted
      from ``manifest``.
    * ``imported_at`` is preserved across re-imports if the row already
      had one — only newly-created rows get the current wall clock —
      so re-running an idempotent confirm does not perturb the byte
      output (Requirement 13.5).
    * The whole file is rewritten with stable JSON encoding so two
      equivalent index states produce identical bytes (Requirement
      14.2).
    """
    index_path = _layouts_root() / "user_templates.json"

    existing = _read_json_or_none(index_path) or {}
    if not isinstance(existing, dict):
        existing = {}
    templates = existing.get("templates")
    if not isinstance(templates, dict):
        templates = {}

    prior_row = templates.get(template_id)
    if not isinstance(prior_row, dict):
        prior_row = {}

    slide_count = int(manifest.get("slide_count") or 0)
    imported_at_raw = manifest.get("imported_at")
    imported_at: float | None
    if isinstance(imported_at_raw, (int, float)):
        imported_at = float(imported_at_raw)
    else:
        prior_imported = prior_row.get("imported_at")
        if isinstance(prior_imported, (int, float)):
            imported_at = float(prior_imported)
        else:
            import time as _time

            imported_at = _time.time()

    summary_value = manifest.get("source_file") or template_id
    summary = f"Imported from {summary_value}"

    # Build the merged row: preserve any unknown extra fields the
    # frontend or v1 codepath may have stored on the existing row.
    merged: dict[str, Any] = dict(prior_row)
    merged["label"] = label
    merged["summary"] = summary
    merged["slide_count"] = slide_count
    merged["slideCount"] = slide_count  # v1 camelCase alias
    merged["imported_at"] = imported_at

    templates[template_id] = merged
    existing["templates"] = templates

    _atomic_write_text(index_path, _dumps_stable(existing))


def remove_user_template(template_id: str) -> bool:
    """Remove a user template from disk + index.

    Steps:
      1. Reject non-``user_`` ids with :class:`ValueError` (built-in
         templates are immutable; mapped to HTTP 400 by the API layer).
      2. Remove the row from ``user_templates.json``.
      3. ``rmtree(layouts/<template_id>)`` if it exists.
      4. Return ``True`` when either step found something to delete,
         ``False`` when neither did.
    """
    if not template_id.startswith("user_"):
        raise ValueError("built-in templates are immutable")

    found = False

    index_path = _layouts_root() / "user_templates.json"
    existing = _read_json_or_none(index_path)
    if isinstance(existing, dict):
        templates = existing.get("templates")
        if isinstance(templates, dict) and template_id in templates:
            templates.pop(template_id, None)
            existing["templates"] = templates
            _atomic_write_text(index_path, _dumps_stable(existing))
            found = True

    layout_dir = _layouts_root() / template_id
    if layout_dir.is_dir():
        shutil.rmtree(layout_dir, ignore_errors=True)
        found = True

    return found


def rename_user_template(template_id: str, label: str) -> bool:
    """Rename a user template by mutating only the index ``label`` field.

    All other row fields stay byte-identical (Requirement 9.2): no SVG,
    asset, manifest, or import-trace file is touched. Raises
    :class:`ValueError` when ``template_id`` does not start with
    ``user_`` (built-in templates are immutable; mapped to HTTP 400 by
    the API layer per Requirement 9.3).

    Returns ``True`` on hit, ``False`` when the row does not exist.
    """
    if not template_id.startswith("user_"):
        raise ValueError("built-in templates are immutable")

    index_path = _layouts_root() / "user_templates.json"
    existing = _read_json_or_none(index_path)
    if not isinstance(existing, dict):
        return False

    templates = existing.get("templates")
    if not isinstance(templates, dict):
        return False

    row = templates.get(template_id)
    if not isinstance(row, dict):
        return False

    new_label = label.strip() or template_id
    if row.get("label") == new_label:
        # Idempotent: no on-disk change needed when the label is already
        # the requested value (preserves byte-identity).
        return True

    row["label"] = new_label
    templates[template_id] = row
    existing["templates"] = templates

    _atomic_write_text(index_path, _dumps_stable(existing))
    return True


# ─────────────────────────────────────────────────────────────────────────────
# v1 → v2 in-memory upgrade & layout-pack loading
# ─────────────────────────────────────────────────────────────────────────────


def upgrade_v1_to_v2_in_memory(manifest: dict[str, Any]) -> dict[str, Any]:
    """Upgrade a v1 manifest dict in-memory to v2 schema.

    Idempotent — already-v2 manifests are returned unchanged. Adds the
    fields v2 introduces (``content_area``, ``warnings``, ``common_assets``)
    with empty defaults when missing. Does **not** write back to disk;
    only the confirm path emits v2 (see ``design.md`` §Migration Strategy).
    """
    if not isinstance(manifest, dict):
        return {"schema_version": 2, "content_area": None, "warnings": [], "common_assets": []}
    if manifest.get("schema_version") == 2:
        return manifest
    upgraded: dict[str, Any] = dict(manifest)
    upgraded["schema_version"] = 2
    upgraded.setdefault("content_area", None)
    upgraded.setdefault("warnings", [])
    upgraded.setdefault("common_assets", [])
    return upgraded


def load_template_pack(template_id: str) -> "LayoutPack | None":
    """Read an installed layout pack from disk.

    Returns ``None`` when the layout directory is missing. The manifest
    is upgraded in-memory via :func:`upgrade_v1_to_v2_in_memory` so v1
    callers see v2 shape without any on-disk migration.
    """
    target = _layouts_root() / template_id
    if not target.is_dir():
        return None

    svgs: dict[str, str] = {}
    for page_type, file_name in _PAGE_TYPE_FILES.items():
        path = target / file_name
        if path.exists():
            try:
                svgs[page_type] = path.read_text(encoding="utf-8")
            except OSError:
                continue

    design_spec_path = target / "design_spec.md"
    design_spec = ""
    if design_spec_path.exists():
        try:
            design_spec = design_spec_path.read_text(encoding="utf-8")
        except OSError:
            design_spec = ""

    manifest_path = target / "manifest.json"
    manifest_raw: dict[str, Any] = {}
    if manifest_path.exists():
        try:
            manifest_raw = json.loads(manifest_path.read_text(encoding="utf-8")) or {}
        except (OSError, json.JSONDecodeError):
            manifest_raw = {}
    manifest = upgrade_v1_to_v2_in_memory(manifest_raw)

    trace_path = target / "import_trace.json"
    import_trace: list[Any] = []
    if trace_path.exists():
        try:
            loaded = json.loads(trace_path.read_text(encoding="utf-8"))
            if isinstance(loaded, list):
                import_trace = loaded
        except (OSError, json.JSONDecodeError):
            import_trace = []

    assets: dict[str, bytes] = {}
    assets_dir = target / "assets"
    if assets_dir.is_dir():
        for child in assets_dir.iterdir():
            if child.is_file():
                try:
                    assets[child.name] = child.read_bytes()
                except OSError:
                    continue

    label = str(manifest.get("label") or template_id)
    return LayoutPack(
        template_id=template_id,
        label=label,
        manifest=manifest,  # type: ignore[arg-type]
        svgs=svgs,  # type: ignore[arg-type]
        design_spec=design_spec,
        assets=assets,
        import_trace=import_trace,
    )
