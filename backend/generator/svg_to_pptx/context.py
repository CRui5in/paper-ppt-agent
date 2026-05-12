"""Conversion context passed through recursive SVG traversal."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .utils import get_style_attr, parse_svg_ratio


@dataclass
class ConvertContext:
    """Mutable state during SVG→DrawingML conversion."""

    defs: dict[str, Any]  # <defs> elements by ID
    slide_num: int = 1

    # Accumulated transform from parent <g> elements
    translate_x: float = 0.0
    translate_y: float = 0.0
    scale_x: float = 1.0
    scale_y: float = 1.0

    # Inherited styles from parent <g>
    inherited_styles: dict[str, str] = field(default_factory=dict)

    # Media tracking
    media_files: dict[str, bytes] = field(default_factory=dict)
    rel_entries: list[dict] = field(default_factory=list)

    # ID counters
    id_counter: int = 2  # shape ID (1 reserved for spTree)
    rel_id_counter: int = 2  # relationship ID (rId1 reserved for layout)

    # SVG directory for resolving external images
    svg_dir: Path = field(default_factory=lambda: Path("."))

    def next_id(self) -> int:
        """Allocate next shape ID."""
        sid = self.id_counter
        self.id_counter += 1
        return sid

    def next_rel_id(self) -> str:
        """Allocate next relationship ID."""
        rid = f"rId{self.rel_id_counter}"
        self.rel_id_counter += 1
        return rid

    def child(
        self,
        dx: float = 0,
        dy: float = 0,
        sx: float = 1,
        sy: float = 1,
        style_overrides: dict[str, str] | None = None,
    ) -> ConvertContext:
        """Fork context for nested <g> element with accumulated transform."""
        new_styles = dict(self.inherited_styles)
        if style_overrides:
            # Opacity is multiplicative
            if "opacity" in style_overrides and "opacity" in new_styles:
                new_op = parse_svg_ratio(style_overrides["opacity"], 1.0) * parse_svg_ratio(
                    new_styles["opacity"], 1.0
                )
                new_styles["opacity"] = str(new_op)
                style_overrides = {
                    k: v for k, v in style_overrides.items() if k != "opacity"
                }
            new_styles.update(style_overrides)

        return ConvertContext(
            defs=self.defs,
            slide_num=self.slide_num,
            translate_x=self.translate_x + dx * self.scale_x,
            translate_y=self.translate_y + dy * self.scale_y,
            scale_x=self.scale_x * sx,
            scale_y=self.scale_y * sy,
            inherited_styles=new_styles,
            media_files=self.media_files,  # shared reference
            rel_entries=self.rel_entries,  # shared reference
            id_counter=self.id_counter,
            rel_id_counter=self.rel_id_counter,
            svg_dir=self.svg_dir,
        )

    def sync_from_child(self, child: ConvertContext) -> None:
        """Pull updated counters back from a child context."""
        self.id_counter = child.id_counter
        self.rel_id_counter = child.rel_id_counter

    def ctx_x(self, val: float) -> float:
        """Transform x coordinate."""
        return val * self.scale_x + self.translate_x

    def ctx_y(self, val: float) -> float:
        """Transform y coordinate."""
        return val * self.scale_y + self.translate_y

    def ctx_w(self, val: float) -> float:
        """Transform width (scale only)."""
        return val * self.scale_x

    def ctx_h(self, val: float) -> float:
        """Transform height (scale only)."""
        return val * self.scale_y

    def get_attr(self, elem: Any, attr: str, default: str = "") -> str:
        """Get attribute from element, falling back to inherited styles."""
        val = get_style_attr(elem, attr)
        if val:
            return val
        return self.inherited_styles.get(attr, default)
