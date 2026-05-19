# 秦若轩开题PPT2 - Design Spec

> This document is the unified handoff artifact for design definition and execution constraints. It combines visual specifications, content outline, speaker-notes requirements, and implementation boundaries needed by downstream roles.

## I. Project Information

| Item | Value |
| ---- | ----- |
| **Project Name** | 秦若轩开题PPT2 |
| **Canvas Format** | PPT 16:9 (1350.08 × 759.36) |
| **Page Count** | 5 (Cover, TOC, Chapter, Content, Ending) |
| **Design Style** | Academic / Formal / Structured |
| **Target Audience** | Academic committee, thesis defense panel |
| **Use Case** | Thesis proposal defense / academic presentation |
| **Created Date** | 2025-01-15 |

---

## II. Canvas Specification

| Property | Value |
| -------- | ----- |
| **Format** | PPT 16:9 |
| **Dimensions** | 1350.08 × 759.36 px |
| **viewBox** | `0 0 1350.08 759.36` |
| **Margins** | Left/Right 60px, Top/Bottom 40px |
| **Content Area** | 1230.08 × 679.36 px |

### Safe Area & Page Structure

> All content elements MUST be placed within the safe area. The safe area defines the boundary that content must not exceed.

| Canvas Format | Safe Area (x, y, width, height) | Margins (L/R, T/B) |
| ------------- | ------------------------------- | ------------------- |
| PPT 16:9 | x=40, y=40, width=1270.08, height=679.36 | 60px, 40px |

### Page Regions (16:9 reference — base layout, varies by page type)

| Region | Y Start | Height | Purpose |
| ------ | ------- | ------ | ------- |
| **Header** | 0 | 80px | Page title, section label, blue accent band (cover/content pages) |
| **Content Area** | 80 | 520px | Main content (text, images, charts, navigation) |
| **Footer** | 640 | 119.36px | Navigation bar, page numbers, blue bottom band |

> Executor MUST adapt region boundaries per page type (see Section IX). All content elements MUST be placed within the designated content area of each page type.

---

## III. Visual Theme

### Theme Style

- **Style**: Academic / Formal / Structured
- **Theme**: Light theme (white background with navy accents)
- **Tone**: Professional, clean, institutional

### Color Scheme

> Derived from original PPTX theme colors and SVG analysis.

| Role | HEX | Purpose |
| ---- | --- | ------- |
| **Background** | `#FFFFFF` | Page background (white) |
| **Secondary bg** | `#F5F7FA` | Card background, subtle section background |
| **Primary** | `#174994` | Decorative accents, bottom bands, diagonal corners, icons |
| **Primary variant** | `#424C80` | Cover header band, section indicators, arrow accents |
| **Accent** | `#ADB9CA` | TOC decorative lines, subtle borders, divider strokes |
| **Secondary accent** | `#ABB2D3` | Light accent for secondary highlights |
| **Body text** | `#000000` | Main body text |
| **Secondary text** | `#44546A` | Subtitles, secondary body text (dk2 from theme) |
| **Tertiary text** | `#A6A6A6` | Page numbers, annotations, captions |
| **Placeholder text** | `#7F7F7F` | Template placeholder labels (e.g., {{TITLE}}) |
| **Dark overlay text** | `#D9D9D9` | Ending page large display text |
| **Border/divider** | `#ADB9CA` | Card borders, divider lines, TOC connector strokes |
| **White** | `#FFFFFF` | Text on dark backgrounds, footer navigation text |
| **Success** | `#2E7D32` | Positive indicators (if needed) |
| **Warning** | `#C62828` | Issue markers (if needed) |

> **Reference**: Theme source from PPTX `theme/` XML; accent3 = `#424C80`, dk2 = `#44546A`.

### Gradient Scheme

No gradients are used in this template. The visual system relies on flat fills and solid color blocks.

