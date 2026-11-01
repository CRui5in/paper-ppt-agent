# {project_name} - Design Spec

> This document is the unified handoff artifact for design definition and execution constraints. It combines visual specifications, content outline, speaker-notes requirements, and implementation boundaries needed by downstream roles.

## I. Project Information

| Item | Value |
| ---- | ----- |
| **Project Name** | {project_name} |
| **Canvas Format** | {canvas_info['name']} ({canvas_info['dimensions']}) |
| **Page Count** | [Filled by Strategist] |
| **Design Style** | {design_style} |
| **Target Audience** | [Filled by Strategist] |
| **Use Case** | [Supplied presentation context, when provided] |

---

## II. Canvas Specification

| Property | Value |
| -------- | ----- |
| **Format** | {canvas_info['name']} |
| **Dimensions** | {canvas_info['dimensions']} |
| **viewBox** | `{canvas_info['viewbox']}` |
| **Margins** | [Recommended by Strategist, e.g., left/right 60px, top/bottom 50px] |
| **Content Area** | [Calculated from canvas] |

### Safe Area & Page Structure

> All content elements MUST be placed within the safe area. The safe area defines the boundary that content must not exceed.

| Canvas Format | Safe Area (x, y, width, height) | Margins (L/R, T/B) |
| ------------- | ------------------------------- | ------------------- |
| PPT 16:9 | x=40, y=40, width=1200, height=640 | 40px, 40px |
| PPT 4:3 | x=40, y=40, width=944, height=688 | 40px, 40px |

### Page Regions (16:9 reference)

| Region | Y Start | Height | Purpose |
| ------ | ------- | ------ | ------- |
| **Header** | 20 | 60px | Page title, subtitle, accent decoration |
| **Content Area** | 100 | 520px | Main content (text, images, charts) |
| **Footer** | 660 | 60px | Page number, branding, source citation |

> Strategist MUST define the content area boundary for each page type. Executor MUST place all content elements within the content area.

---

## III. Visual Theme

### Theme Style

- **Style**: {design_style}
- **Theme**: [Light theme / Dark theme]
- **Tone**: [Filled by Strategist, e.g., tech, professional, modern, innovative]

### Color Scheme

> Strategist should determine specific color values based on project content, industry, and brand colors

| Role | HEX | Purpose |
| ---- | --- | ------- |
| **Background** | `#......` | Page background (light theme typically white; dark theme dark gray/navy) |
| **Secondary bg** | `#......` | Card background, section background |
| **Primary** | `#......` | Title decorations, key sections, icons |
| **Accent** | `#......` | Data highlights, key information, links |
| **Secondary accent** | `#......` | Secondary emphasis, gradient transitions |
| **Body text** | `#......` | Main body text (dark theme uses light text) |
| **Secondary text** | `#......` | Captions, annotations |
| **Tertiary text** | `#......` | Supplementary info, footers |
| **Border/divider** | `#......` | Card borders, divider lines |
| **Success** | `#......` | Positive indicators (green family) |
| **Warning** | `#......` | Issue markers (red family) |

> **Reference**: Industry colors in `references/strategist.md` or `scripts/config.py` under `INDUSTRY_COLORS`

### Gradient Scheme (if needed, using SVG syntax)

```xml
<!-- Title gradient -->
<linearGradient id="titleGradient" x1="0%" y1="0%" x2="100%" y2="100%">
  <stop offset="0%" stop-color="#[primary]"/>
  <stop offset="100%" stop-color="#[secondary accent]"/>
</linearGradient>

<!-- Background decorative gradient (note: rgba forbidden, use stop-opacity) -->
<radialGradient id="bgDecor" cx="80%" cy="20%" r="50%">
  <stop offset="0%" stop-color="#[primary]" stop-opacity="0.15"/>
  <stop offset="100%" stop-color="#[primary]" stop-opacity="0"/>
</radialGradient>
```

---

## IV. Typography System

### Font Plan

> Strategist should select a font preset based on content characteristics, or customize the font combination
> Preset descriptions: P1=Modern business/tech | P2=Government docs | P3=Culture/arts | P4=Traditional/conservative | P5=English-primary

**Recommended preset**: [Fill in preset code]

| Role | Chinese | English | Fallback |
| ---- | ------- | ------- | -------- |
| **Title** | [font name] | [font name] | [font name] |
| **Body** | [font name] | [font name] | [font name] |
| **Code** | - | Consolas | Monaco |
| **Emphasis** | [font name] | [font name] | [font name] |

