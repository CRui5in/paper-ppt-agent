# Icon Decoration Planner

Decide which pages get icons and which stay clean, based on per-page candidates.

## Rules

- **Target**: chapter dividers, process steps, KPI highlights, warning/failure cards.
- **Skip**: dense data slides, text-heavy explanation slides, slides with paper figures.
- **Distribution**: icons in the same section should follow a consistent rhythm.
  Do NOT put an icon on page 3 and page 7 but skip 4-5-6 — either use icons throughout a section or not at all.
- **Max 1 icon per page**. Most pages: `None`.
- **Only** use icon paths from the candidate list.
- Placeholder syntax: `<use data-icon="lib/name" x="" y="" width="" height="" fill=""/>`

## Per-page Candidates

{per_page_candidates_table}

## Output

Return a markdown table only — no extra text:

| Page | Icon | Reason |
|------|------|--------|
| 01   | None | cover  |
| 02   | chunk/target | section: goal |
