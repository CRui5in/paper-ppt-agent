# SVG Native Export Contract

Use the exact presentation viewBox and inline SVG attributes.

The generated document uses well-formed XML element markup. Use descriptive group IDs for organization, keep implementation notes outside the SVG, and escape reserved characters in text and attribute values.

## Drawing vocabulary

- Native shapes: `rect`, `circle`, `ellipse`, `line`, `polyline`, `polygon`, and `path` with line, Bezier, and arc commands. Use them for diagrams, plots, connectors, annotations, and illustrations that explain the source material.
- Text: `text` and inline `tspan`, with a system font stack that covers the requested language. Continuous body/caption/callout paragraphs use a parent `g` with `data-textbox="x y width height"` and an appropriate `data-role` for measured wrapping. Diagram and data labels can be positioned directly.
- Text flows: `g data-layout="text-flow" data-flow-box="x y width height" data-gap="12"` positions its direct `text` children vertically using measured font metrics, wrapping, and the declared gap. Children carry their typography and inline `tspan` styles; backgrounds and graphics are separate siblings. Frame dimensions remain fixed and excess capacity is reported.
- Inline scripts: use `tspan` with a relative `font-size` and `baseline-shift="sub"` or `baseline-shift="super"`; the surrounding text keeps its baseline.
- Math: `g data-latex="..." data-math-box="x y width height"`; expressions are typeset as vector paths during preparation.
- Images: assigned existing local paths via `image href="..."`, preserving the complete paper figure with `preserveAspectRatio="xMidYMid meet"`.
- Styling: solid HEX colors, fill/stroke opacity on individual shapes, dashed strokes, linear and radial gradients in `defs`. Use distinct semantic or series colors when needed.
- Simple shape shadows: `feDropShadow` referenced directly by one shape. Filters on groups, glow, and complex blur effects require unsupported rendering behavior.
- Logical `g` groups with descriptive IDs organize editable geometry.

## Connector arrowheads

`marker-start` and `marker-end` on a line or path can map to native arrows. Define the marker in `defs` with `orient="auto"`; use one triangle, diamond, or oval child, matching the connector's stroke color, with marker dimensions between 3 and 15. Use a filled polygon for custom arrow geometry.

```xml
<defs>
  <marker id="arrow" markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto">
    <path d="M0,0 L8,4 L0,8 Z" fill="#2B6CB0"/>
  </marker>
</defs>
<path d="M100,220 C220,220 220,360 340,360" fill="none" stroke="#2B6CB0" stroke-width="2" marker-end="url(#arrow)"/>
```

## Export boundary

Keep final SVG static and use inline styling. CSS stylesheets/classes, external CSS, `foreignObject`, scripts/events, animation, masks, clip paths, patterns, text paths, and arbitrary `symbol`/`use` references are outside the native conversion contract. Assigned icon placeholders are expanded before conversion. Express the design with supported geometry instead.

Use `fill-opacity` / `stroke-opacity` on the actual shapes rather than group opacity or rgba colors. Keep essential labels and images within the assigned content boundary and clear of footer text. Full-canvas backgrounds and accents can extend to the canvas edges.

## Delivery pipeline

Write raw pages to `svg_output/slide_###.svg`. The backend prepares equations, measured paragraphs, icons, and images, then finalizes to `svg_final/` before native PPTX export. After changing a source SVG, rerun preparation/finalization and export.