```xml
<!-- Background decorative radial (ending page concentric circles — rendered as strokes, not gradients) -->
<!-- Cover header band uses solid fill #424C80 -->
<!-- Content footer band uses solid fill #174994 -->
```

---

## IV. Typography System

### Font Plan

> This template uses a dual-script academic font combination. Chinese text uses Microsoft YaHei; English headings use Arial or Times New Roman per theme defaults.

**Recommended preset**: P2 (Government / Academic docs)

| Role | Chinese | English | Fallback |
| ---- | ------- | ------- | -------- |
| **Title** | 微软雅黑 (Microsoft YaHei) | Arial | sans-serif |
| **Body** | 微软雅黑 (Microsoft YaHei) | Times New Roman | serif |
| **Code** | - | Consolas | Monaco |
| **Emphasis** | 微软雅黑 (Microsoft YaHei) | Arial | sans-serif |

**Font stack**: `"Microsoft YaHei", "微软雅黑", Arial, "Times New Roman", sans-serif`

### Font Size Hierarchy

> **Design principle**: Academic presentation with moderate text density. Use body font size as baseline (1x).
> **Unit convention**: px (SVG native unit)
> **Selection principle**: Font size is based on **content density** for thesis defense style (moderate density, 3–5 points per page).

**Baseline**: Body font size = 20px (moderate academic density)

| Purpose | Ratio | Size (px) | Weight |
| ------- | ----- | --------- | ------ |
| Cover title | 2.5–3x | 50–60px | Bold |
| Chapter title | 2.5x | 48px | Bold |
| Content title | 1.4x | 28px | Bold |
| Section indicator (side) | 1.2x | 24px | Bold |
| TOC section title | 1.8x | 36px | Bold |
| TOC section number | 1.25x | 25px | Regular |
| Cover subtitle/label | 1.4x | 28px | Bold |
| Navigation text | 1.2x | 24px | Bold |
| **Body content** | **1x** | **20px** | **Regular** |
| Annotation / caption | 0.75x | 15px | Regular |
| Page number / footer | 0.6x | 12px | Regular |
| Ending display text | 4.8x | 96px | Bold |

---

## V. Layout Principles

### Page Structure

> Each page follows a structured region. The specific layout varies by page type.

- **Cover**: Blue header band (top 25%, y=0–187) + white center body + blue bottom bar with author/date info
- **TOC**: White background + left "目录" label + right section entries with numbers, arrow connectors, and section titles
- **Chapter**: White background + blue diagonal corner triangles (top-left, bottom-right) + centered chapter title + decorative elements
- **Content**: White background + bottom navigation bar (blue band, y≈651–759) + optional side section indicator (right side vertical box) + main content area
- **Ending**: White background + concentric circle decorative motif + centered "Thanks" text + bottom image band

### Common Layout Modes

| Mode | Suitable Scenarios |
| ---- | ----------------- |
| **Single column centered** | Cover title, chapter title, ending text |
| **Left-right split (3:7)** | TOC — left number, right title |
| **Bottom navigation bar** | Content pages — 5-cell navigation strip |
| **Full-width content band** | Content main body area |
| **Centered with decorative frame** | Ending page — circles frame content |

### Spacing Specification

| Element | Recommended Range | Current Template |
| ------- | ---------------- | --------------- |
| Card gap | 20–30px | 20px |
| Content block gap | 24–36px | 28px |
| Card padding | 20–30px | 24px |
| Card border radius | 8–12px | 0px (sharp corners) |
| Icon-text gap | 10–14px | 12px |
| Section indicator width | 40px | 40px |
| Navigation bar cell width | ~240px | 240px |
| Navigation bar cell gap | 1px (divider lines) | 1px |
| TOC entry vertical spacing | ~104px | 104px |
| Content text left margin | 60px | 60px |

### Text Volume Guidelines

> Academic presentations should maintain clean readability. Do not overload slides.

**Content Area Capacity Table** (content area approx. 1170 × 440 px):

