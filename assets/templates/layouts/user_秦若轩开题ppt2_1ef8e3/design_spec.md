# Imported Presentation Template - Design Spec

> This document is the unified handoff artifact for design definition and execution constraints. It combines visual specifications, content outline, speaker-notes requirements, and implementation boundaries needed by downstream roles.

## I. Project Information

| Item | Value |
| ---- | ----- |
| **Project Name** | Imported Presentation Template (8f43bd1bbaad) |
| **Canvas Format** | Custom 16:9 (1350×759) |
| **Page Count** | 5 |
| **Design Style** | Corporate Blue / Professional |
| **Target Audience** | [Filled by Strategist] |
| **Use Case** | Academic or corporate presentation with structured sections |
| **Created Date** | 2025 |

---

## II. Canvas Specification

| Property | Value |
| -------- | ----- |
| **Format** | Custom 16:9 |
| **Dimensions** | 1350×759 |
| **viewBox** | `0 0 1350 759` |
| **Margins** | Left/right 40px, top/bottom 40px |
| **Content Area** | 1270×679 (x=40, y=40, w=1270, h=679) |

### Safe Area & Page Structure

> All content elements MUST be placed within the safe area. The safe area defines the boundary that content must not exceed.

| Canvas Format | Safe Area (x, y, width, height) | Margins (L/R, T/B) |
| ------------- | ------------------------------- | ------------------- |
| Custom 16:9 (1350×759) | x=40, y=40, width=1270, height=679 | 40px, 40px |

### Page Regions

| Region | Y Start | Height | Purpose |
| ------ | ------- | ------ | ------- |
| **Header** | 0 | 108px | Blue accent bar, page title, navigation numbers, logo placeholder |
| **Content Area** | 108 | 520px | Main content (text, images, charts, data) |
| **Footer** | 628 | 131px | Ending slide decorative overlay zone (page-type dependent) |

> Strategist MUST define the content area boundary for each page type. Executor MUST place all content elements within the content area.

---

## III. Visual Theme

### Theme Style

- **Style**: Corporate Blue / Professional
- **Theme**: Light theme (white background with deep blue accents)
- **Tone**: Formal, structured, academic-to-corporate, authoritative

### Color Scheme

> Colors extracted directly from imported template SVGs.

| Role | HEX | Purpose |
| ---- | --- | ------- |
| **Background** | `#FFFFFF` | Page background (all slides use white) |
| **Secondary bg** | `#FFFFFF` | Card background (cards use white with no fill differentiation in template) |
| **Primary** | `#174994` | Header bar fill, chapter decorations, triangular accents, divider lines |
| **Accent** | `#424c7d` | TOC triangle arrows, cover accent bar, section-level emphasis |
| **Secondary accent** | `#004493` | Cover main title text color |
| **Body text** | `#000000` | Content body text, navigation numbers |
| **Secondary text** | `#808080` | Cover subtitle text |
| **Tertiary text** | `#a6a6a6` | TOC section numbers (decorative secondary) |
| **Border/divider** | `#ADB9CA` | TOC diagonal decorative lines |
| **Overlay** | `#33364bd9` | Ending slide dark semi-transparent overlay (≈85% opacity on `#33364b`) |
| **Light text** | `#FFFFFF` | Author/date text on dark cover bar, nav bar white separators |
| **Ending text** | `#D9D9D9` | "Thanks" text on ending overlay |
| **Watermark** | `#174994` with opacity ≈0.38 | Large chapter number watermark (observed as `#17499461`) |

### Gradient Scheme

No gradients are used in this template. The design relies on flat color fills and opacity-based layering.

```xml
<!-- Ending overlay (recreated from template opacity pattern) -->
<!-- Original uses fill="#33364bd9" which is ~85% opacity hex -->
<!-- PPT-safe recreation: base rect + overlay rect -->
<rect x="0" y="208" width="1350" height="351" fill="#33364b" fill-opacity="0.85"/>
```

---

## IV. Typography System

### Font Plan

> Preset P1 (Modern business/tech) adapted for Chinese-primary bilingual use.

**Recommended preset**: P1 (Modern business/tech, bilingual)

| Role | Chinese | English | Fallback |
| ---- | ------- | ------- | -------- |
| **Title** | 微软雅黑 | 微软雅黑 | Arial, sans-serif |
| **Body** | 微软雅黑 | 微软雅黑 | Arial, sans-serif |
| **Code** | - | Consolas | Monaco |
| **Emphasis / Nav** | 微软雅黑 Bold | 微软雅黑 Bold | Arial, sans-serif |
| **Chapter number watermark** | - | Times New Roman | serif |
| **TOC / Chapter headings** | - | Arial | sans-serif |

