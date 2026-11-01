# Output Contract

Required files:

- `manuscript.md`: slide-by-slide manuscript.
- `design_spec.md`: visual system, template usage, slide inventory, and per-slide design intent.
- `svg_output/slide_001.svg`, `svg_output/slide_002.svg`, ...: final authoring SVGs.
- `agent_report.json`: machine-readable summary.

Optional files:

- `notes/slide_001.md`, ...: speaker notes matched by SVG stem.
- `qa/*.md` or `qa/*.json`: review results.

Conditionally required before authoring any deck output:

- When external research is enabled: `research/raw_external_results.json`, `research/sources.json`, and `research/brief.md`. The search helper also writes `research/external_search_summary.json`; read that before sampling raw results.
- When deep research is enabled: `research/deep/notes_index.json` and `research/deep/brief.md`.
- The backend research gate rejects manuscript, design, notes, report, and slide SVG writes until the enabled research requirements are complete.

`agent_report.json` should be valid JSON:

```json
{
  "status": "complete",
  "slides": 12,
  "language": "zh",
  "external_research_used": false,
  "deep_research_used": false,
  "detail_level": "normal|high|very_high",
  "detail_profile_followed": true,
  "icon_policy": {
    "local_icons_searched": true,
    "purposeful_usage": true,
    "consistent_vocabulary": true,
    "assets_used": ["relative/path/icon.svg"],
    "substitutes_used": false,
    "notes": "Icons clarify repeated concepts and follow the deck palette."
  },
  "subagents": [
    {
      "name": "paper-extraction",
      "used": true,
      "purpose": "Extract method/results facts",
      "skip_reason": null
    }
  ],
  "paper_figures_used": [
    {
      "slide": 4,
      "id": "pdf_fig_001_p3_abcd",
      "href": "../source_assets/images/pdf_fig_001_p3_abcd.png",
      "caption": "Figure 1. Method overview"
    }
  ],
  "layout_qa": {
    "region_plans_followed": true,
    "text_capacity_preflight": true,
    "occupancy_or_wrapping_repairs": [3, 7],
    "remaining_limitations": []
  },
  "slide_authoring": {
    "mode": "direct_per_slide",
    "multi_slide_generator_used": false,
    "direct_files": ["slide_001.svg", "slide_002.svg"],
    "notes": "Each slide was authored from its own claim, evidence, and layout."
  },
  "visual_qa": "passed|skipped|failed",
  "notes": ["short summary"]
}
```

SVG requirements:

- Root element must be `<svg>`.
- `viewBox` must match `agent_task.json.presentation.viewbox`.
- Use filenames `slide_001.svg`, `slide_002.svg`, etc.
- Use UTF-8 text.
- Use local relative image/icon references only.
- Use inline SVG styling only. Do not include `<style>` blocks or `class=` attributes.
- Paper figure references must use exact `href` values from `source_assets/figures.json` or `source_assets/figures.md`.
- Keep all visible content inside the canvas.
- Content slides should have a well-designed layout with major elements aligned to a consistent grid instead of being freely scattered.
- For Chinese text, avoid repeated short orphan lines and excessive blank space caused by overly narrow text boxes.
- Every slide SVG must be directly authored as its own file. Multi-slide generator programs, loops, shared renderers, slide registries, and template expansion are forbidden.
- Preserve source number precision and units; do not introduce K/M/B shorthand or rounded metrics unless the source uses them.
- Do not include `<style>`, `class=`, masks, clip paths, script tags, event handlers, remote network URLs, or `foreignObject`.
- Optional filters are limited to simple convertible drop shadows attached directly to one shape. Group filters, text glow, complex blur effects, and offset duplicate rectangles used as shadows are prohibited.
- Do not use text glyphs (letters, emoji, Unicode symbols, arrows, stars) as fake icons. Use existing local icon assets or omit the icon.