| Font Size | Max Lines | Max CJK Chars | Max Latin Chars |
| --------- | --------- | ------------- | --------------- |
| 18px | 20 | ~10,500 | ~19,000 |
| 20px | 18 | ~8,500 | ~15,400 |
| 24px | 14 | ~5,500 | ~10,000 |

**Anti-Overflow Rules**:
1. Do NOT fill the entire content area with text. Leave at least 25% whitespace for academic clarity.
2. For bullet lists, limit to 5–7 items per slide.
3. For multi-column layouts, each column has independent character limits.
4. If content exceeds capacity, split across multiple slides.

---

## VI. Icon Usage Specification (Optional)

Icons are optional. This academic template uses minimal iconography. Only decorative geometric elements (arrows, triangles, rectangles) are used as structural accents.

### Recommended Icon List

| Purpose | Icon Path | Page | Justification |
| ------- | --------- | ---- | ------------- |
| Navigation arrow | (inline SVG triangle) | TOC | Directional indicator linking section number to title |
| Section indicator box | (inline SVG rect) | Content | Right-side vertical section label container |
| Footer diamond | (inline SVG path) | Cover | Decorative accent between author and advisor lines |

> All decorative elements are rendered as inline SVG shapes (triangles, rectangles, lines), NOT as external icon files.

---

## VII. Visualization Reference List (if needed)

> This template is designed for thesis defense text content and does not include pre-built chart visualizations. If data visualization is needed by the user, select from `templates/charts/charts_index.json`.

| Visualization Type | Reference Template | Used In |
| ------------------ | ------------------ | ------- |
| — | — | — |

> No default visualizations embedded. Add per project content needs.

---

## VIII. Image Resource List (if needed)

| Filename | Dimensions | Ratio | Purpose | Type | Status | Generation Description |
| -------- | --------- | ----- | ------- | ---- | ------ | --------------------- |
| cover_header_bg.png | 1350 × 187 | 7.2:1 | Cover blue header band (optional raster overlay) | Background | Placeholder | Solid navy blue #424C80 band — no image needed, use SVG rect |
| ending_bottom_bg.png | 1350 × 120 | 11.25:1 | Ending page bottom decorative image band | Decorative | Placeholder | Muted academic-themed image or texture strip |
| content_footer_accent.png | 100 × 108 | 0.93:1 | Content page footer icon (left of navigation bar) | Decorative | Placeholder | Small academic/institutional logo or icon |

> **Note**: The original template uses inline base64 images for the footer accent icon. Regenerate or provide a replacement icon file.

---

## IX. Content Outline

### Part 1: Cover

#### Slide 01 - Cover

- **Layout**: Full-width — blue header band (top 25%) + white body + blue bottom bar
- **Structure**:
  - **Header band** (y=0, h=187): Blue fill `#424C80`
  - **Title area** (y=200–280): Large centered title in `#004493`, 48px, bold
  - **Subtitle area** (y=280–320): Subtitle placeholder in `#7F7F7F`, 28px, bold
  - **Info block** (y=350–470): Three rows — "报告人：{{AUTHOR}}", "导　师：{{ADVISOR}}", "{{DATE}}" — white text on blue band
  - **Bottom band** (y=470–560): Blue fill `#424C80` containing the info text
- **Placeholders**:
  - `{{TITLE}}` — Main presentation title
  - `{{SUBTITLE}}` — Subtitle / thesis topic subtitle
  - `{{AUTHOR}}` — Presenter name
  - `{{ADVISOR}}` — Advisor name
  - `{{DATE}}` — Presentation date

---

### Part 2: Table of Contents

#### Slide 02 - TOC

- **Layout**: White background, left-aligned "目录" label, right-side section entries stacked vertically
- **Structure**:
  - **Title** (left, y≈245): "目录" in `#424C80`, 60px, bold
  - **Section entries** (5 entries stacked, y spacing ~104px each):
    - Each entry has: section number (e.g., "01") in `#A6A6A6` 25px + arrow connector (line + triangle in `#ADB9CA`/`#424C80`) + section title in `#424C80` 36px bold
  - **Bottom accent** (y≈490–570): Small decorative image strip (same as cover footer)
