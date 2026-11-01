# SVG Layout Editor

Correct the reported layout and information defects in the supplied authoring SVG. The page manuscript defines its content; the current SVG defines its visual identity. Reallocate regions, reflow paragraphs, and coordinate containers, labels, equations, and connectors to fit the complete material.

Each editable element has a `data-editor-id`. Return JSON with an `edits` array. Every edit names its `target` using that ID and applies one or more of these operations:

- `attributes`: an object of SVG attribute names and new string values.
- `replace`: XML for one replacement element or group.
- `append`: XML for children to append to that element.
- `remove`: `true` to remove a redundant element.

Example:

```json
{"edits":[{"target":"e12","attributes":{"y":"320","height":"180"}},{"target":"e18","replace":"<text x='80' y='350' font-size='18'>Complete explanation</text>"}]}
```

Use coordinated edits when several regions need new bounds. Retain the assigned viewBox, source facts, values, notation, and comparison conditions. Complete the selected table records with their labels. Structural pages keep their title, metadata, agenda, or closing role.

Equations use their existing `data-latex` and `data-math-box="x y width height"`, with positive width and height; the renderer creates their vector paths. Allocate visible space for each expression. Paragraphs use `data-textbox` and continuous text with inline emphasis; the renderer measures and wraps them. Keep the supported native SVG vocabulary and the supplied local figure paths.

Tables and diagram labels use independently positioned cells or labels. Keep their groups distinct from paragraph-flow groups.

Return the JSON edits directly.
