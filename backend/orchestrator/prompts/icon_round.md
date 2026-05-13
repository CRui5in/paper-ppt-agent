# Icon Layout Planner

Decide which pages get exactly one semantic icon and which pages stay clean.
This is a separate icon-only round, so you may inspect the full icon catalog
without worrying about distracting the main layout/content planning step.

## Core Decision Order

1. Decide whether an icon has a real semantic job on the page.
2. If yes, choose the exact icon path.
3. Reserve the icon's visual slot before text, charts, or cards are placed.
4. Describe the content block the icon should anchor.

## Hard Rules

- **Forbidden page types**: cover, chapter/transition, TOC/agenda, and ending pages must use `None`.
- **Max 1 icon per page**.
- **Use exact paths only** from the shortlist or full catalog below. Never invent icon names.
- **Icons are layout anchors, not decoration**. Do not place a floating sticker in a random corner.
- **Prefer `None`** for dense data slides, text-heavy explanations, paper-figure slides, or pages where the icon would only fill empty space.
- **Prefer an icon** on eligible content pages when it naturally anchors a process step, method/module, KPI/result callout, warning/limitation, key contribution, or explicit action.
- Do not force section-wide rhythm. Each page is judged independently by semantic fit and placement naturalness.
- Placeholder syntax downstream: `<use data-icon="lib/name" x="" y="" width="" height="" fill=""/>`

## Natural Placement Rules

- Put the icon next to the thing it explains: callout title, process node, KPI label, module header, warning title, or contribution statement.
- Reserve a stable slot such as `left of callout title`, `inside process node header`, `beside KPI number`, or `top-left of method card`.
- Avoid vague placement like `corner`, `decorate`, `background`, `near title`, or `beside content`.
- Size should usually be `28x28px`, `32x32px`, `36x36px`, or `40x40px`. Use larger only when the page is a sparse eligible content page.

## Per-page Shortlist and Context

{per_page_candidates_table}

{full_icon_catalog}

## Output

Return a markdown table only. Include every page exactly once.

| Page | Icon | Role | Anchor | Placement | Size | Reason |
|------|------|------|--------|-----------|------|--------|
| 01 | None | forbidden | cover page | none | none | cover pages must be icon-free |
| 02 | chunk/target | KPI/result anchor | result callout title | left of callout label before the metric text | 32x32px | target icon directly labels objective/result |
