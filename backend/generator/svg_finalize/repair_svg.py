"""Repair common malformed SVG/XML patterns before structured post-processing."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from pathlib import Path

from lxml import etree


_AMP_RE = re.compile(r"&(?!#\d+;|#x[0-9A-Fa-f]+;|[A-Za-z_][\w.-]*;)")
_COMMENT_RE = re.compile(r"<!--(.*?)-->", re.DOTALL)
_TEXT_CLOSE_RE = re.compile(r"(<text\b[^>]*>[^<]*)</tspan>", re.IGNORECASE)
_TEXT_BLOCK_RE = re.compile(r"(<text\b[^>]*>)(.*?)(</text\s*>)", re.IGNORECASE | re.DOTALL)
_SPAN_OPEN_RE = re.compile(r"<span\b([^>]*)>", re.IGNORECASE)
_SPAN_CLOSE_RE = re.compile(r"</span\s*>", re.IGNORECASE)
_LINE_TAG_RE = re.compile(r"<line\b[^>]*>", re.IGNORECASE)
_ATTR_NAME_RE = re.compile(r"\s+([A-Za-z_:][\w:.-]*)\s*=")
_ANIMATION_ELEMENT_RE = re.compile(
    r"<(?:[A-Za-z_][\w.-]*:)?(?:animate|animateMotion|animateTransform|set)\b"
    r"[^>]*(?:/>\s*|>.*?</(?:[A-Za-z_][\w.-]*:)?"
    r"(?:animate|animateMotion|animateTransform|set)\s*>)",
    re.IGNORECASE | re.DOTALL,
)
_KEYFRAMES_RE = re.compile(
    r"@(?:-[A-Za-z]+-)?keyframes\s+[^{]+\{(?:[^{}]|\{[^{}]*\})*\}",
    re.IGNORECASE | re.DOTALL,
)
_CSS_ANIMATION_DECL_RE = re.compile(
    r"\s*(?:-[A-Za-z]+-)?animation(?:-[A-Za-z-]+)?\s*:\s*[^;\"']+;?",
    re.IGNORECASE,
)
_TEXT_ELEMENT_RE = re.compile(r"<text\b(?P<attrs>[^>]*)>(?P<body>.*?)</text\s*>", re.IGNORECASE | re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")
_ATTR_RE_TEMPLATE = r"""\b{name}\s*=\s*["']([^"']+)["']"""
_ADMIN_HEADER_RE = re.compile(
    r"^(?:"
    r"(?:part|section|chapter|slide|page)\s*[:#.-]?\s*\d{1,2}"
    r"|第\s*\d{1,2}\s*(?:页|章|节)"
    r"|(?:章节|部分)\s*\d{1,2}"
    r")$",
    re.IGNORECASE,
)
_FONT_FAMILY_ATTR_RE = re.compile(
    r'font-family="([^<>]*?)"\s+(?=(?:x|y|dx|dy|font-size|font-weight|font-style|'
    r'fill|stroke|text-anchor|letter-spacing|dominant-baseline|opacity|transform|class|id|style)=)',
    re.IGNORECASE,
)

# SVG spec forbids nesting <text> inside <text> — LLMs sometimes emit it
# anyway to fake inline emphasis. When that happens, lxml's recover mode
# silently splits the run into overlapping siblings, producing scrambled
# output. ``_unnest_text`` below pre-rewrites each inner ``<text ...>X</text>``
# into ``<tspan ...>X</tspan>`` so the SVG parses correctly and the inline
# run renders as one logical line.


def _unnest_text(content: str) -> tuple[str, int]:
    """Convert any inner ``<text>`` nested inside an outer ``<text>`` into ``<tspan>``.

    Returns ``(new_content, changes)``. Operates on the raw string because
    we run *before* the XML parser; this is intentional — once lxml
    recovers from the malformed nesting it is too late to know what was
    nested where. A bounded outer-loop is used so multiple levels of
    accidental nesting collapse cleanly.
    """
    changes = 0

    def _scan_once(src: str) -> tuple[str, int]:
        out = []
        i = 0
        depth = 0  # currently-open <text> elements
        local_changes = 0
        text_open_re = re.compile(r"<text\b[^>]*>", re.IGNORECASE)
        text_close_re = re.compile(r"</text\s*>", re.IGNORECASE)
        n = len(src)
        while i < n:
            open_match = text_open_re.search(src, i)
            close_match = text_close_re.search(src, i)
            # Earliest tag wins.
            next_open = open_match.start() if open_match else n + 1
            next_close = close_match.start() if close_match else n + 1
            if next_open == n + 1 and next_close == n + 1:
                out.append(src[i:])
                break
            if next_open < next_close:
                # An <text> tag is opening.
                if depth >= 1 and open_match is not None:
                    # Inner <text> — rewrite this open + its matching close
                    # to a <tspan> pair. Find the matching </text>.
                    inner_open_end = open_match.end()
                    attrs = open_match.group(0)[len("<text"):-1]
                    # Greedy-but-bounded scan for matching close, allowing
                    # further nesting (rare).
                    sub_depth = 1
                    j = inner_open_end
                    while j < n and sub_depth > 0:
                        nxt_open = text_open_re.search(src, j)
                        nxt_close = text_close_re.search(src, j)
                        if nxt_close is None:
                            break
                        if nxt_open is not None and nxt_open.start() < nxt_close.start():
                            sub_depth += 1
                            j = nxt_open.end()  # noqa: PERF — inner branch
                        else:
                            sub_depth -= 1
                            j = nxt_close.end()  # nxt_close is not None here
                            if sub_depth == 0:
                                inner_close_start = nxt_close.start()
                                inner_close_end = nxt_close.end()
                                # Emit everything before this inner open.
                                out.append(src[i:open_match.start()])
                                # Rewrite as <tspan>...</tspan>, preserving
                                # the inner content verbatim (any further
                                # nested <text> inside will be picked up by
                                # the next outer pass via local_changes).
                                inner_body = src[inner_open_end:inner_close_start]
                                out.append(f"<tspan{attrs}>{inner_body}</tspan>")
                                i = inner_close_end
                                local_changes += 1
                                break
                    else:
                        # No matching close; bail out.
                        out.append(src[i:])
                        break
                    continue
                # Outer <text>: keep as-is, increment depth.
                out.append(src[i:open_match.end()])
                i = open_match.end()
                depth += 1
            else:
                # </text> wins.
                out.append(src[i:close_match.end()])
                i = close_match.end()
                if depth > 0:
                    depth -= 1
        return "".join(out), local_changes

    # Run repeatedly so multi-level nesting collapses fully.
    for _ in range(4):
        content, did = _scan_once(content)
        changes += did
        if did == 0:
            break
    return content, changes


def _replace_html_spans_in_text(content: str) -> tuple[str, int]:
    """Convert HTML ``<span>`` runs inside SVG ``<text>`` to ``<tspan>``.

    Browsers may parse ``<span>`` inside inline SVG as HTML flow content,
    causing the span text to escape the slide and render as a huge page-level
    line. SVG text styling must use ``<tspan>`` instead.
    """
    changes = 0

    def _rewrite_text_block(match: re.Match[str]) -> str:
        nonlocal changes
        open_tag, body, close_tag = match.groups()
        span_opens = len(_SPAN_OPEN_RE.findall(body))
        span_closes = len(_SPAN_CLOSE_RE.findall(body))
        if span_opens == 0 and span_closes == 0:
            return match.group(0)
        changes += span_opens + span_closes
        body = _SPAN_OPEN_RE.sub(r"<tspan\1>", body)
        body = _SPAN_CLOSE_RE.sub("</tspan>", body)
        return f"{open_tag}{body}{close_tag}"

    return _TEXT_BLOCK_RE.sub(_rewrite_text_block, content), changes


def _repair_quoted_font_family_attrs(content: str) -> tuple[str, int]:
    """Remove CSS inner quotes from malformed SVG font-family attributes.

    LLMs often emit XML-invalid stacks such as
    ``font-family="SF Pro", "PingFang SC", sans-serif"``. Browsers recover,
    but XML post-processing truncates the tag. Keep the font stack semantics
    while making the attribute well-formed.
    """
    changes = 0

    def _rewrite(match: re.Match[str]) -> str:
        nonlocal changes
        raw = match.group(1)
        cleaned = raw.replace('"', "")
        cleaned = re.sub(r"\s*,\s*", ", ", cleaned).strip()
        if cleaned != raw:
            changes += 1
        return f'font-family="{cleaned}" '

    return _FONT_FAMILY_ATTR_RE.sub(_rewrite, content), changes


def _remove_svg_animation(content: str) -> tuple[str, int]:
    """Strip SVG/CSS animation features that PPTX cannot reproduce."""
    changes = 0
    content, element_changes = _ANIMATION_ELEMENT_RE.subn("", content)
    changes += element_changes
    content, keyframe_changes = _KEYFRAMES_RE.subn("", content)
    changes += keyframe_changes
    content, declaration_changes = _CSS_ANIMATION_DECL_RE.subn("", content)
    changes += declaration_changes
    return content, changes


def _sanitize_xml_comments(content: str) -> tuple[str, int]:
    """Rewrite XML comments that contain forbidden ``--`` runs."""
    changes = 0

    def _rewrite(match: re.Match[str]) -> str:
        nonlocal changes
        body = match.group(1)
        if "--" not in body and not body.rstrip().endswith("-"):
            return match.group(0)
        cleaned = re.sub(r"-{2,}", " - ", body)
        cleaned = re.sub(r"\s+", " ", cleaned).strip(" -")
        if cleaned == body:
            return match.group(0)
        changes += 1
        return f"<!-- {cleaned} -->" if cleaned else ""

    return _COMMENT_RE.sub(_rewrite, content), changes


def _repair_line_endpoint_attrs(content: str) -> tuple[str, int]:
    """Repair common ``<line>`` endpoint typos before XML parsing.

    LLMs often write ``x1 y1 x2 y1`` when drawing many edges. XML rejects this
    as a redefined attribute, and recovery parsers may silently drop the second
    endpoint. When a line has a duplicate start-coordinate attribute and the
    corresponding end-coordinate is absent, treat the second copy as x2/y2.
    """
    changes = 0

    def _rename_second(tag: str, attr: str, replacement: str) -> str:
        nonlocal changes
        seen = 0

        def _rewrite(match: re.Match[str]) -> str:
            nonlocal changes, seen
            seen += 1
            if seen == 2:
                changes += 1
                return f"{match.group(1)}{replacement}{match.group(2)}"
            return match.group(0)

        return re.sub(rf"(\s){re.escape(attr)}(\s*=)", _rewrite, tag)

    def _rewrite_tag(match: re.Match[str]) -> str:
        tag = match.group(0)
        names = [m.group(1) for m in _ATTR_NAME_RE.finditer(tag)]
        if names.count("x1") >= 2 and "x2" not in names:
            tag = _rename_second(tag, "x1", "x2")
            names = [m.group(1) for m in _ATTR_NAME_RE.finditer(tag)]
        if names.count("y1") >= 2 and "y2" not in names:
            tag = _rename_second(tag, "y1", "y2")
        return tag

    return _LINE_TAG_RE.sub(_rewrite_tag, content), changes


def _float_attr(attrs: str, name: str) -> float | None:
    match = re.search(_ATTR_RE_TEMPLATE.format(name=re.escape(name)), attrs, re.IGNORECASE)
    if not match:
        return None
    try:
        return float(match.group(1))
    except ValueError:
        return None


def _plain_text(body: str) -> str:
    text = _TAG_RE.sub("", body)
    return re.sub(r"\s+", " ", text).strip()


def _remove_top_left_admin_labels(content: str) -> tuple[str, int]:
    """Remove generated page/chapter chrome labels from the top-left header.

    The footer may still contain a normal ``N / total`` page number. This only
    targets non-semantic labels such as ``SECTION 03`` or ``第 7 页`` placed in
    the header, which have been a common source of inconsistent deck chrome.
    """
    changes = 0

    def _rewrite(match: re.Match[str]) -> str:
        nonlocal changes
        attrs = match.group("attrs")
        x = _float_attr(attrs, "x")
        y = _float_attr(attrs, "y")
        if x is None or y is None or x > 280 or y > 95:
            return match.group(0)
        text = _plain_text(match.group("body"))
        if _ADMIN_HEADER_RE.fullmatch(text):
            changes += 1
            return ""
        return match.group(0)

    return _TEXT_ELEMENT_RE.sub(_rewrite, content), changes


def repair_svg_file(svg_path: Path) -> int:
    """Repair common malformed XML patterns in-place.

    Returns 1 when the file was modified and became parseable, else 0.

    Run order matters:
      1) Always pre-rewrite illegally nested ``<text>`` to ``<tspan>``
         even if the file *parses* — lxml's recover mode silently
         flattens nested text into broken sibling runs, so we cannot
         rely on a parse-error signal here.
      2) Standard malformed-XML repairs (entity escaping, stray tspan
         close tags, etc.) that only run when the file does NOT parse.
    """
    content = svg_path.read_text(encoding="utf-8")

    # Step 1: always run raw text fix-ups. ``lxml`` would otherwise accept
    # malformed/invalid input silently and downstream finalizers would see
    # broken sibling runs or leaked HTML flow content.
    repaired, nested_fixes = _unnest_text(content)
    repaired, span_fixes = _replace_html_spans_in_text(repaired)
    repaired, font_fixes = _repair_quoted_font_family_attrs(repaired)
    repaired, animation_fixes = _remove_svg_animation(repaired)
    repaired, comment_fixes = _sanitize_xml_comments(repaired)
    repaired, line_attr_fixes = _repair_line_endpoint_attrs(repaired)
    repaired, header_label_fixes = _remove_top_left_admin_labels(repaired)
    parses_now = True
    try:
        ET.fromstring(repaired)
    except ET.ParseError:
        parses_now = False

    if parses_now:
        if repaired != content:
            svg_path.write_text(repaired, encoding="utf-8")
            return 1
        return 0

    # Step 2: standard malformed-XML repairs.
    repaired = _AMP_RE.sub("&amp;", repaired)

    previous = None
    while previous != repaired:
        previous = repaired
        repaired = _TEXT_CLOSE_RE.sub(r"\1</text>", repaired)

    try:
        ET.fromstring(repaired)
    except ET.ParseError:
        try:
            parser = etree.XMLParser(recover=True)
            recovered_root = etree.fromstring(repaired.encode("utf-8"), parser=parser)
            repaired = etree.tostring(recovered_root, encoding="unicode")
            ET.fromstring(repaired)
        except Exception:
            return 0

    if repaired != content:
        svg_path.write_text(repaired, encoding="utf-8")
        return 1
    return 0
