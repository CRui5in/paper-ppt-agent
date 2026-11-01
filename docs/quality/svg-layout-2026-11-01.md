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

## First cross-paper pass

The local corpus contains nine PDFs covering latent augmentation, Chinese remote-sensing research, forest point-cloud models, attention architectures, and a lake-bloom time series. The evaluation generates three-page fixtures from numbered source excerpts, then runs the production design and SVG generators. It saves source hashes, prompts, responses, previews, critic events, native export audits, and a separate model-assisted fidelity review.

The first pass produced 27 SVG pages. Eight three-page exports passed; one page used a group-level filter and fell back to a raster slide. This exposed a missing native-export check when style review was disabled. The repair path now includes native-incompatible filters and elements.

A Chinese formula also exposed missing glyphs in the default mathematical font. CJK formula labels now select an installed font with distinct glyph coverage. The math renderer signature was versioned so previously prepared equations regenerate with the corrected font.

Validation after these fixes: 66 tests passed. The corpus script is `scripts/evaluate_svg_layout.py`; credentials are read from an environment variable or password prompt and are not stored in artifacts. Model-assisted fidelity findings require review: the first reports included both demonstrated invented chart points and incorrectly categorized supported values.

## Second pass changes

- Repair includes text collisions and opaque shapes covering text, in addition to capacity, canvas, container, and native-conversion failures.
- Each design brief names its actual visual material: observation records, an assigned figure, or the components and relationships of a schematic. Qualitative descriptions use labeled relationships or stated milestones.
- Structural-page information is scoped to the page role; detailed explanatory prose accompanies the speaker notes.
- Inline emphasis and scripts share their parent text chunk's anchor. A reproduced end-aligned line previously measured past its x=400 anchor; it now ends at x=400. Native script fragments use the same chunk offset.
- 69 unit tests passed, and the original 41-slide native export passed again after the anchor change.

The second corpus pass reuses the first pass's manuscript fixtures (`--reuse-fixtures`) to separate SVG changes from new source summarization.

## Visual-evidence review

The second pass exported all nine samples natively, but inspecting the SVGs still found invented annual trend curves and stacked observations specified by the planner from qualitative prose. Adding a material field to the design brief did not resolve that contradiction.

Design generation now includes a focused visual-evidence review before drawing. It replaces unsupported visual treatments with supported relationships or milestone timelines. The patcher changes only visual fields, preserving slide titles, roles, source content, and document sections. Assigned figure inventory is supplied separately from the proposed specification. An unapplied review is recorded in debug output.

The evaluation script records the Git revision and source hashes at startup. `--recheck` reruns rendering and native audits without API calls, allowing different sample sets to be measured with the same checker. Validation before the next corpus run: 72 tests passed.

## Mixed inline layout and selected table records

The third pass exported all 27 pages natively. Remaining defects included paragraphs whose mixed sizes or inline scripts disabled wrapping, content obscured by later shapes, and a source table reduced to a few rows.

Paragraph flow now preserves script offsets and mixed font sizes during wrapping. Base symbols stay with their scripts. Numeric values selected into manuscript tables are checked against visible text and equation metadata, separately from SVG coordinates. Missing records trigger targeted repair in both sequential and parallel paths. This detects omissions; it does not establish every semantic claim or row-to-value association.

Writing guidance preserves ties and comparison conditions when summarizing tables. Validation: 76 tests passed. The evaluator can reuse design briefs and replay existing initial SVGs, sending only detected repairs to the model. This isolates renderer and repair changes from new layout generation.

## Repair context and rendering parity

The replay pass exposed two truncated repairs. A page containing several equations sent approximately 65K input tokens to the model and reached its 8,192-token output limit. The application had expanded LaTeX into glyph paths before inserting the SVG into the conversation. That forced the model to copy renderer-generated vector data while repairing layout.

Model-facing SVG now keeps `data-latex` and the math box while omitting generated glyph children. The renderer regenerates identical vector paths after repair. The reproduced source page shrinks from 108,274 to 15,010 SVG characters in repair context, without changing the drawing's formula source.

Repaired candidates now receive the same font normalization, script conversion, paragraph flow, and math preparation as initial candidates before the next geometry check. Preview also resolves concrete fonts before measuring paragraphs. Repair directions permit coordinated recomposition of affected regions while preserving content and the deck's visual identity. Raw repair responses are retained in debug output, including whether a complete SVG was extracted.

Validation before replay: 78 tests passed, including both generation modes and identical vector reconstruction from compact mathematical authoring markup.

## Identified XML repairs

Compact context reduced the reproduced repair request from 64,508 to 12,410 input tokens and changed its completion from a truncated 8,192 tokens to a complete 6,015 tokens. Complete-SVG repairs nevertheless left some reported regions unchanged.

