# Slide SVG Authoring

Author clean SVG for PowerPoint conversion.

Canvas:

- Use the exact `viewBox` from `agent_task.json.presentation.viewbox`.
- Common 16:9 canvas is `0 0 1280 720`.
- Common 4:3 canvas is `0 0 1024 768`.

Layout:

- Keep margins stable across the deck.
- Avoid tiny text. Prefer fewer, clearer points.
- Use template assets in `templates/` when available.
- Author one explicitly named slide SVG at a time. Do not build a deck generator, loop over slide definitions, or emit pages through shared renderer functions. Each page's layout and final SVG markup must be written for that page.
- Shared colors, typography, margins, and recurring chrome may be repeated directly. They are not permission to reuse a universal page skeleton or card grid for unrelated content.
- Put styling directly on SVG elements. Do not use `<style>` blocks or `class=` attributes in final slide SVGs; inline `fill`, `stroke`, `font-family`, `font-size`, opacity, and line attributes instead.
- Use local icons from the configured icons path; verify files exist before referencing them.
- Design each slide around the relationship its audience needs to see. The Slide Plan's layout family is a starting idea; choose the final composition from the material within explicit template boundaries. Use paths, curves, connectors, direct annotations, spatial grouping, and typography to express that relationship.
- Size regions from their text and figures. Use whitespace for separation and hierarchy; keep related edges aligned.
- Align related elements and use spacing to show grouping. Choose gutters from the density and reading order of the page.
- Establish hierarchy through typography, contrast, alignment, and spacing. Use shadows, gradients, or accent bars when they serve the selected style or clarify grouping. Cards need clear grouping and inner padding; a flat layout is valid. Optional shadows must be simple convertible drop shadows attached directly to one shape. Group filters, text glow, complex blur effects, and offset duplicate rectangles used as shadows are prohibited.
- Use emphasis and grouping where they clarify the current page's evidence or explanation.
- Choose ornament for visual balance, atmosphere, and the deck's aesthetic. Build explanatory content from the page manuscript and source figures, and use the design brief for visual treatment. Preserve scientific spatial geometry from its source figure.
- Keep colors consistent in meaning across the deck. Use distinguishable series colors when a chart needs them; use colors according to their roles.
- For figure slides, reserve the figure frame first and fit the image inside that frame preserving aspect ratio. Do not stretch the paper figure to fill a convenient box.
- Give the main visual room to explain the material. Whitespace can separate ideas; a supported mechanism, comparison, or direct figure annotation can reveal relationships that prose obscures. Containers are optional grouping tools.
- Before writing markup, check actual text geometry for each box. Keep the complete glyph bounds inside the region, with room for padding and line spacing.
- Reserve vertical space explicitly: heading, heading-to-body gap, `line_count * line_height`, caption/source, and bottom padding. Do not use an 80px card for a heading plus two body lines merely because the first baseline fits.
- For tables, measure the longest visible cell in each column, assign column widths first, and right-align numeric columns. Do not position rows by guessed x coordinates that let labels and values overlap.

Icons:

- Match icons only after identifying a concrete icon need. Use filesystem commands against `agent_task.json.paths.icons` to list and match available `.svg` filenames directly.
- Do not use `skills/paper-ppt-generate/scripts/search_icons.py`, `index_meta.json`, `index.npz`, icon metadata files, or vector indexes while choosing an icon.
- Allowed icons are existing local SVG/icon files or template-provided vector assets that actually exist. Verify the file exists before referencing it.
- Do not use text glyphs (letters, emoji, Unicode symbols, arrows, stars) as icon substitutes. If no suitable icon exists, omit it or use a neutral shape.
- Keep `design_spec.md` or `agent_report.json` aware of the icon choices so QA can check missing assets and policy violations.

Text:

- Use normal SVG `<text>` elements.
- Give each body paragraph a parent group with `data-textbox="x y width height"` and `data-role="body"`. Write continuous paragraph text and inline `<tspan>` emphasis; the renderer measures fonts and wraps to the declared width. Use separate text elements for separate paragraphs and reserve neighboring regions for figures or captions.
- Paragraphs sharing a declared text box flow vertically in document order and share its capacity. Use separate boxes for independently positioned text.
- For mathematical expressions, use a `<g>` with `data-math-box="x y width height"`, `font-size`, and `fill`. Put the source LaTeX without dollar delimiters in a `<desc data-math="latex"><![CDATA[...]]></desc>` child. CDATA preserves TeX alignment separators and comparison symbols literally. The rendering pipeline typesets it as vector paths inside that box. Preserve the source's indices, limits, operators, and grouping.
- Avoid excessive nested `tspan` complexity.
- Do not use `foreignObject`, `<style>`, CSS classes, masks, clip paths, scripts, event handlers, or remote URLs.
- Wrap against each text box's actual width and font size.
- For Chinese body copy, use the box width and a consistent line height. Align related paragraphs on common left edges. Header labels, titles, and dividers need distinct rows and spacing.
- Mark the header group with `data-role="header"`. Centered labels share their enclosing shape's center and stay within its height.
- If wrapping is clearly wasteful or clips, widen or reallocate the text region, tune gaps, or adjust typography. Do not create huge blank bands with unnecessarily narrow text boxes.
- Preserve exact source values, precision, notation, and units. Charts may calculate plot positions and readable axis ticks from those values; scale guides are distinct from observations. Show derivations with their inputs, and use qualitative diagrams when no numeric data is supplied.

Images:

- Use local relative paths.
- Prefer figures that directly support the slide's argument.
- Avoid remote URLs.
- For paper figures, read `source_assets/figures.json` or `source_assets/figures.md`.
- Use the exact manifest `href` in `<image href="...">`; from `svg_output/` it normally starts with `../source_assets/images/`.
- Preserve the listed natural aspect ratio and fit the complete figure within its box using letterboxing or a resized box. Final Agent SVGs prohibit `clipPath`; keep figure content intact.
- Choose figures by caption, page number, section/context, and slide claim. Do not choose a figure only because the filename looks convenient.

Consistency:

- Write `design_spec.md` before drafting many slides.
- For parallel slide authoring, treat `design_spec.md` as the single source of visual truth.
- For a chapter slide, inspect at most the two latest earlier chapter-slide SVGs. For a content slide, inspect at most the two latest earlier content-slide SVGs, skipping structural pages.
- Structural pages organize titles, metadata, or agenda items. Develop their visual identity through typography, vector illustration, depth, and background composition as the page calls for.
- Content slides should prioritize method, evidence, results, and takeaways.
- Consistency means common visual rules, not identical page construction. Select each content slide's layout from its own evidence shape: figure, process, comparison, table, equation, or argument.

SVG vocabulary:

Use native Bezier/arc paths, polygons, dashed connectors, direct labels, and gradients to communicate relationships. A continuous diagram or open typographic composition is as valid as a grid. Choose geometry for the explanation and group related elements through alignment and spacing.
