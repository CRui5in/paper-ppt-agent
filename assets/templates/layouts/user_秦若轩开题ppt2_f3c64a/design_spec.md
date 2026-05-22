# Imported Presentation Template - Design Spec

> This document is the unified handoff artifact for design definition and execution constraints. It combines visual specifications, content outline, speaker-notes requirements, and implementation boundaries needed by downstream roles.

## I. Project Information

| Item | Value |
| ---- | ----- |
| **Project Name** | Imported Presentation Template |
| **Canvas Format** | Custom 16:9 (1350 × 759) |
| **Page Count** | 5 |
| **Design Style** | Professional / Academic |
| **Target Audience** | [Filled by Strategist] |
| **Use Case** | Academic defense, corporate report, institutional presentation |
| **Created Date** | 2025-01-15 |

---

## II. Canvas Specification

| Property | Value |
| -------- | ----- |
| **Format** | Custom 16:9 |
| **Dimensions** | 1350 × 759 |
| **viewBox** | `0 0 1350 759` |
| **Margins** | Left/Right 60px, Top/Bottom 50px |
| **Content Area** | 1230 × 659 (x=60, y=50) |

### Safe Area & Page Structure

> All content elements MUST be placed within the safe area. The safe area defines the boundary that content must not exceed.

| Canvas Format | Safe Area (x, y, width, height) | Margins (L/R, T/B) |
| ------------- | ------------------------------- | ------------------- |
| Custom 16:9 (1350×759) | x=40, y=40, width=1270, height=679 | 40px, 40px |

### Page Regions (Custom 16:9 reference)

| Region | Y Start | Height | Purpose |
| ------ | ------- | ------ | ------- |
| **Header** | 0 | 110px | Top bar decoration, navigation breadcrumb, page title |
| **Content Area** | 110 | 540px | Main content (text, images, charts, section numbers) |
| **Footer** | 650 | 109px | Bottom decorative elements, date, page number |

> Strategist MUST define the content area boundary for each page type. Executor MUST place all content elements within the content area.

---

## III. Visual Theme

### Theme Style

- **Style**: Professional / Academic
- **Theme**: Light theme
- **Tone**: Formal, structured, institutional, trustworthy

### Color Scheme

| Role | HEX | Purpose |
| ---- | --- | ------- |
| **Background** | `#FFFFFF` | Page background (white) |
| **Secondary bg** | `#F5F7FA` | Card background, section background |
| **Primary** | `#174994` | Title decorations, key sections, icons, triangular accents |
| **Accent** | `#424c7d` | Secondary decorative blocks, footer panels |
| **Secondary accent** | `#ADB9CA` | Decorative lines, divider strokes, subtle accents |
| **Body text** | `#333333` | Main body text, titles, all readable content |
| **Secondary text** | `#666666` | Captions, annotations |
| **Tertiary text** | `#999999` | Supplementary info, footers |
| **Border/divider** | `#ADB9CA` | Card borders, divider lines, horizontal rules |
| **Overlay** | `#33364BD9` | Semi-transparent overlay on ending slide images |
| **Success** | `#2E7D32` | Positive indicators (green family) |
| **Warning** | `#C62828` | Issue markers (red family) |

### Gradient Scheme

```xml
<!-- Title gradient (not used in template, available for optional enhancement) -->
<linearGradient id="titleGradient" x1="0%" y1="0%" x2="100%" y2="100%">
  <stop offset="0%" stop-color="#174994"/>
  <stop offset="100%" stop-color="#424c7d"/>
</linearGradient>

<!-- Background decorative radial (optional, for section divider pages) -->
<radialGradient id="bgDecor" cx="80%" cy="20%" r="50%">
  <stop offset="0%" stop-color="#174994" stop-opacity="0.08"/>
  <stop offset="100%" stop-color="#174994" stop-opacity="0"/>
</radialGradient>
```

---

## IV. Typography System

### Font Plan

**Recommended preset**: P2 (Government / institutional documents)

| Role | Chinese | English | Fallback |
| ---- | ------- | ------- | -------- |
| **Title** | 微软雅黑 | Arial | sans-serif |
| **Body** | 微软雅黑 | Arial | sans-serif |
| **Code** | - | Consolas | Monaco |
| **Emphasis** | 微软雅黑 | Arial | sans-serif |

**Font stack**: `"Microsoft YaHei", Arial, sans-serif`

