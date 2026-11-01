> Current direction: evaluate first-generation composition across the corpus. Automatic SVG repair, post-design rewrite, and whole-slide raster fallback have been removed. Earlier repair experiments below are historical results, not the current generation path.

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

## Actionable collision feedback and visual review

The fresh pass generated 27 pages. All exported natively, but four pages across three papers still contained geometry errors; visual inspection confirmed a paragraph drawn over an attention diagram and overflowing right-column copy. Export success is reported separately from those defects.

Collision feedback previously named only the first text element on a shared baseline. The reproduced repair repeatedly moved the label while missing the paragraph laid over it. Reports now identify both text elements, their visible copy, and the second bounding box.

A further review showed the per-category acceptance rule rejecting a candidate that reduced eight clipped lines to two because it introduced one remaining overlap. Candidate ranking now prioritizes native compatibility, visible clipping and collisions, then declared paragraph capacity. Source-number membership has no contribution to acceptance. A capacity-only fix still cannot compensate for creating a visible overlap. Debug records distinguish decoding success from candidate rejection and retain the actual reason.

Validation: 94 tests passed. The focused replay below evaluates the new feedback against the saved fresh outputs rather than generating a new manuscript.

## Direction correction: first generation

The user explicitly rejected reliance on repair, fallback, and repeated optimization around one sample. Removed the SVG critic-to-edit loops, their XML edit interpreter and prompts, invalid-SVG regeneration requests, the added post-design evidence rewrite, and full-slide raster fallback during native PPTX export. Layout diagnostics observe the first result. Raw SVG is saved separately in `svg_initial` before declared rendering preparation. Native conversion failures identify the failing slide and stop export.

Public generation arguments remain compatible with existing callers; diagnostic settings no longer grant automatic rewrite attempts. Existing font resolution, formula typesetting, explicit paragraph flow, and text-preservation fixes remain rendering capabilities.

Validation: 82 tests passed after replacing deleted repair tests with first-output preservation and single-generation-call checks. Tests verify that both generation modes keep the first response even when layout checks fail, invalid output triggers no regeneration, content design uses no extra review rewrite, and a native conversion failure produces no rasterized PPTX.

## Generation-stage composition

The strategist now writes the visible copy together with its proposed regions, selected table cells, diagram labels, and explanations. Supporting detail has a speaker-notes field. The SVG designer receives that planned `Content` as the visible copy and allocates line heights before placing neighboring regions. This changes the initial handoff, rather than adding a post-generation correction step.

Condensed repeated information instructions in the SVG prompt. The evaluation script now labels results `first_generation`, records generation-call counts, retains initial SVGs, and removes the prior replay-to-repair mode. Fidelity review uses compact equation source instead of renderer-generated glyph paths. Tests: 82 passed.

## Agent-mode parity and first-generation corpus result

Removed the separate Agent-mode contract-repair session as well. Agent mode now records initial artifact/layout findings without starting another authoring session. Its bundled instructions and report example describe first-generation findings rather than repaired slides. Validation: 83 tests passed, including a check that initial Agent findings never instantiate a second agent session.

The first-generation run covers the same nine manuscript fixtures, with new design specifications and three SVGs per paper. All 27 pages used exactly one SVG-generation call and exported natively; no SVG repair calls or raster slide fallbacks occurred. Seven pages still have geometry findings (28 total). A separate audit of the prior raw first responses found eight affected pages (33 findings). The sample is one stochastic run, so this difference does not establish a reliable quality improvement.

Visual inspection also found an unsupported annual time series generated from qualitative bloom-trend prose. That factual defect remains a generation-stage issue. The tested change therefore establishes the requested first-generation workflow, not a claim that layout or factual quality is solved. Artifacts are under `.test-runtime/paper-corpus/v13-first-generation/`; original raw model responses are in `svg_initial/`. Existing user decks were not edited.

## Named records before planning

The first-generation inputs mixed qualitative accounts, scalar facts, and numeric tables in prose. A fresh plan converted qualitative trends into a full annual plot and specified additional series without observation records.

A deterministic source-record catalog now exposes the manuscript's actual Markdown tables to both planning and drawing, with page/table/row IDs and unchanged labels, values, precision, and units. Qualitative prose produces no table records. Quantitative visual briefs select catalog rows or an assigned source figure; SVG chart groups can retain those references as `data-source-records`. This supplies material before the first generation and adds no rewrite or fallback path.

Validation before corpus evaluation: 87 tests passed, including page-scoped identity, cell fidelity, mathematical/code pipes, fenced examples, and qualitative prose without fabricated observations. The next corpus pass measures the first outputs across all nine papers.

## Preserve layout conventions; make material conditional

The user clarified that fixed headers, body/footer regions, component dimensions, and chart-library selection are valid layout conventions. The proposed reference-template rewrite was withdrawn before commit; the original template remains unchanged.

The source catalog is now included only when an actual manuscript table exists. It is optional provenance, not a required paper schema or a mandatory chart format. The analysis prompts organize the paper's actual purpose, reasoning, support, and contribution rather than presuming an architecture/experiment/performance sequence. Evaluation fixture generation selects two coherent source sections rather than requiring method/results sections.

The catalog-only first-generation pass exported all 27 pages natively, but retained geometry defects and unsupported qualitative curves. Its invalid model-assisted review on one paper also obscured that paper's successful geometry/export artifacts in the summary. These results do not establish a quality fix; the source catalog remains a faithful input aid, separate from visual-quality claims.

## Read-only copy measurements before SVG generation

The runtime still asked the designer to render the current page's information completely, while the design brief separately selected visible copy and speaker notes. The runtime now preserves the selected copy's facts and meaning, rather than treating all supporting source prose as on-screen material.