- **Placeholders** (per entry):
  - `{{SECTION_NUM}}` — Two-digit section number (e.g., "01")
  - `{{SECTION_TITLE}}` — Section/chapter title text

> Supports up to 5 section entries. For fewer entries, remove bottom entries and adjust vertical spacing evenly.

---

### Part 3: Chapter Divider

#### Slide 03 - Chapter

- **Layout**: White background + blue diagonal corner triangles + centered chapter content
- **Structure**:
  - **Top-left triangle**: Blue `#174994`, fills top-left corner (hypotenuse from top-left to ~151px right and 151px down)
  - **Bottom-right triangle**: Blue `#174994`, fills bottom-right corner (mirror of top-left)
  - **Center content** (y=200–380): Chapter number/title centered
  - **Decorative "∞" motif** (y≈285): Stylized infinity/loop shape in `#174994` 14px stroke (from original SVG path data)
- **Placeholders**:
  - `{{CHAPTER_NUM}}` — Chapter number
  - `{{CHAPTER_TITLE}}` — Chapter title

> The original template contains a complex decorative path element resembling a stylized infinity symbol. Reproduce as a simple geometric accent or omit.

---

### Part 4: Content Slide

#### Slide 04 - Content

- **Layout**: White background + bottom blue navigation bar + optional right-side section indicator
- **Structure**:
  - **Content area** (y=0–640): Main body content (title + text/visuals)
  - **Content title** (y≈385): `{{CONTENT}}` placeholder, 28px, bold, black
  - **Navigation bar** (y=651–759): Blue `#174994` fill band, 5 cells with dividers
    - Each cell: `{{NAV_NUM}}` text in white, 24px, bold — represents sequential navigation labels
    - 4 white vertical divider lines between cells at fixed x positions
  - **Footer icon** (y≈651, x≈0–87): Small decorative image/icon in bottom-left corner
  - **Section indicator** (right side, x≈1270–1345): Vertical box in `#174994` with `{{SECTION_NUM}}` text stacked vertically, white on blue, 18px bold
- **Placeholders**:
  - `{{NAV_NUM}}` — Navigation cell label (×5, e.g., "背景介绍", "研究方法", etc.)
  - `{{CONTENT}}` — Main content title
  - `{{SECTION_NUM}}` — Section indicator label (vertical text on right)

> For actual content slides, replace `{{NAV_NUM}}` placeholders with meaningful navigation labels. The 5-cell nav bar corresponds to the 5 main sections of the presentation.

---

### Part 5: Ending

#### Slide 05 - Ending

- **Layout**: White background + concentric circle decorative motif + centered "Thanks" text + bottom image band
- **Structure**:
  - **Concentric circles** (centered): Two circle outlines in `#7F7F7F`, stroke-width 1px — outer circle r≈285px, inner circle r≈175px
  - **Display text** (centered, y≈395): "Thanks" in `#D9D9D9`, 96px, bold
  - **Bottom image band** (y≈560–700): Decorative image strip (same as cover footer area)
- **Placeholders**:
  - No dynamic placeholders — this is a static closing slide

---

## X. Speaker Notes Requirements

Generate corresponding speaker note files for each page, saved to the `notes/` directory:

- **File naming**: Match SVG names, e.g., `01_cover.md`, `02_toc.md`, `03_chapter.md`, `04_content.md`, `05_ending.md`
- **Content includes**:
  - Script key points and talking prompts
  - Timing cues (e.g., "Spend ~30 seconds on this slide")
  - Transition phrases to next slide
  - For content slides: reminders of what data/argument to present
- **Note format**:

