"""Small repairs for PPTX packages produced by browser-side exporters."""

from __future__ import annotations

from io import BytesIO
from pathlib import Path
import re
import xml.etree.ElementTree as ET
import zipfile


_NOTES_PART_PREFIXES = ("ppt/notesSlides/", "ppt/notesMasters/")
_NOTES_REL_TYPES = (
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships/notesSlide",
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships/notesMaster",
)


def sanitize_pptx_bytes(data: bytes) -> bytes:
    """Return a PPTX package with directory entries and loose content types fixed.

    Some browser-generated PPTX files include ZIP directory entries such as
    ``ppt/media/`` as package parts. PowerPoint can report these decks as
    repairable even when all real slide relationships are present.
    """
    if not data:
        return data

    source = BytesIO(data)
    target = BytesIO()
    try:
        with zipfile.ZipFile(source, "r") as zin, zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as zout:
            seen: set[str] = set()
            part_names = {
                info.filename
                for info in zin.infolist()
                if info.filename and not info.filename.endswith("/") and not info.is_dir()
            }
            for info in zin.infolist():
                name = info.filename
                if not name or name.endswith("/") or info.is_dir():
                    continue
                if name.startswith(_NOTES_PART_PREFIXES):
                    continue
                if name in seen:
                    continue
                seen.add(name)
                payload = zin.read(info)
                payload = _sanitize_xml_part(name, payload, part_names)
                out_info = zipfile.ZipInfo(filename=name, date_time=info.date_time)
                out_info.comment = info.comment
                out_info.extra = info.extra
                out_info.internal_attr = info.internal_attr
                out_info.external_attr = info.external_attr
                out_info.compress_type = zipfile.ZIP_DEFLATED
                zout.writestr(out_info, payload)
    except zipfile.BadZipFile:
        return data

    return target.getvalue()


def _sanitize_xml_part(name: str, payload: bytes, part_names: set[str]) -> bytes:
    if name.endswith(".xml") or name.endswith(".rels"):
        payload = _sanitize_broken_font_entities(payload)

    if name == "[Content_Types].xml":
        return _sanitize_content_types(payload, part_names)

    if name == "docProps/app.xml":
        return re.sub(rb"<Notes>\d+</Notes>", b"<Notes>0</Notes>", payload)

    if name == "ppt/presentation.xml":
        return re.sub(rb"\s*<p:notesMasterIdLst\b.*?</p:notesMasterIdLst>", b"", payload, flags=re.DOTALL)

    if name.endswith(".rels"):
        for rel_type in _NOTES_REL_TYPES:
            payload = re.sub(
                rb"\s*<Relationship\b[^>]*Type=\"" + re.escape(rel_type.encode("ascii")) + rb"\"[^>]*/>",
                b"",
                payload,
            )
        return payload

    return payload


def _sanitize_broken_font_entities(payload: bytes) -> bytes:
    # PPTist/pptxgenjs can export a malformed font face when an imported font
    # family was quoted, e.g. ``typeface="&quot"``.  That is invalid XML because
    # the entity is missing ``;`` and PowerPoint repairs the slide on open.
    payload = re.sub(rb'typeface="&(?:quot|apos)"', b'typeface="Calibri"', payload)
    payload = re.sub(rb'typeface="&(?:quot|apos);([^"]*?)&(?:quot|apos);"', rb'typeface="\\1"', payload)
    return payload


def _sanitize_content_types(payload: bytes, part_names: set[str]) -> bytes:
    payload = payload.replace(b'ContentType="image/jpg"', b'ContentType="image/jpeg"')
    try:
        root = ET.fromstring(payload)
    except ET.ParseError:
        payload = re.sub(
            rb'\s*<Override\b[^>]*PartName="/ppt/notes(?:Slides|Masters)/[^"]+"[^>]*/>',
            b"",
            payload,
        )
        return payload

    namespace = root.tag.split("}", 1)[0].strip("{") if root.tag.startswith("{") else ""
    override_tag = f"{{{namespace}}}Override" if namespace else "Override"
    for child in list(root):
        if child.tag != override_tag:
            continue
        part_name = child.attrib.get("PartName", "").lstrip("/")
        if not part_name or part_name not in part_names or part_name.startswith(("ppt/notesSlides/", "ppt/notesMasters/")):
            root.remove(child)

    ET.register_namespace("", namespace)
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def sanitize_pptx_file(path: Path) -> None:
    data = path.read_bytes()
    sanitized = sanitize_pptx_bytes(data)
    if sanitized != data:
        path.write_bytes(sanitized)


def validate_pptx_xml(path: Path) -> list[str]:
    """Return XML/package issues that would make a PPTX structurally suspect."""
    issues: list[str] = []
    try:
        with zipfile.ZipFile(path, "r") as zf:
            names = set(zf.namelist())
            for name in names:
                if not (name.endswith(".xml") or name.endswith(".rels")):
                    continue
                try:
                    ET.fromstring(zf.read(name))
                except ET.ParseError as exc:
                    issues.append(f"{name}: {exc}")
            try:
                content_types = zf.read("[Content_Types].xml")
            except KeyError:
                issues.append("[Content_Types].xml: missing")
            else:
                for part_name in re.findall(rb'PartName="/([^"]+)"', content_types):
                    part = part_name.decode("utf-8", errors="replace")
                    if part not in names:
                        issues.append(f"[Content_Types].xml: missing part {part}")
    except zipfile.BadZipFile as exc:
        issues.append(f"zip: {exc}")
    return issues