### Font Size Hierarchy

**Baseline**: Body font size = 22px (moderate density — suitable for academic/defense presentations with 4–6 points per page)

| Purpose | Ratio | Actual Size | Weight |
| ------- | ----- | ----------- | ------ |
| Cover title | 2.5–3x | 54–60px | Bold (600) |
| Chapter title (section number) | 2.5x | 54px | Bold (600) |
| Content title / Section heading | 1.2–1.5x | 26.9–30.7px | Bold (600) |
| Subtitle / Chapter subtitle | 1.2x | 26.9px | Regular (400) |
| TOC section title | 1.0x | 21.4px | Regular (400) |
| **Body content** | **1.0x** | **22px** | Regular (400) |
| Cover subtitle | 0.85x | 19px | Bold (600) |
| Navigation / breadcrumb | 0.75x | 16.8px | Regular (400) |
| Section number (TOC) | 0.65x | 14.7px | Regular (400) |
| Cover metadata (date, author) | 1.0x | 22.2px | Bold (600) |

---

## V. Layout Principles

### Page Structure

> Each page MUST follow this three-region structure. The content area boundary is the hard limit for all content elements.

- **Header area**: y=0, h=110px — Top bar with navigation breadcrumb, section numbers, decorative image/logo placement
- **Content area**: y=110, w=1270, h=540px — All content elements MUST be within this boundary
- **Footer area**: y=650, h=109px — Bottom decorative blocks, triangles, date, page number

### Common Layout Modes

| Mode | Suitable Scenarios |
| ---- | ----------------- |
| **Full-screen background + centered overlay** | Cover, Ending |
| **Vertical list with numbered sections** | Table of Contents |
| **Centered hero with triangular decorations** | Chapter dividers |
| **Top bar + full-width content block** | Standard content pages |

### Spacing Specification

| Element | Recommended Range | Current Project |
| ------- | ---------------- | --------------- |
| Card gap | 20–32px | 24px |
| Content block gap | 24–40px | 32px |
| Card padding | 20–32px | 24px |
| Card border radius | 8–16px | 8px |
| Icon-text gap | 8–16px | 12px |
| Single-row card height | 530–600px | 540px |
| Double-row card height | 265–295px each | 270px each |
| Three-column card width | 360–380px each | 376px each |

### Image-Text Layout Formulas

> When a page contains images, calculate layout based on the image's original aspect ratio. Never use arbitrary splits.

**Layout Decision** (Custom 16:9, content area W=1270, H=540):

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
Image height = H (= 540)
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

> Control text density to prevent overflow and maintain readability.

**Character Capacity Estimation** (for a text area of width W, height H):
```
Single-line height = fontSize × max(lineHeight, 1.3)
CJK character width  ≈ fontSize
Latin character width ≈ fontSize × 0.55
Max lines       = floor(H / single-line-height)
Max chars/line  = floor(W / avg-char-width)
```

**Content Area Capacity Table** (Custom 16:9, content area 1270×540):

| Font Size | Max Lines | Max CJK Chars | Max Latin Chars |
| --------- | --------- | ------------- | --------------- |
| 14px | 29 | ~22,700 | ~41,300 |
| 16px | 25 | ~16,500 | ~30,000 |
| 18px | 22 | ~12,800 | ~23,300 |
| 22px | 18 | ~8,600 | ~15,600 |
| 24px | 16 | ~7,100 | ~12,900 |

**Anti-Overflow Rules**:
1. Do NOT fill the entire content area with text. Leave at least 20% whitespace.
2. For bullet lists, limit to 6–8 items per slide.
3. For multi-column layouts, each column has independent character limits.
4. If content exceeds capacity, split across multiple slides.

---

## VI. Icon Usage Specification (Optional)

Icons are optional. The imported template does not use standalone icon elements; decorative triangles and geometric shapes serve as visual markers. No icon additions are required. Only add icons when they have a clear semantic role (section header, process step, KPI highlight). Never use as bullet prefixes or generic decoration.

### Recommended Icon List

| Purpose | Icon Path | Page | Justification |
| ------- | --------- | ---- | ------------- |
| _None required_ | — | — | Template uses geometric shapes (triangles) as decorative markers instead of icon sets |

---

## VII. Visualization Reference List

> The imported template does not contain data visualization elements (charts, graphs, infographics). This section is reserved for future extension if the presentation content requires data visualization.