**Font stack**: `"Microsoft YaHei", "微软雅黑", Arial, sans-serif`

### Font Size Hierarchy

> **Design principle**: Use body font size as baseline (1x), derive other levels proportionally
> **Unit convention**: Use px uniformly (SVG native unit)
> **Baseline**: Body font size = 36px (relaxed, 3–5 content points per page typical)

| Purpose | Ratio | Size (px) | Weight | Notes |
| ------- | ----- | --------- | ------ | ----- |
| Ending decorative text | 3.5x | 128px | Bold | "Thanks" on ending slide only |
| Cover title | 1.8x | 64px | Bold (700) | Main cover title, `#004493` |
| Chapter section title | 1.5x | 53px | Bold (700) | Chapter page section name |
| TOC section title | 1.3x | 48px | Bold (700) | TOC entry labels |
| Cover subtitle / Body content | **1x** | **36px** | Bold (700) | Cover subtitle, content text blocks |
| Navigation numbers | 0.9x | 32px | Bold (700) | Content header nav numbers |
| TOC section number | 0.9x | 33px | Regular | Decorative section numbers in TOC |
| Chapter watermark number | 7.7x | 279px | Bold (700) | Background watermark, low opacity |
| Page number/date | 0.5x | 18px | Regular | Footer metadata |

> **Note**: All body-level text in this template uses Bold (700) weight. The template does not use Regular weight for body text. This is a stylistic choice of the imported design.

---

## V. Layout Principles

### Page Structure

> Each page follows a type-specific structure. The template defines five canonical page types with distinct layouts.

- **Header area**: y=0, h≈108px — Blue header bar with accent shape, navigation numbers, triangle arrow
- **Content area**: y=108, h≈520px — Main content zone for text, images, and data
- **Footer/Overlay area**: y≈628, h≈131px — Ending slide overlay zone or footer content

### Page Type Layout Modes

| Page Type | Layout Mode | Description |
| --------- | ----------- | ----------- |
| **Cover** | Full-page centered | Top decorative image strip, centered title block, author/date bar at bottom |
| **TOC** | Vertical list (left-aligned entries) | Decorative image top-left, "目录" large label left, stacked section entries right |
| **Chapter** | Full-page watermark | Large translucent number, corner triangle decorations, section title centered |
| **Content** | Header bar + body | Blue header with nav, white body with text content |
| **Ending** | Centered overlay | Full-width background image with dark overlay, centered decorative text |

### Spacing Specification

| Element | Recommended Range | Current Project |
| ------- | ---------------- | --------------- |
| Card gap (TOC entries) | 20-32px | 24px |
| Content block gap | 24-40px | 32px |
| Card padding | 20-32px | 24px |
| Card border radius | 8-16px | 0px (sharp rectangles in template) |
| Icon-text gap | 8-16px | 12px |
| TOC entry vertical spacing | 120-140px | 130px |
| Header bar height | 100-110px | 108px |
| Cover author bar height | 70-80px | 72px |

### Image-Text Layout Formulas

> When a page contains images, calculate layout based on the image's original aspect ratio.

**Layout Decision** (Custom 16:9, content area W=1270, H=520):

| Image Aspect Ratio (R = width/height) | Layout Type | Image Position |
| ------------------------------------- | ----------- | -------------- |
| R > 2.0 (ultra-wide) | Top-bottom | Top, full width |
| 1.2 < R ≤ 2.0 (standard/wide) | Top-bottom | Top, full width |
| R ≤ 1.2 (square/portrait) | Left-right | Left side |