Repair now uses a separate layout-editor request and an identified XML edit list. Coordinates, region replacements, and appended table rows can change without copying the rest of the page. Edits apply atomically to the current authoring tree, preserve its assigned viewBox, and then pass through normal rendering and checks. Existing full-SVG repair responses remain accepted for compatibility. Critic findings identify both the affected text and an occluding shape by editable element ID.

Equation/text collisions now use the actual typeset glyph bounds, rather than assuming the whole reserved math box is occupied. Validation before the focused replay: 83 tests passed, including unknown targets, atomic failure, namespace preservation, canvas preservation, appended records, and an end-to-end JSON repair.

## Repair response validation

The first XML-edit replay reduced the three repair completions to 554–1,276 tokens (2.5–4.5 seconds), but exposed overescaped XML attributes and a proposed zero-height math box. Fragment decoding now recovers escaped quotes after an XML parse failure. Equation edits are rendered on the candidate tree before acceptance; invalid patches are rejected atomically, preserving the last valid slide. The repair example uses single-quoted XML attributes.

Validation: 85 tests passed, including escaped fragments, preserved LaTeX, and invalid equation edits. The replay still has unresolved layout and source-table omissions; response validity is measured separately from successful composition.

## Bounded follow-up edits

The replay exposed a repair-control defect: partial fixes were accepted after one attempt, and a valid but worse candidate could replace the previous page. Repair now measures outstanding source records, geometry defects, and paragraph capacity pressure before accepting a candidate. Improvements can receive a follow-up, bounded to three basic repair calls. A rejected edit gets one correction request containing the actual validation error; the original page stays current. Unchanged results retain the existing single-pass bound.

Validation: 86 tests passed. Entry-point tests exercise partial improvement, a capacity regression, invalid edit targets, bounded unchanged results, and continuation of the deck in both sequential and parallel modes.

## SVG transform-aware checks

Geometry checks now compose SVG affine transforms and inherited text styles before measuring canvas overflow, text collisions, local containers, occluding shapes, and equation/text intersections. Measurements stay separate from the authored tree. Footer checks follow the declared footer, including transformed footer lines; pages without a footer no longer receive an invented 100px footer band.

Validation: 88 tests passed. New regressions cover translated occluders, transformed equations, nested scale with inherited font size, and translated footer boundaries. Remaining visual defects are still checked independently of export success.

## Footer occupancy and equation retention

Inspecting the replay PNGs found a relocated footer drawn through a diagram, despite a zero-error report. The check now includes content beginning below the footer boundary and excludes the footer's own elements. Footer occupancy is part of basic geometry repair. Changed candidates with equal defect counts can receive another bounded edit, since one repaired region can expose another at the same count. Repeated identical candidates still stop.

Both edit-list and complete-SVG repairs retain existing mathematical expressions and validate their renderability before acceptance. Validation: 89 tests passed, including content entirely below the footer and equation deletion in both response formats.

## Regression correction: source-table forcing and grid flow

The v9 replay demonstrated a regression introduced by the numeric-membership gate. One three-page fixture supplied roughly fifty Markdown table lines on its results page. The gate treated every source number as mandatory visible content, and the repair score allowed added numbers to compensate for new overlap and canvas errors. The resulting oversized table was worse than the initial composition.

Removed that gate from production repair. Numeric membership remains an evaluation diagnostic, separate from decisions about selected slide content. Repair acceptance now compares each geometry error category and paragraph capacity independently: correcting capacity cannot pay for a new overlap or footer violation.

Paragraph flow also leaves independently positioned table columns intact, including tables nested inside an outer text box. Explicit diagram, chart, figure, and table groups establish a separate positioning scope. This prevents the paragraph engine from stacking independently authored cells.

Validation: 91 tests passed. Added regressions preserve table-cell coordinates and reject a capacity-to-overlap trade. The earlier v9 native-export success did not establish layout quality; its rendered pages exposed the regression described here.

## Complete acceptance reports

Geometry reports retain all findings for candidate acceptance and saved audits. Only the model-facing repair excerpt is capped at twenty findings. Previously the same cap also truncated the score, allowing different heavily broken candidates to appear equal. Validation: 92 tests passed, including twenty-five simultaneous canvas violations with a bounded model prompt.

## Stop inferred label rewrapping

The preparation pass still inferred text regions from nearby rectangles and design-spec bounds. It then wrapped independently positioned labels and reduced their font size by up to 18%, without recomposing neighboring elements. That is a direct source of new stacking and inconsistent line breaks.

Preparation now applies only the declared paragraph flow, opted-in label/header layout, font resolution, and mathematical rendering. Positioned labels keep their coordinates and font sizes. Geometry findings can request a coordinated model repair rather than mutating labels through inferred ownership.

Validation: 93 tests passed. A long positioned annotation remains a single text element at its authored baseline and size. Existing paragraph, script, table, equation, and export regressions pass.