**Font stack**: `[Fill in CSS font-family string]`

### Font Size Hierarchy

> **Design principle**: Use body font size as baseline (1x), derive other levels proportionally
> **Unit convention**: Use px uniformly (SVG native unit) to avoid pt/px conversion errors
> **Selection principle**: Establish readable typography for the canvas and viewing context, then select the visible content that fits it.

**Baseline**: Body font size = [fill in]px (24px is a starting point on a 1280x720 canvas; follow assigned template typography)

| Purpose | Ratio | Example at a 24px baseline | Weight |
| ------- | ----- | ------------------------- | ------ |
| Cover title | 2.5-3x | 60-72px | Bold |
| Chapter title | 2-2.5x | 48-60px | Bold |
| Content title | 1.5-2x | 36-48px | Bold |
| Subtitle | 1.2-1.5x | 29-36px | SemiBold |
| **Body content** | **1x** | **24px** | Regular |
| Annotation | 0.75-0.85x | 18-20px | Regular |
| Page number/date | 0.55-0.65x | 13-16px | Regular |

---

## V. Layout Principles

### Page Structure

> Each page MUST follow this three-region structure. The content area boundary is the hard limit for all content elements.

- **Header area**: y=20, h=60px — Page title + subtitle + accent decoration
- **Content area**: y=100, w=1200, h=520px (16:9) — All content elements MUST be within this boundary
- **Footer area**: y=660, h=60px — Page number + source + branding

### Common Layout Modes

| Mode | Suitable Scenarios |
| ---- | ----------------- |
| **Single column centered** | Covers, conclusions, key points |
| **Left-right split (5:5)** | Comparisons, dual concepts |
| **Left-right split (4:6)** | Image-text mix |
| **Top-bottom split** | Processes, timelines |
| **Three/four column cards** | Feature lists, team introductions |
| **Matrix grid** | Comparative analysis, classifications |

### Spacing Specification

> Strategist may adjust based on project needs

| Element | Recommended Range | Current Project |
| ------- | ---------------- | --------------- |
| Card gap | 20-32px | [fill in] |
| Content block gap | 24-40px | [fill in] |
| Card padding | 20-32px | [fill in] |
| Card border radius | 8-16px | [fill in] |
| Icon-text gap | 8-16px | [fill in] |
| Single-row card height | 180-320px | [fill in] |
| Double-row card height | 220-240px each | [fill in] |
| Three-column card width | 368-384px each | [fill in] |

### Image-Text Layout Formulas

> When a page contains images, calculate layout based on the image's original aspect ratio. Never use arbitrary splits.

**Layout Decision** (PPT 16:9, content area W=1200, H=520):

| Image Aspect Ratio (R = width/height) | Layout Type | Image Position |
| ------------------------------------- | ----------- | -------------- |
| R > 2.0 (ultra-wide) | Top-bottom | Top, full width |
| 1.2 < R ≤ 2.0 (standard/wide) | Top-bottom | Top, full width |
| R ≤ 1.2 (square/portrait) | Left-right | Left side |

**Top-Bottom Layout**:
```
Image width  = W (= 1200)
Image height = W / R
Text area    = height: H - image_height - 20(gap)
Constraint:  text area height ≥ 150px, else switch to left-right
```

**Left-Right Layout**:
```
Image height = H (= 520)
Image width  = H × R
Text area    = width: W - image_width - 20(gap)
Constraint:  text area width ≥ 280px
```

**Multi-Image Grid**:
```
cell_width  = (W - (columns - 1) × 20) / columns
cell_height = (H - (rows - 1) × 20) / rows
```

### Text Volume Guidelines

> Derive text density and wrapping from each page's actual text regions, font size,
> visual share, and content structure. Do not use a deck-wide character-capacity table.

**Dynamic wrapping guidance**:
1. Let ordinary non-final lines use most of the available width instead of inserting conservative early breaks.
2. Use semantic punctuation and phrase boundaries for manual line breaks.
3. Use consistent line heights and paragraph spacing to distinguish continuous explanation from separate ideas.
4. Choose line widths and composition together with the selected visible copy.

**Anti-Overflow Rules**:
1. Preserve readable padding and clear separation between text, figures, charts, and footers.
2. Treat each text region independently according to its width and role.
3. Select visible copy at the planned readable type sizes. Carry extended explanation and supporting detail into speaker notes.
4. Only split content across slides when the manuscript and page plan already support that structure.