**Top-Bottom Layout**:
```
Image width  = W (= 1270)
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

### Text Volume Guidelines

> Control text density to prevent overflow and maintain readability.

**Character Capacity Estimation** (for a text area of width W, height H):
```
Single-line height = fontSize × max(lineHeight, 1.3)
CJK character width  ≈ fontSize
Latin character width ≈ fontSize × 0.55
Max lines       = floor(H / single-line-height)
Max chars/line  = floor(W / avg-char-width)
```

**Content Area Capacity Table** (Custom 16:9, content area 1270×520):

| Font Size | Max Lines | Max CJK Chars | Max Latin Chars |
| --------- | --------- | ------------- | --------------- |
| 20px | 20 | ~12,700 | ~23,100 |
| 24px | 16 | ~8,467 | ~15,400 |
| 32px | 12 | ~4,760 | ~8,655 |
| 36px | 11 | ~3,776 | ~6,866 |
| 48px | 8 | ~2,117 | ~3,850 |

**Anti-Overflow Rules**:
1. Do NOT fill the entire content area with text. Leave at least 20% whitespace.
2. For bullet lists, limit to 6–8 items per slide.
3. For multi-column layouts, each column has independent character limits.
4. If content exceeds capacity, split across multiple slides.

---

## VI. Icon Usage Specification (Optional)

Icons are optional in this template. The template uses geometric shapes (triangles, circles) as decorative elements rather than semantic icons. Only add icons when they serve a clear semantic role.

### Recommended Icon List

No icons are required by the template design. The template relies on:
- **Triangle arrows** (filled `#174994`) for TOC navigation indicators
- **Triangle decorations** (filled `#174994`) for chapter page corners
- **Circle/ring** decorative element on ending slide

If additional icons are needed for content pages:

| Purpose | Icon Path | Page | Justification |
| ------- | --------- | ---- | ------------- |
| [Only add when justified] | - | - | - |

---

## VII. Visualization Reference List

> When the presentation includes data visualization or infographic-style structured information design, list visualization types here.

| Visualization Type | Reference Template | Used In |
| ------------------ | ------------------ | ------- |
| [No data visualizations in base template] | - | - |

> The content page template contains a single text block placeholder (`{{CONTENT}}`). If data visualizations are added to content pages, select types from `templates/charts/charts_index.json` and document them here.

---

## VIII. Image Resource List

| Filename | Dimensions | Ratio | Purpose | Type | Status | Generation Description |
| -------- | --------- | ----- | ------- | ---- | ------ | --------------------- |
| cover_header.png | 1053×140 | ~7.5:1 | Cover decorative header strip (geometric/abstract pattern) | Decorative | Pending | Abstract geometric blue gradient pattern, horizontal banner, corporate style, transparent/dark tones suitable for overlay with title text below |
| toc_header.png | 788×105 | ~7.5:1 | TOC page top-left decorative strip | Decorative | Pending | Abstract geometric blue pattern matching cover style, left-aligned horizontal strip |
| chapter_header.png | 788×105 | ~7.5:1 | Chapter page top-left decorative strip | Decorative | Pending | Same style as TOC header, consistent visual language |
| ending_bg.png | 1350×351 | ~3.8:1 | Ending slide background image (under dark overlay) | Background | Pending | Professional abstract background (cityscape, technology, or geometric), will be darkened by overlay, center-weighted composition |

**Status descriptions**:

- **Pending** - Needs AI generation, provide detailed description
- **Existing** - User already has image, place in `images/`
- **Placeholder** - Not yet processed, use dashed border placeholder in SVG

**Type descriptions**:

- **Background** - Full-page background for covers/chapters, reserve text area
- **Photography** - Real scenes, people, products, architecture
- **Illustration** - Flat design, vector style, cartoon, concept diagrams
- **Diagram** - Flowcharts, architecture diagrams, concept maps
- **Decorative** - Partial decorations, textures, borders, dividers

---

## IX. Content Outline

### Part 1: Presentation Structure

#### Slide 01 - Cover

- **Layout**: Full-page with top decorative image strip + centered title block + bottom author bar
- **Structure**:
  - y=0–108: Decorative image strip (full width, aspect ~7.5:1)
  - y=202–264: Main title (centered, 64px, Bold, `#004493`)
  - y=400–436: Subtitle (centered, 36px, Bold, `#808080`)
  - y=506–579: Dark accent bar (`#424c7d`, full width, h≈72px)
    - Author line: centered, 36px, Bold, `#FFFFFF` — "报告人：{{ANTHOR}}"
    - Advisor line: centered, 36px, Bold, `#FFFFFF` — "导 师：{{ADVISOR}}"
  - y=663–703: Date line (centered, 36px, Bold, `#FFFFFF`) — "{{DATE}}"
- **Title**: {{Title}}
- **Subtitle**: {{SUBtitle}}
- **Info**: 报告人：{{ANTHOR}} / 导 师：{{ADVISOR}} / {{DATE}}

#### Slide 02 - TOC (Table of Contents)