Before the SVG request, selected copy receives measured unwrapped glyph advances using an explicit 18px reference font. Selected Markdown tables receive maximum header/cell advances per column and row counts. The designer can scale these measurements with its chosen typography when allocating regions. The measurements add no paper sections and perform no post-generation text movement, resizing, repair, or fallback. Empty/missing selected copy adds no measurement block.

Validation: 92 tests passed. New cases cover exact values and units, inequalities, literal identifiers, empty copy versus speaker notes, table-column widths, and font-size scaling. Layout conventions and the original reference template remain intact. The next run reuses the same nine manuscript/design pairs to isolate the first SVG request's measurement input.

## Keep audit stages separate

The corpus evaluator now records geometry and native-export outcomes before the advisory model review. An invalid review response is recorded as `fidelity_status=failed`, preserving its error and raw response; it triggers no retry and does not overwrite generation/export evidence. Omission review receives the page briefs and compares visible output with their selected copy, while factual consistency remains checked against the manuscript and source excerpts.

## Conditional content fields

The page-brief schema still requested `Visual relationship` and `Visual material` on every page, including typographic pages. These fields now accompany an explanatory visual when the composition uses one. The visible-copy field includes cells, labels, values, and notation as required by the selected composition. The manuscript format now requires page metadata, title, and visible content, rather than treating highlighted numbers as a per-page item. Narrative roles are descriptive rather than a fixed label menu, and the analysis asks about the assumptions underlying the paper's reasoning rather than presuming data-label construction and inference stages.

The original layout reference, fixed canvas regions, card dimensions, and chart-library conventions remain unchanged. This is a prompt-contract change; it adds no generation retry, repair, or fallback. Validation: all 92 existing tests passed, including design-spec parsing with absent visual-material fields. A fresh model evaluation has not been run for this change; these tests do not establish an improvement in generated visual quality.

## Fresh conditional-content evaluation and declared width

The next first-generation run used all nine source fixtures, fresh designs, and exactly one SVG request per page. All 27 pages exported natively. Eight pages retained 18 geometry errors; visual inspection additionally found data labels drawn in the same color as the bars underneath them. Low-contrast warnings already identified these labels, so a page with zero geometry errors is not evidence that all its text is readable. Unsupported curves drawn from qualitative trend descriptions also remain. Artifacts: `.test-runtime/paper-corpus/v16-conditional-content/`.

The paragraph typesetter abandoned an entire declared flow box whenever its planned height exceeded the box, including fractional-pixel overruns. This left long continuous text unwrapped despite the explicit width request. Declared width and typography now determine wrapping even when the resulting height exceeds capacity. The original box, text, styles, and font size remain intact, and the capacity diagnostic remains present. This implements the paragraph contract; it does not resize boxes, shrink copy, infer label ownership, or start another generation.

An isolated comparison typeset the same 80 parseable raw first outputs from three runs with the previous and current paragraph implementations. All compared outputs retained their text. Geometry errors changed from 21 to 18 across 27 pages in v14, from 31 to 27 across 26 parseable pages in v15, and stayed at 17 across 27 pages in v16. These paired checks start from raw SVGs, rather than the earlier prepared generation outputs; their totals are not interchangeable with the corpus summaries. They test typesetting behavior, not a new model-quality improvement. Artifacts: `.test-runtime/paper-corpus/paragraph-width-comparison/comparison.json`.

Validation: 95 tests passed, including shared-box stacking, fractional capacity overruns, unchanged capacity findings, overwide identifiers, idempotent typesetting, exact copy retention, and native text export for an overfull paragraph.

## Remove remaining runtime correction passes

Preview and final export still invoked legacy XML recovery and sibling-text merging. Finalization additionally inferred icon-label alignment, removed unsupported filters, and restored an earlier checkpoint if later stages changed visible text. Those runtime calls are removed. Invalid XML and text-loss failures now surface directly; unsupported effects remain available to compatibility diagnostics. Declared paragraph/header layout, script semantics, mathematical typesetting, image fitting, and native geometry conversion remain rendering operations.

Validation: 100 tests passed. New end-to-end preview/finalization checks retain coincident labels as separate elements, keep header text and positioned label baselines, preserve filter declarations, report malformed XML without recovery or leaked temporary files, and raise on lost text without checkpoint restoration. This change exposes initial output defects; it does not establish an improvement in first-generation aesthetics.

## Readability priority and measured text-flow capability

Fresh v17/v18 outputs still contained crowded text and competing visual blocks. The v18 reference change removed the instruction to preserve all copy before adjusting typography, established readable type before selection, and retained the original header/body/footer regions, card dimensions, and visualization-library conventions. The source fixtures stayed the same. Visual inspection found remaining diagram-label collisions and repeated table/chart encodings; the prompt change alone did not solve the problem.

Added an explicit initial-typesetting capability, `data-layout="text-flow"` with `data-flow-box` and `data-gap`. The designer chooses a region and its styled text children. The renderer measures their glyphs, wraps lines, and positions successive blocks using their line heights and gap. It handles headings, prose, labels, values, alignment, and inline emphasis without assuming a paper structure. Independent SVG geometry remains available. Frames and type sizes stay fixed; excess capacity is still reported. Existing raw SVGs without this declaration retain their positioning.

The capability is wired through first SVG preparation, preview, final export, and capacity diagnostics. It is initial layout semantics, like declared paragraph wrapping and equation typesetting, and adds no repair request, inferred label ownership, or fallback. Validation: 109 tests passed, including measured separation, centered/end-aligned copy, inherited fonts, transforms, overfull frames, unbreakable identifiers, idempotent compilation, and preview/native text retention. First-output corpus evaluation follows this commit.