```markdown
# Slide 01 - Cover

## Key Points
- Introduce yourself, your advisor, and your thesis topic
- Keep this brief — ~30 seconds

## Timing
~30 seconds

## Transition
"Let me start by showing you the outline of today's presentation..."
```

---

## XI. Technical Constraints Reminder

### SVG Generation Must Follow:

1. **viewBox**: `0 0 1350.08 759.36`
2. **Background**: Use `<rect>` elements with solid fills
3. **Text wrapping**: Use `<tspan>` with manual positioning (`<foreignObject>` FORBIDDEN)
4. **Transparency**: Use `fill-opacity` / `stroke-opacity`; `rgba()` FORBIDDEN
5. **FORBIDDEN**: `clipPath`, `mask`, `<style>`, `class`, `foreignObject`
6. **FORBIDDEN**: `textPath`, `animate*`, `script`
7. **Markers**: Conditionally allowed — `<marker>` must be in `<defs>`, `orient="auto"`, shape must be triangle/diamond/circle (see shared-standards.md §1.1)

> **CRITICAL**: The original template uses `clipPath` extensively for element clipping. All clip-path usage MUST be removed during generation. Replace with:
> - Trimming path coordinates to stay within visible bounds
> - Using `<rect>` fills that match the intended clip region
> - Redrawing shapes within the safe area without clipping

### PPT Compatibility Rules:

1. `<g opacity="...">` FORBIDDEN (group opacity) — set opacity on each child element individually
2. Image transparency uses overlay mask layer (`<rect fill="bg-color" opacity="0.x"/>`)
3. Inline styles only; external CSS and `@font-face` FORBIDDEN
4. All text must use `xml:space="preserve"` for proper rendering
5. Coordinate transforms: use `transform="matrix(...)"` only when matching original layout; prefer direct x/y positioning for new content

### Template-Specific Generation Notes:

| Page Type | Key Technical Notes |
| --------- | ------------------- |
| **Cover** | Blue header band is a single `<rect>` fill; info text sits in a second blue `<rect>` below center; avoid clipPath for text bounds |
| **TOC** | Section numbers are positioned with explicit x/y per character cluster; arrow connector is a `<line>` + `<polygon>` triangle; replicate per entry |
| **Chapter** | Corner triangles are `<path>` elements with triangular coordinates (no clipPath needed); center content uses direct text placement |
| **Content** | Navigation bar is a blue `<rect>` + white `<line>` dividers + 5 text elements; section indicator is a blue `<rect>` + rotated/transposed text; footer icon as `<image>` or `<rect>` placeholder |
| **Ending** | Concentric circles as `<circle>` elements with `stroke` and `fill="none"`; "Thanks" as single `<text>` element; bottom image as `<image>` element |

### Placeholder Contract

All placeholders follow the `{{PLACEHOLDER_NAME}}` convention. During content population:

| Placeholder | Page(s) | Replacement |
| ----------- | ------- | ----------- |
| `{{TITLE}}` | Cover | Project/thesis title |
| `{{SUBTITLE}}` | Cover | Subtitle or research topic |
| `{{AUTHOR}}` | Cover | Presenter name |
| `{{ADVISOR}}` | Cover | Advisor/supervisor name |
| `{{DATE}}` | Cover | Presentation date |
| `{{SECTION_NUM}}` | TOC, Content | Two-digit section number (01–05) |
| `{{SECTION_TITLE}}` | TOC | Chapter/section title text |
| `{{NAV_NUM}}` | Content | Navigation bar cell label |
| `{{CONTENT}}` | Content | Main content slide title |
| `{{CHAPTER_NUM}}` | Chapter | Chapter number |
| `{{CHAPTER_TITLE}}` | Chapter | Chapter title |

> All placeholder text uses `fill="#7F7F7F"` (gray) in the original template to indicate editable regions. During generation, replace with actual content using the appropriate color per the color scheme (typically `#000000` for body text, `#004493` or `#174994` for titles).