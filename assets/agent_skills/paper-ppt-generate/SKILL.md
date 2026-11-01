---
name: paper-ppt-generate
description: Generate an academic PowerPoint deck from a PDF or LaTeX source inside Paper PPT Agent's Agent-mode workspace. Use this when the task asks Claude Code or Codex to analyze a paper, plan a slide deck, author SVG slides, perform research/QA with subagents where useful, and write artifacts that the backend can preview and export.
---

# Paper PPT Generate

You are working inside a Paper PPT Agent generation workspace. The backend has
prepared `agent_task.json`, the uploaded paper source, template assets, icon
assets, and output folders. You own paper understanding, research, deck
planning, slide SVG authoring, and ordinary QA. The backend only watches files,
validates SVGs, finalizes them, and exports PPTX.

## Required Workflow

1. Read `agent_task.json`.
2. Read only the references you need:
   - `skills/paper-ppt-generate/references/output-contract.md` for required files and SVG rules.
   - `skills/paper-ppt-generate/references/slide-authoring.md` before writing SVG.
   - `skills/paper-ppt-generate/references/writing.md` before writing manuscripts, speaker notes, or reports.
   - `skills/paper-ppt-research/SKILL.md` when external research is enabled.
   - `skills/paper-ppt-deep-research/SKILL.md` when deep research is enabled.
3. If `agent_task.json.presentation.template_id` is set, inspect the selected
   template directory in `agent_task.json.paths.selected_template` before
   planning the deck. Read its `design_spec.md`, page SVG skeletons, and
   `template_context.json`; preserve its page chrome, coordinate system,
   colors, typography, and structural conventions unless the user explicitly
   asks otherwise.
4. Inspect the paper source in `sources/`. Use commands/scripts as needed.
   For PDF sources, do not use the Read tool directly on `*.pdf`: it is a
   binary file and the tool may falsely report that the PDF is password
   protected. Use the Python interpreter from `agent_task.json.paths.python`
   or `PAPER_PPT_PYTHON` with a PDF library such as PyMuPDF (`fitz`) first,
   and only call the PDF password-protected if `doc.needs_pass` is true.
   Before drafting content, read `source_assets/paper.md` as the complete
   extracted paper, not merely as a summary, and read
   `source_assets/figures.md`/`figures.json` if present. If they are missing,
   stale, or `source_assets/extraction_error.json` exists, run:
   `"<python>" skills/paper-ppt-generate/scripts/extract_paper_assets.py --task agent_task.json --out source_assets`
   using `agent_task.json.paths.python` or `PAPER_PPT_PYTHON`.
   During long work, periodically check `agent_feedback/` for user guidance.
5. Write `manuscript.md` as slide-structured content.
   Slide 1 is the cover. Slide 2 is a mandatory table of contents whose
   chapter titles exactly match the final chapter plan.
   Choose chapter count and slide count so every TOC chapter has content
   slides that develop it.
6. Write `design_spec.md` with deck-level visual rules and per-slide intent.
7. Write each slide as soon as it is usable:
   - `svg_output/slide_001.svg`
   - `svg_output/slide_002.svg`
   - etc.
   Author each file directly and independently. Do not create or run a general
   script/program that generates multiple slide SVGs.
8. Optionally write speaker notes in `notes/slide_001.md`, etc.
9. Write `agent_report.json` when finished.

## Agent Behavior

