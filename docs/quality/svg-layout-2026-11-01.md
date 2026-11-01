# SVG layout regression checks, 2026-11-01

## Reproduced defects

- A four-line paragraph on page 27 was reported as 91.2px high in a 90px box. The estimated descent (`font_size * 0.25`) exceeded the actual font descent. Measuring the glyphs confirms that the paragraph fits.
- A callout on page 13 contained parent text, an inline superscript, and trailing explanation. The previous bounds checker measured only the direct tspan, omitting the rest of the sentence. Measuring every positioned text run detects the overflow.
- Exhausting paragraph repair raised a RuntimeError and stopped the deck. Remaining layout findings now emit through the page critic callback and archive the candidate while subsequent pages continue.
- An explicit footer at y=660 was ignored in favor of a fixed y=620 boundary. The checker now reads the marked footer geometry; pages without a marked footer retain the previous fallback.

## Validation

- 63 unit tests passed, covering actual glyph capacity, complete inline text bounds, footer boundaries, continuation in sequential and parallel generation, and native text preservation.
- Replayed the failing page 27: its paragraph capacity report passes.
- Replayed page 13: its full callout line now reports `text_overflow_in_container`.
- DeepSeek Flash generated three representative pages from the same manuscript: cover, two-branch method with equation, and attribution workflow. All three passed geometry checks and native export audit. Samples are under `.test-runtime/layout-regression-qa/`.

## Design instructions

Cover ornament uses unlabeled abstract geometry. Data-bearing marks encode supplied values or source assets. Structural schematics explain components, dimensions, and operations. The planner allocates margins and gutters before region widths; the designer sizes paragraphs together with neighboring visuals.

These checks establish specific rendering and export behavior. Cross-paper evaluation tracks visual composition and factual fidelity separately.
