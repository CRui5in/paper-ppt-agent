"""Remove SVG filters that would force full-slide raster fallback."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from pathlib import Path

from backend.generator.svg_to_pptx.styles import is_supported_shadow_filter

_FILTER_REF_RE = re.compile(r"url\(\s*#([^)]+)\s*\)", re.IGNORECASE)


def strip_filters_in_svg(svg_path: Path) -> int:
    """Remove only filters that would force full-slide raster fallback."""
    try:
        tree = ET.parse(svg_path)
    except (ET.ParseError, OSError):
        return 0

    root = tree.getroot()
    parent_map = {child: parent for parent in root.iter() for child in parent}
    filter_defs = {
        elem.get("id"): elem
        for elem in root.iter()
        if _local_tag(elem.tag) == "filter" and elem.get("id")
    }
    changed = 0

    for elem in list(root.iter()):
        tag = _local_tag(elem.tag)
        if tag == "filter":
            continue

        style = elem.get("style")
        attr_value = elem.get("filter")
        style_value = _style_property(style or "", "filter")
        filter_value = attr_value or style_value
        if not filter_value or filter_value == "none":
            continue
        match = _FILTER_REF_RE.search(filter_value)
        filter_elem = filter_defs.get(match.group(1).strip()) if match else None
        supported = (
            tag != "g"
            and filter_elem is not None
            and is_supported_shadow_filter(filter_elem)
        )
        if supported:
            continue

        if attr_value is not None:
            del elem.attrib["filter"]
            changed += 1
        if style_value is not None:
            declarations = [
                declaration.strip()
                for declaration in style.split(";")
                if declaration.strip()
                and not declaration.strip().lower().startswith("filter:")
            ]
            if declarations:
                elem.set("style", "; ".join(declarations))
            else:
                elem.attrib.pop("style", None)
            changed += 1

    referenced_ids: set[str] = set()
    for elem in root.iter():
        if _local_tag(elem.tag) == "filter":
            continue
        style = elem.get("style") or ""
        filter_value = elem.get("filter") or _style_property(style, "filter")
        match = _FILTER_REF_RE.search(filter_value or "")
        if match:
            referenced_ids.add(match.group(1).strip())

    for filter_id, elem in filter_defs.items():
        if filter_id in referenced_ids and is_supported_shadow_filter(elem):
            continue
        parent = parent_map.get(elem)
        if parent is not None:
            parent.remove(elem)
            changed += 1

    if changed:
        ET.register_namespace("", "http://www.w3.org/2000/svg")
        tree.write(svg_path, encoding="unicode")
    return changed


def _local_tag(tag: str) -> str:
    return tag.split("}", 1)[-1] if "}" in tag else tag


def _style_property(style: str, name: str) -> str | None:
    for declaration in style.split(";"):
        key, separator, value = declaration.partition(":")
        if separator and key.strip().lower() == name:
            return value.strip()
    return None
