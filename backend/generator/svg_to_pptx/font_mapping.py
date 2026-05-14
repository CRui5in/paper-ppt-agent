"""Cross-platform font mapping for SVG-to-PPTX conversion.

Maps CSS font-family stacks to concrete DrawingML typefaces, handling
cross-platform differences (macOS/Linux fonts → Windows equivalents)
and separating Latin vs East Asian font slots.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Known East Asian font names
# ---------------------------------------------------------------------------
EA_FONTS: set[str] = {
    # macOS
    "PingFang SC", "PingFang TC", "PingFang HK",
    "Hiragino Sans", "Hiragino Sans GB", "Hiragino Mincho ProN",
    "Songti SC", "Songti TC",
    # Windows
    "Microsoft YaHei", "Microsoft JhengHei",
    "SimSun", "SimHei", "FangSong", "KaiTi",
    # Cross-platform / Linux
    "Noto Sans CJK SC", "Noto Sans CJK TC",
    "Noto Sans SC", "Noto Sans TC",
    "Noto Serif CJK SC", "Noto Serif CJK TC",
    "Noto Serif SC", "Noto Serif TC",
    "Source Han Sans SC", "Source Han Sans TC",
    "Source Han Serif SC", "Source Han Serif TC",
    "WenQuanYi Micro Hei", "WenQuanYi Zen Hei",
    # Legacy / macOS older
    "STHeiti", "STSong", "STKaiti", "STFangsong", "STXihei", "STZhongsong",
    "YouYuan", "LiSu", "HuaWenKaiTi",
    # Additional Windows
    "DengXian", "FangSong", "NSimSun",
}

# CSS system font keywords — not real font names, skip during resolution
SYSTEM_FONTS: set[str] = {
    "system-ui", "-apple-system", "BlinkMacSystemFont",
}

# ---------------------------------------------------------------------------
# macOS / Linux → Windows font fallback mapping
# ---------------------------------------------------------------------------
FONT_FALLBACK_WIN: dict[str, str] = {
    # CJK — macOS → Windows
    "PingFang SC": "Microsoft YaHei",
    "PingFang TC": "Microsoft JhengHei",
    "PingFang HK": "Microsoft JhengHei",
    "Hiragino Sans": "Microsoft YaHei",
    "Hiragino Sans GB": "Microsoft YaHei",
    "Hiragino Mincho ProN": "SimSun",
    "STHeiti": "SimHei",
    "STSong": "SimSun",
    "STKaiti": "KaiTi",
    "STFangsong": "FangSong",
    "STXihei": "Microsoft YaHei",
    "STZhongsong": "SimSun",
    "Songti SC": "SimSun",
    "Songti TC": "SimSun",
    # CJK — Linux / cross-platform → Windows
    "Noto Sans CJK SC": "Microsoft YaHei",
    "Noto Sans CJK TC": "Microsoft JhengHei",
    "Noto Sans SC": "Microsoft YaHei",
    "Noto Sans TC": "Microsoft JhengHei",
    "Noto Serif CJK SC": "SimSun",
    "Noto Serif CJK TC": "SimSun",
    "Noto Serif SC": "SimSun",
    "Noto Serif TC": "SimSun",
    "Source Han Sans SC": "Microsoft YaHei",
    "Source Han Sans TC": "Microsoft JhengHei",
    "Source Han Serif SC": "SimSun",
    "Source Han Serif TC": "SimSun",
    "WenQuanYi Micro Hei": "Microsoft YaHei",
    "WenQuanYi Zen Hei": "Microsoft YaHei",
    # Latin — macOS / Linux → Windows
    "SF Pro": "Segoe UI",
    "SF Pro Display": "Segoe UI",
    "SF Pro Text": "Segoe UI",
    "SF Mono": "Consolas",
    "Menlo": "Consolas",
    "Monaco": "Consolas",
    "Helvetica Neue": "Arial",
    "Helvetica": "Arial",
    "Roboto": "Segoe UI",
    "Ubuntu": "Segoe UI",
    "Liberation Sans": "Arial",
    "Liberation Serif": "Times New Roman",
    "Liberation Mono": "Consolas",
    "DejaVu Sans": "Segoe UI",
    "DejaVu Serif": "Times New Roman",
    "DejaVu Sans Mono": "Consolas",
    "Aptos": "Calibri",
    "Inter": "Segoe UI",
}

# CSS generic font family → concrete Windows font
GENERIC_FONT_MAP: dict[str, str] = {
    "monospace": "Consolas",
    "sans-serif": "Segoe UI",
    "serif": "Times New Roman",
}

# Serif Latin fonts — when the resolved Latin font is serif and no EA font
# was explicitly specified, prefer SimSun (serif CJK) over Microsoft YaHei.
_SERIF_LATIN: set[str] = {
    "Times New Roman", "Georgia", "Garamond", "Palatino", "Palatino Linotype",
    "Book Antiqua", "Cambria", "SimSun", "Liberation Serif", "DejaVu Serif",
}

# Default fallbacks
_DEFAULT_LATIN = "Segoe UI"
_DEFAULT_EA = "Microsoft YaHei"


def _split_font_stack(font_stack: str) -> list[str]:
    """Split a CSS font-family string into individual family names."""
    families: list[str] = []
    current: list[str] = []
    quote: str | None = None
    for ch in font_stack:
        if ch in {"'", '"'}:
            quote = None if quote == ch else ch if quote is None else quote
            continue
        if ch == "," and quote is None:
            family = "".join(current).strip()
            if family:
                families.append(family)
            current = []
            continue
        current.append(ch)
    family = "".join(current).strip()
    if family:
        families.append(family)
    return families


def _resolve_one(name: str) -> str:
    """Resolve a single font name: skip system keywords, map generics, apply cross-platform fallback."""
    if name in SYSTEM_FONTS:
        return ""
    lower = name.lower()
    if lower in GENERIC_FONT_MAP:
        return GENERIC_FONT_MAP[lower]
    return FONT_FALLBACK_WIN.get(name, name)


def parse_font_family(font_stack: str) -> dict[str, str]:
    """Parse a CSS font-family stack into separate Latin and EA typefaces.

    Returns ``{"latin": "...", "ea": "..."}`` with Windows-safe font names.
    """
    if not font_stack or not font_stack.strip():
        return {"latin": _DEFAULT_LATIN, "ea": _DEFAULT_EA}

    raw_families = _split_font_stack(font_stack)

    latin: str | None = None
    ea: str | None = None

    for raw in raw_families:
        resolved = _resolve_one(raw)
        if not resolved:
            continue
        if resolved in EA_FONTS:
            if ea is None:
                ea = resolved
        else:
            if latin is None:
                latin = resolved
        if latin and ea:
            break

    # If only an EA font was found, use it for Latin too (PPT renders CJK
    # via the latin typeface when the EA slot doesn't match).
    if ea and not latin:
        latin = ea

    if not latin:
        latin = _DEFAULT_LATIN
    if not ea:
        # Choose serif/sans-serif CJK based on Latin font
        ea = "SimSun" if latin in _SERIF_LATIN else _DEFAULT_EA

    return {"latin": latin, "ea": ea}


def resolve_latin_font(font_stack: str) -> str:
    """Convenience: return only the Latin typeface (for SVG preview normalization)."""
    return parse_font_family(font_stack)["latin"]