| Visualization Type | Reference Template | Used In |
| ------------------ | ------------------ | ------- |
| _None_ | — | — |

---

## VIII. Image Resource List

| Filename | Dimensions | Ratio | Purpose | Type | Status | Generation Description |
| -------- | --------- | ----- | ------- | ---- | ------ | --------------------- |
| header_decoration.png | 788 × 105 | ~7.5:1 | Top header decorative band (used on cover, TOC, chapter pages) | Decorative | Placeholder | Wide decorative banner with subtle geometric or abstract pattern in primary blue (#174994) tones; institutional/academic feel |
| cover_content_bg.png | 1053 × 140 | ~7.5:1 | Cover page content area background image | Decorative | Placeholder | Abstract or thematic banner image aligned with presentation topic; wide format, moderate visual weight |
| ending_overlay_bg.png | 1350 × 351 | ~3.85:1 | Ending page background image with overlay | Background | Placeholder | Full-width atmospheric or thematic image; will be overlaid with semi-transparent blue (#33364B at 85% opacity) |
| header_logo.png | 116 × 107 | ~1.08:1 | Logo/brand mark in content page top-left corner | Illustration | Placeholder | Organization logo or thematic icon; square format, transparent background preferred |

**Status descriptions**:

- **Pending** - Needs AI generation, provide detailed description
- **Existing** - User already has image, place in `images/`
- **Placeholder** - Not yet processed, use dashed-border placeholder in SVG

---

## IX. Content Outline

### Part 1: Presentation Structure

#### Slide 01 - Cover

- **Layout**: Full white background + bottom decorative dark-blue block (y=506, h=253) + header image banner + centered title group
- **Title**: `{{Title}}` — positioned at (107, 250), font-size 32.6px, bold, color #333
- **Subtitle**: `{{SUBtitle}}` — positioned at (501, 428), font-size 19px, bold, color #333
- **Author/Advisor**: `报告人：{{ANTHOR}} 导 师：{{ADVISOR}}` — centered at (675, 608), font-size 42px, bold, color #333
- **Date**: `{{DATE}}` — centered at (675, 696), font-size 22.2px, bold, color #333
- **Decorative elements**:
  - Header image banner at (146, 43), 1053×140px
  - Dark blue bottom rectangle at (0, 506), 1350×253px, fill #424c7d
  - Accent rectangle overlay at (471, 531), 409×125px (invisible placeholder)

---

#### Slide 02 - Table of Contents

- **Layout**: White background + header decoration image + centered "目录" heading + 5 vertical TOC entries arranged in a 2-column grid (left: sections 1–3, right: sections 4–5)
- **Title**: `目录` — centered at (289, 408), font-size 35.7px, color #333
- **Header decoration**: Image at (0, 1), 788×105px
- **Section entries** (each entry consists of: section number + triangular marker + section title + horizontal line):
  - **Entry 1**: Num at (592, 125), triangle at (564, 145), title at (688, 142), line from (77, 0) to (0, 72) — stroke #ADB9CA
  - **Entry 2**: Num at (592, 262), triangle at (564, 284), title at (685, 282)
  - **Entry 3**: Num at (592, 389), triangle at (566, 417), title at (690, 414)
  - **Entry 4**: Num at (592, 524), triangle at (566, 550), title at (690, 547)
  - **Entry 5**: Num at (592, 652), triangle at (564, 684), title at (688, 681)
- **Placeholder content**:
  - `{{SECTION_NUM}}` — font-size 14.7px, color #333
  - `{{SECTION_TITLE}}` — font-size 21.4px, color #333
- **Decorative elements**: Each entry has a downward-pointing triangle (fill #424c7d) and a diagonal line (stroke #ADB9CA, width 1.33px)

---

#### Slide 03 - Chapter Divider

- **Layout**: White background + top header image + bottom-left dark-blue right triangle + top-right inverted dark-blue right triangle + centered section number and title
- **Section Number**: `{{SECTION_NUM}}` — positioned at (577, 374), font-size 54px, color #333
- **Section Title**: `{{SECTION_TITLE}}` — positioned at (460, 345), font-size 26.9px, color #333
- **Decorative elements**:
  - Bottom-left triangle: right-angle at (0, 709), 151×151px, fill #174994
  - Top-right triangle: right-angle at (1283, 0), 200×200px, fill #174994
  - Horizontal accent line at (385, 389) to (965, 389), fill #174994, 4px thick (scaled path)
  - Header image at (0, 1), 788×105px

---

#### Slide 04 - Content Page

- **Layout**: Dark-blue header bar (0, 0, 1350×110) + white content area + centered content block
- **Header bar**: Full-width rectangle, fill #174994, height ~108px
- **Header image**: Logo/brand at (0, 0), 116×107px
- **Navigation elements**: Section/page numbers at multiple positions in header area
  - `{{NAV_NUM}}` at (775, 54), font-size 16.8px — breadcrumb position
  - `{{NAV_NUM}}` at (500, 55), font-size 16.8px — breadcrumb position
  - `{{NAV_NUM}}` at (243, 58), font-size 16.8px — breadcrumb position
  - `{{NAV_NUM}}` at (1240, 75), font-size 30.7px — large page indicator
  - `{{NAV_NUM}}` at (1028, 76), font-size 30.7px — large page indicator
- **Decorative header elements**:
  - White accent rectangles at various positions in header (structural decoration)
  - Triangular pointer at (197, 84), fill #174994
  - Vertical white divider lines (1.33px stroke)
- **Content block**: Centered text area at approximately (537, 380), text `{{CONTENT}}` at (554, 415), font-size 23.6px, bold, color #333
- **White accent panel**: At (115, 0), scaled to ~260×107px, fill #FFFFFF (content area accent behind header)

---

#### Slide 05 - Ending

- **Layout**: White background + full-width background image + semi-transparent blue overlay + centered "Thanks" text + centered ending content
- **Background image**: Full-width at (0, 208), 1350×351px (background/thematic)
- **Overlay**: Dark semi-transparent rectangle at (0, 208), 1350×351px, fill #33364B, opacity ~0.85
- **Title**: `Thanks` — centered at (675, 386), font-size 54px, color #333
- **Ending content**: `{{END_CONTENT}}` — centered at (675, 412), font-size 54px, color #333
- **Decorative elements**:
  - Ring/donut shape centered at (295, 380), outer radius ~380px, inner radius ~233px (structural decoration)

---

## X. Speaker Notes Requirements

Generate corresponding speaker note files for each page, saved to the `notes/` directory:

- **File naming**: Match SVG names, e.g., `01_cover.md`
- **Content includes**: Script key points, timing cues, transition phrases

| Slide | Filename | Key Notes |
| ----- | -------- | --------- |
| 01 Cover | `01_cover.md` | Introduce self, advisor, topic. 30–45 seconds. Transition: "Let's look at the outline." |
| 02 TOC | `02_toc.md` | Walk through each section briefly. 60–90 seconds. Transition: "Starting with section 1…" |
| 03 Chapter | `03_chapter.md` | Announce section number and title. 10–15 seconds. Transition: direct into content. |
| 04 Content | `04_content.md` | Deliver main content points. 2–5 minutes per section. Transition: summary or next chapter. |
| 05 Ending | `05_ending.md` | Thank audience, invite questions. 15–30 seconds. |

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

### Template-Specific Constraints (from imported SVG analysis):

1. **Triangular decorative shapes**: Use `<path>` with explicit `d` attributes and absolute coordinates. Scale via `transform="translate() scale()"`. Fill `#174994` or `#424c7d`.
2. **Decorative diagonal lines**: Use `<line>` with `stroke="#ADB9CA"`, `stroke-width="1.33"`.
3. **Header bar on content pages**: Single `<rect>` spanning full width at y=0, height ~108px, fill `#174994`. No group opacity.
4. **Ending overlay pattern**: Place background image first, then overlay `<rect>` with `fill="#33364B" fill-opacity="0.85"` on top. Use `fill-opacity` (not rgba, not group opacity).
5. **Placeholder variables**: Use `{{VAR_NAME}}` syntax for all dynamic content fields (Title, SUBtitle, SECTION_NUM, SECTION_TITLE, NAV_NUM, CONTENT, END_CONTENT, ANTHOR, ADVISOR, DATE).
6. **Image references**: Use `href="data:image/*;base64,<omitted>"` placeholder format. Actual images placed in `images/` directory with relative paths.
7. **Font declaration**: All text elements use `font-family="Arial, sans-serif"` with explicit `font-size` and `font-weight` attributes inline per element.
8. **Text positioning**: Use `x`, `y` attributes with `text-anchor` set to `"start"`, `"middle"`, or `"end"` as needed. No CSS transforms on text.