- Do not call Paper PPT Agent's provider model pipeline or request provider API keys.
- Treat `agent_task.json.presentation.user_instruction` and newer `agent_feedback/` guidance as the highest product requirements within SVG/PPTX export validity. Preserve the user's intended content and visual direction when choosing the implementation.
- Translate the user's visual direction into valid output artifacts. For an explicit image-led request such as "只用图像生成" or "GPT image", use local/generated raster assets inside SVG slide wrappers. For "更美观", improve hierarchy, spacing, typography, and composition according to the selected style.
- Use `agent_task.json.presentation.detail_profile` only for slide-count planning and source/summary retrieval budgets. It must not determine per-slide density, bullet count, evidence count, typography, or layout.
- Follow `agent_task.json.agent_policy.slide_authoring_policy` as a hard contract. Write one explicitly named slide SVG at a time. Do not create or run Python, JavaScript, TypeScript, shell, PowerShell, batch, Ruby, or other code that emits multiple slides.
- Do not use loops, slide arrays/registries, shared `base_svg`/`title`/card renderers, or template expansion to generate the deck. Shared palette, typography, margins, and recurring chrome are allowed only as documented visual tokens; every slide's complete composition and markup must remain explicit.
- Final slide SVGs must use inline SVG attributes. Do not use `<style>` blocks or `class=` attributes; place `fill`, `stroke`, `font-family`, `font-size`, and related styling directly on the element.
- Scripts may parse the paper, retrieve research, inspect or validate SVGs read-only, render previews, and export PPTX. They may not write or regenerate slide SVGs.
- Follow `agent_task.json.agent_policy.layout_policy` during generation, not only during review. Design each slide's spatial layout when generating that slide, based on its content, figures, and layout family.
- Choose the composition from the page's material. Native paths, curves, connectors, and direct annotations can explain relationships; shared typography, spacing, and palette keep the deck coherent. Layout families are suggestions within explicit template boundaries.
- **Visual hierarchy**: establish hierarchy through typography, contrast, alignment, and spacing. Use shadows, gradients, and accent bars when they serve the selected template or clarify grouping. A flat layout is valid.
- **Card design**: use clear grouping and proper inner padding. Optional shadows must be simple convertible drop shadows attached directly to one shape; group filters, text glow, complex blur effects, and offset duplicate rectangles used as shadows are prohibited.
- **Content emphasis**: use emphasis, labels, and grouping where they clarify a result, comparison, or explanation. Choose these elements from the current page's material.
- **Color**: retain consistent meanings across the deck and distinguish series when needed.
- Before drawing a slide, plan the layout regions explicitly. Budget heading height, line-height, caption space, and padding. Reflow only when clipping or overlap would occur.
- Size table columns from the longest visible method/label/cell before placing any row text. A long method name must never collide with the first numeric column.
- Wrap Chinese body text dynamically from each region's actual width, font size, visual share, and current-page density. Avoid conservative early breaks, use semantic boundaries, and allow light raggedness when it improves composition.
- When a paper figure supports a slide, reserve the figure frame first and preserve the figure aspect ratio inside that frame. Arrange captions and explanations around the visible image according to its role on the page.
- Follow `agent_task.json.agent_policy.factual_rendering_policy`. Keep source numbers and units exact; preserve the paper's original notation and precision. Preserve metrics, sample sizes, dates, and model settings with their source precision. Charts may derive plot coordinates and scale guides from supplied values. Label derived values and show their inputs/calculation nearby.
- Use `paper-ppt-deep-research` for deep work. When `allow_deep_research` is true, run that skill and produce `research/deep/notes_index.json` and `research/deep/brief.md` before authoring any deck output. Launch focused research SubAgents with the Task/SubAgent tool if it is available; skip only if the runtime has no such tool or it fails, and record the concrete reason in `agent_report.json.subagents`. When Agent review is enabled, start one focused reviewer after the first full deck draft; review the whole deck for layout overflow, missing assets, icon-policy violations, and narrative gaps instead of checking every page one by one. The main Agent remains responsible for the final story, visual consistency, and artifact integration.
- Generate slides in parallel when useful; keep consistency through `design_spec.md`.
- Before authoring a chapter slide, read at most the two most recent earlier chapter-slide SVGs. Before authoring a content slide, read at most the two most recent earlier content-slide SVGs and skip structural pages. Use those SVGs only for visual continuity; the current manuscript remains authoritative.
- Icons are optional visual aids. Add them only when they clarify the content (e.g., process steps, comparison dimensions, legend keys). Do not add icons as filler decoration.
- Use filesystem commands against `agent_task.json.paths.icons` to find real local `.svg` files. Verify the file exists before referencing it. Never invent icon names, use remote URLs, or use text glyphs (letters, emoji, Unicode symbols, arrows, stars) as icon substitutes.
- Define a small icon vocabulary in `design_spec.md` before drafting slides. Most content slides should use 0-3 icons. Color icons from the deck palette.
- Flow connectors must be real SVG geometry (`line`, `path`, `polyline`), not text glyph arrows.
- Use external research only when `agent_task.json` allows it. Run `paper-ppt-research` after reading the paper; choose search queries yourself from paper understanding, then produce `research/raw_external_results.json`, `research/sources.json`, and `research/brief.md` before authoring any deck output. Read `research/external_search_summary.json` before sampling raw search output, and treat documented sparse/no relevant results as a valid completion state.
- Treat `agent_task.json.agent_policy.research_gate` as mandatory. When enabled, the backend will reject writes to manuscript, design, notes, report, and slide SVG files until the required research artifacts exist.
- If deep research is enabled, run `paper-ppt-deep-research`, split work across focused readers/SubAgents where supported, and merge the synthesis into the manuscript.
- If Agent review is enabled, perform a whole-deck review after the first complete draft. Use multimodal inspection only when the runtime supports it; otherwise perform XML/static review and record the limitation.
- For any Python command, prefer `agent_task.json.paths.python` or `PAPER_PPT_PYTHON` over system `python`; this keeps PDF parsing, SVG checks, and export helpers in the backend environment where required packages are installed.
- Treat `source_assets/figures.json` as the paper-figure contract. It gives each figure's `id`, exact SVG `href`, caption, PDF page/bbox when available, surrounding context, and natural dimensions. Select figures by caption/page/context, not by filename alone.
- Use paper figures in content slides whenever they directly support the slide argument. In SVG, reference the exact manifest `href` such as `../source_assets/images/pdf_fig_001_p3_abcd.png`; preserve the listed aspect ratio.
- For TeX archives, use the source graphics discovered from `\includegraphics`; do not screenshot every archive image. PDF graphics referenced by TeX may already be rendered into `source_assets/images/`.

