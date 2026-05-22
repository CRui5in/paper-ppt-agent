# 第二组 - Design Spec

## I. Project Information

| Item | Value |
| ---- | ----- |
| **Project Name** | 第二组 |
| **Canvas Format** | 16:9 (1280×720) |
| **Page Count** | 5 representative layouts (cover, toc, chapter, content, ending) |
| **Design Style** | Academic presentation, clean modern |
| **Target Audience** | University academic reviewers |
| **Use Case** | Paper reading report / group presentation |
| **Created Date** | 2026-05-22 |

---

## II. Canvas Specification

| Property | Value |
| -------- | ----- |
| **Format** | 16:9 |
| **Dimensions** | 1280 × 720 |
| **viewBox** | `0 0 1280 720` |
| **Margins** | L/R 40px, T/B 40px |
| **Content Area** | x=40, y=40, w=1200, h=640 |

### Safe Area & Page Structure

| Canvas Format | Safe Area (x, y, width, height) | Margins (L/R, T/B) |
| ------------- | ------------------------------- | ------------------- |
| PPT 16:9 | x=40, y=40, width=1200, height=640 | 40px, 40px |

### Page Regions (16:9 reference)

| Region | Y Start | Height | Purpose |
| ------ | ------- | ------ | ------- |
| **Header** | 0 | 90px | Chapter number, page title, blue accent line |
| **Content Area** | 90 | 530px | Main content (text, images, charts) |
| **Footer** | 660 | 60px | Corner triangles, branding band |

---

## III. Visual Theme

### Theme Style

- **Style**: Academic / institutional
- **Theme**: Light theme (white background)
- **Tone**: Professional, clean, blue-accented

### Color Scheme

| Role | HEX | Purpose |
| ---- | --- | ------- |
| **Background** | `#FFFFFF` | Page background |
| **Primary** | `#174994` | Decorations, bands, accent elements |
| **Primary light** | `#6D9FEC` | Lighter triangle decorations |
| **Accent** | `#2f5596` | Cover title color |
| **Body text** | `#000000` | Main body text |
| **Placeholder text** | `#174994` | Content area placeholder text |
| **White text on blue** | `#FFFFFF` | Text on blue backgrounds |
| **Chapter number bg** | `#17499461` | Large faded chapter number |
| **Stroke** | `#203864` | Shape stroke (0-width, invisible) |

### Gradient Scheme

No gradients used. Solid fills only.

---

## IV. Typography System

### Font Plan

**Preset**: P2 (Government / institutional docs)

| Role | Chinese | English | Fallback |
| ---- | ------- | ------- | -------- |
| **Title** | 微软雅黑 | 微软雅黑 | sans-serif |
| **Chapter** | 微软雅黑 | 微软雅黑 | sans-serif |
| **Content title** | 思源黑体 CN Bold | 思源黑体 CN Bold | sans-serif |
| **Body** | 思源黑体 CN Bold | 思源黑体 CN Bold | sans-serif |
| **Subtitle/info** | 微软雅黑 | 微软雅黑 | sans-serif |

**Font stack**: `微软雅黑, 思源黑体 CN Bold, sans-serif`

### Font Size Hierarchy

**Baseline**: Body font size = 32px

| Purpose | Size | Weight | Page Type |
| ------- | ---- | ------ | --------- |
| Cover title | 53px | 700 | cover |
| Cover subtitle | 26px | 700 | cover |
| TOC heading | 88px | 700 | toc |
| TOC item | 37px | 700 | toc |
| TOC number | 32px | 700 | toc |
| Chapter title | 88px | 700 | chapter |
| Chapter number (decorative) | 120px | 700 | chapter |
| Content page title | 37px | 700 | content |
| Content page header chapter num | 48px | 700 | content |
| Content body | 32px | 700 | content |
| Ending title | 80px | 700 | ending |
| Ending message | 32px | 700 | ending |
| Footer/info | 21px | 700 | cover |

---

## V. Layout Principles

### Page Structure

- **Cover**: Centered layout with title, group bar, author, date; blue bands top-left/right, corner triangles
- **TOC**: Left blue panel with "目录" title; right area with 5 numbered items + vertical divider
- **Chapter**: Centered chapter title with large faded number overlay; corner triangles
- **Content**: Header bar with chapter number/title + blue accent line; body area below; corner triangles bottom
- **Ending**: Centered title on blue banner; message below; corner triangles

### Spacing Specification

| Element | Value |
| ------- | ----- |
| Cover title vertical center | y≈227 |
| Content area top margin | ~90px from top |
| Content body left margin | ~118px |
| TOC items start | y≈170, spacing ~90px |
| Chapter title center | y≈312 |
| Ending title center | y≈336 |

---

## VI. Icon Usage Specification

No icons used. Decorative elements are geometric shapes (triangles, rectangles, circles).

---

## VII. Visualization Reference List

Not applicable — no data visualization in template layouts.

---

## VIII. Image Resource List

| Filename | Dimensions | Purpose | Type | Status |
| -------- | --------- | ------- | ---- | Existing |
| university_logo.png | ~279×59 (cover), ~226×48 (toc), ~360×76 (chapter) | University/institution logo | Decorative | Existing (embedded base64) |
| ucas_logo.png | ~407×86 | UCAS logo for ending | Decorative | Existing (embedded base64) |

---

## IX. Content Outline

### Cover (01_cover.svg)

- **Layout**: Centered, blue decorative bands and corner triangles
- **Title**: `{{TITLE}}` — paper/presentation title
- **Group**: `{{GROUP}}` — group identifier
- **Author**: `{{AUTHOR}}` — team member names
- **Date**: `{{DATE}}` — presentation date
- **Footer**: "University of Chinese Academy of Sciences" (preserved)

### TOC (02_toc.svg)

- **Layout**: Left blue panel + right numbered list
- **Items**: `{{TOC_ITEM_1}}` through `{{TOC_ITEM_5}}` — chapter/section names
- **Chrome**: Blue left panel, "目录" heading, numbered circles, vertical divider

### Chapter (02_chapter.svg)

- **Layout**: Centered title with large faded background number
- **Title**: `{{CHAPTER_TITLE}}` — chapter name
- **Number**: `{{CHAPTER_NUMBER}}` — chapter number (decorative background)

### Content (03_content.svg)

- **Layout**: Header with chapter number/title + blue line; body area below
- **Title**: `{{PAGE_TITLE}}` — section title
- **Content**: `{{CONTENT_AREA}}` — main body text
- **Chrome**: Chapter number "1/", chapter name header, blue accent line, corner triangles

### Ending (04_ending.svg)

- **Layout**: Centered on blue banner
- **Title**: `{{ENDING_TITLE}}` — closing message (e.g., "感谢聆听")
- **Message**: `{{ENDING_MESSAGE}}` — group info / date

---

## X. Speaker Notes Requirements

Not applicable for template import. Speaker notes are generated at presentation time.

---

## XI. Technical Constraints Reminder

### SVG Generation Must Follow:

1. viewBox: `0 0 1280 720`
2. Background uses `<rect>` elements
3. Text wrapping uses `<tspan>` (`<foreignObject>` FORBIDDEN)
4. Transparency uses `fill-opacity` / `stroke-opacity`; `rgba()` FORBIDDEN
5. FORBIDDEN: `clipPath`, `mask`, `<style>`, `class`, `foreignObject`
6. FORBIDDEN: `textPath`, `animate*`, `script`

### PPT Compatibility Rules:

- `<g opacity="...">` FORBIDDEN (group opacity); set on each child element individually
- Image transparency uses overlay mask layer
- Inline styles only; external CSS and `@font-face` FORBIDDEN