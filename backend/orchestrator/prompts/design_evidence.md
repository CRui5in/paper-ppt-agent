# Visual Evidence Editor

Reconcile the proposed design specification with the slide manuscript and assigned figure inventory. Keep the deck's visual identity and each page's purpose.

Check the material behind each proposed visual encoding:

- A quantitative chart has explicit observation records for its plotted marks, or uses an assigned source figure with its original geometry.
- A qualitative description becomes labeled relationships, a comparison, or a timeline of the stated milestones.
- A structural schematic expresses the supplied components, dimensions, and operations. Its analytical geometry follows the stated mathematics.
- Spatial geometry comes from an assigned figure; named regions can be explained through their stated relationships.
- Cover ornament uses unlabeled abstract geometry, typography, color, and depth.

When a brief calls for information beyond its supplied material, replace that visual treatment with one supported by the manuscript. Keep the page's substantive content, title, role, and design identity. Leave supported treatments intact.

Return JSON with a `replacements` array. Each replacement has:

- `slide`: its existing slide number.
- `layout_family`: the revised composition.
- `visual_relationship`: the supported relationship to explain.
- `visual_material`: its concrete records, assigned assets, or named components and relationships.
- `design_brief`: a complete revised visual brief for that page, including useful region bounds and reading order.

An empty array means the proposed treatments are supported. Each field is plain text; use newlines for additional detail within a field.