- **Layout**: Decorative image top-left + "目录" large label + vertical section entry list (right-aligned)
- **Structure**:
  - y=0–105: Decorative image strip (left-aligned, w=788)
  - y=290–424: "目录" large label (left column, 80px, Bold, `#424c7d`)
  - Right column: 5 section entries, each containing:
    - Section number (small, 33px, `#a6a6a6`, centered above title)
    - Triangle arrow indicator (filled `#424c7d`)
    - Section title (48px, Bold, `#424C80`)
    - Diagonal decorative line (stroke `#ADB9CA`)
  - Entry vertical positions: ~152, ~291, ~424, ~557, ~690
- **Title**: 目录
- **Content**: 5 section entries ({{SECTION_NUM}} + {{SECTION_TITLE}} pairs)

#### Slide 03 - Chapter Divider

- **Layout**: Full-page with corner triangle decorations + large watermark number + section title
- **Structure**:
  - Bottom-left corner: Right-triangle decoration (filled `#174994`)
  - Top-right corner: Reflected right-triangle decoration (filled `#174994`)
  - y=0–105: Decorative image strip (left-aligned, w=788)
  - Center area:
    - Large watermark number (279px, Bold, `#174994` at ~38% opacity, Times New Roman)
    - Section title (53px, Bold, `#424C80`, Arial)
    - Horizontal divider line (stroke `#174994`, w≈580)
- **Title**: {{SECTION_TITLE}}
- **Info**: {{SECTION_NUM}} (watermark)

#### Slide 04 - Content

- **Layout**: Blue header bar with navigation + white body with content text
- **Structure**:
  - y=0–108: Blue header bar (fill `#174994`, full width)
    - Left logo area placeholder (x≈0, w≈116)
    - Navigation numbers distributed across header (32px, Bold, `#000000`)
    - White separator lines between nav items
    - Triangle arrow indicator (filled `#174994`)
    - White accent rectangle overlay
  - y=108+: White content area
    - Content text block (36px, Bold, `#000000`)
- **Title**: (implied by header navigation)
- **Content**: {{CONTENT}}

#### Slide 05 - Ending

- **Layout**: Full-width background image with dark semi-transparent overlay + centered decorative text
- **Structure**:
  - y=208–559: Background image (full width, h≈351)
  - Overlay: `#33364b` at ~85% opacity over image area
  - Center: Ring/circle decorative element (unfilled, stroke only)
  - Center text: "Thanks" (128px, Bold, `#D9D9D9`, 微软雅黑)
- **Title**: Thanks
- **Content**: [None — purely decorative closing slide]

---

## X. Speaker Notes Requirements

Generate corresponding speaker note files for each page, saved to the `notes/` directory:

- **File naming**: Match SVG names, e.g., `01_cover.md`
- **Content includes**: Script key points, timing cues, transition phrases

| File | Page Type | Content Guidance |
| ---- | --------- | ---------------- |
| `01_cover.md` | Cover | Welcome introduction, presenter name, topic overview, date acknowledgment |
| `02_toc.md` | TOC | Overview of presentation structure, section descriptions, estimated duration |
| `03_chapter.md` | Chapter | Section transition statement, brief preview of upcoming content |
| `04_content.md` | Content | Main talking points for {{CONTENT}}, key data to highlight |
| `05_ending.md` | Ending | Closing remarks, Q&A invitation, thank-you message, contact information |

---

## XI. Technical Constraints Reminder

### SVG Generation Must Follow:

1. viewBox: `0 0 1350 759`
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

### Template-Specific Constraints:

- **Ending slide overlay**: The original template uses a hex-with-alpha fill (`#33364bd9`). For PPT compatibility, this MUST be decomposed into a base image rect + a separate overlay rect with `fill="#33364b" fill-opacity="0.85"`.
- **Chapter watermark opacity**: The large section number uses `#17499461` (hex alpha). Recreate as `fill="#174994" fill-opacity="0.38"` for compatibility.
- **Triangle decorations**: All triangular shapes use `<path>` elements with absolute coordinates and transforms. Do NOT convert to `<polygon>` or `<polyline>`.
- **Image placeholders**: The template embeds base64 images inline. In production SVGs, use `href="images/filename.png"` with relative paths. Placeholder images use dashed-border `<rect>` with `stroke-dasharray="8,4"`.
- **Font rendering**: All text uses `text-anchor="middle"` or `text-anchor="start"` explicitly. Never omit the `text-anchor` attribute.
- **Navigation system**: Content page header contains multiple navigation number placeholders. Each must be positioned independently (no `<g>` grouping with opacity).