## Live Preview

The backend watches `svg_output/`. Save each first-completed slide SVG
there immediately. Rewriting an existing `slide_###.svg` updates the app's live
preview.

## Quality Bar

- Every SVG must parse as XML and define a correct canvas `viewBox`.
- Do not use `<style>`, `class=`, `mask`, `clipPath`, `foreignObject`, scripts, event handlers, or remote URLs in final SVGs.
- Keep all needed assets local or embedded by relative path.
- Avoid external image URLs in final SVGs.
- Audit the final SVGs for icon-policy violations: icons are purposeful rather than decorative, the icon vocabulary is consistent, colors follow the deck palette, and there are no fake icon letters/symbols/emoji, made-up icon files, or missing local icon references.
- Audit for text glyph symbols used visually, including exclamation marks, stars, checkmarks, and arrow characters. Replace them with real local icon SVGs or SVG geometry before finishing.
- Preserve template chrome when a template is provided.
- Prefer clear academic storytelling over dense paper transcription.
- Audit writing against `references/writing.md`: lead with supported findings, retain material conditions and uncertainty, and remove repeated disclaimers or vague self-criticism.
- Audit layout: major elements should align to a consistent grid, content slides should have balanced occupancy, paper figures should not be stretched, and Chinese line breaks should not create repeated short orphan lines or large dead zones.
- Record first-generation layout findings in `agent_report.json`, with the affected regions and preview images.
- Audit authorship: no generated program may have written multiple slide SVGs, and `agent_report.json.slide_authoring.direct_files` must list every final slide.
- End only when all slides are present and `agent_report.json` explains what was produced, what research was used, what icon assets were used or why icons were omitted, which SubAgent tasks were used/skipped, and whether QA passed.
- Include `paper_figures_used` in `agent_report.json` with figure ids/hrefs/captions used in slides, or explain why no extracted paper figure was suitable.