---

## VI. Icon Usage Specification (Optional)

Icons are optional. Most slides need 0 icons. Only add when it has a clear semantic role (section header, process step, KPI highlight). Never use as bullet prefixes or generic decoration.

### Recommended Icon List (fill only when justified)

| Purpose | Icon Path | Page | Justification |
| ------- | --------- | ---- | ------------- |
| [example] | `tabler-outline/check-circle` | Slide XX | [Why this icon is needed here] |

---

## VII. Visualization Reference List (if needed)

> When the presentation includes data visualization or infographic-style structured information design, Strategist selects visualization types from `templates/charts/charts_index.json` and lists them here for the Executor to reference. The path remains under `templates/charts/` for backward compatibility.

| Visualization Type | Reference Template | Used In |
| ------------------ | ------------------ | ------- |
| [e.g. grouped_bar_chart] | `templates/charts/grouped_bar_chart.svg` | Slide 05 |

---

## VIII. Image Resource List (if needed)

| Filename | Dimensions | Ratio | Purpose | Type | Status | Generation Description |
| -------- | --------- | ----- | ------- | ---- | ------ | --------------------- |
| cover_bg.png | {canvas_info['dimensions']} | [ratio] | Cover background | [Background/Photography/Illustration/Diagram/Decorative] | [Pending/Existing/Placeholder] | [AI generation prompt] |

**Status descriptions**:

- **Pending** - Needs AI generation, provide detailed description
- **Existing** - User already has image, place in `images/`
- **Placeholder** - Not yet processed, use dashed border placeholder in SVG

**Type descriptions** (used by Image_Generator for prompt strategy selection):

- **Background** - Full-page background for covers/chapters, reserve text area
- **Photography** - Real scenes, people, products, architecture
- **Illustration** - Flat design, vector style, cartoon, concept diagrams
- **Diagram** - Flowcharts, architecture diagrams, concept maps
- **Decorative** - Partial decorations, textures, borders, dividers

---

## IX. Content Outline

### Part 1: [Chapter Name]

#### Slide 01 - Cover

- **Layout**: [Title and metadata composition with unlabeled abstract ornament]
- **Title**: [Main title]
- **Subtitle**: [Subtitle]
- **Info**: [Author / Date / Organization]

#### Slide 02 - [Page Name]

- **Layout**: [Choose layout mode]
- **Style Family**: [Page visual family]
- **Layout Family**: [Reusable layout family]
- **Density**: [Derived from this page's content and visual share]
- **Title**: [Page title]
- **Region Plan**: [Final x/y/w/h boxes for major regions]
- **Text Strategy**: [Font range, line-height range, wrapping behavior, and table columns if relevant]
- **Visualization**: [visualization_type] (see VII. Visualization Reference List)
- **Content**:
  - [Point 1]
  - [Point 2]
  - [Point 3]

> **Visualization field**: Only add when the page includes data visualization or structured infographic elements. Visualization type must be listed in section VII.

---

[Strategist continues adding more pages based on source document content and page count planning...]

---

## X. Speaker Notes Requirements

Generate corresponding speaker note files for each page, saved to the `notes/` directory:

- **File naming**: Match SVG names, e.g., `01_cover.md`
- **Content includes**: Script key points, timing cues, transition phrases

---

## XI. Technical Constraints Reminder

### SVG Generation Must Follow:

1. viewBox: `{canvas_info['viewbox']}`
2. Background uses `<rect>` elements
3. Text wrapping uses `<tspan>` (`<foreignObject>` FORBIDDEN)
4. Transparency uses `fill-opacity` / `stroke-opacity`; `rgba()` FORBIDDEN
5. FORBIDDEN: `clipPath`, `mask`, `<style>`, `class`, `foreignObject`
6. FORBIDDEN: `textPath`, `animate*`, `script`
7. `marker-start` / `marker-end` conditionally allowed: `<marker>` must be in `<defs>`, `orient="auto"`, shape must be triangle / diamond / circle (see shared-standards.md §1.1)

### PPT Compatibility Rules:

- `<g opacity="...">` FORBIDDEN (group opacity); set on each child element individually
- Image transparency uses overlay mask layer (`<rect fill="bg-color" opacity="0.x"/>`)
- Inline styles only; external CSS and `@font-face` FORBIDDEN
