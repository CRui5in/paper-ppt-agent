# Pass 4: Self-Evaluation & Revision

You are a presentation quality review panel. Evaluate the slide manuscript from **three reviewer perspectives**, then revise any slides that fall below threshold.

---

## Step 1: Multi-Reviewer Assessment

Review the manuscript from three distinct perspectives. Each reviewer scores the seven dimensions 1-5.

### Reviewer A: Domain Expert
Focus on **accuracy and depth**. Ask: Does this correctly represent the paper's contribution? Are the technical details precise?

### Reviewer B: Presentation Designer
Focus on **narrative and visual fitness**. Ask: Does this tell a compelling story? Would each slide hold the audience's attention? Are visuals used where they matter?

### Reviewer C: Target Audience Advocate
Focus on **insight density and audience reach**. Ask: Would a knowledgeable non-specialist understand this? Is every slide worth the audience's time? Are jargon and assumptions explained?

---

## Seven-Dimension Scoring (per reviewer)

| Dimension | Question | Slides to Check |
|-----------|----------|-----------------|
| **Depth** | Does each slide reflect understanding, not paraphrasing? | Slides with copy-pasted paper sentences |
| **Narrative** | Does the deck tell a coherent story with momentum? | Slides lacking narrative role or transition |
| **Insight Density** | Is every slide worth the audience's attention? | Slides with only generic/tangential content |
| **Visual Fitness** | Are visual markers used where they improve comprehension? | Data-heavy slides lacking visual markers |
| **Accuracy** | Are all claims traceable to the paper? | Slides with unverified or fabricated claims |
| **Logic** | Does the argument flow build convincingly? | Slides with logical gaps to neighbors |
| **Audience Reach** | Can a knowledgeable non-specialist follow? | Slides with undefined jargon or missing context |

---

## Step 2: Consolidated Assessment

Combine the three reviewers' scores into a consensus:

```
Consensus Scores:
- Depth: [1-5] — [consensus justification]
- Narrative: [1-5] — [consensus justification]
- Insight Density: [1-5] — [consensus justification]
- Visual Fitness: [1-5] — [consensus justification]
- Accuracy: [1-5] — [consensus justification]
- Logic: [1-5] — [consensus justification]
- Audience Reach: [1-5] — [consensus justification]
Total: [X/35]

Key Disagreements (if any):
- [Dimension]: Reviewer A scored [X], Reviewer C scored [Y] — reason: [brief explanation]

Issues Found (unified):
- Slide N: [issue description] → [fix description]
- Slide M: [issue description] → [fix description]
```

---

## Step 3: Revised Manuscript

If consensus total score < 28/35 or any dimension < 3:
- Revise the identified slides addressing concerns from ALL reviewers who flagged them
- Output the **complete revised manuscript** with all slides (unchanged slides preserved verbatim)

If consensus total score ≥ 28/35 and all dimensions ≥ 3:
- Output the manuscript **unchanged**, preceded by: "QUALITY_CHECK_PASSED"

---

## Common Issues & Fixes

| Issue | Detection | Fix |
|-------|-----------|-----|
| Surface bullets | Slide restates paper text verbatim | Rewrite as insight: "Why does this matter?" not "What did they do?" |
| Missing narrative | Slide lacks connection to neighbors | Add transition logic; ensure slide answers "So what?" |
| Data without context | Number appears without proof point | Add what the number proves: "4.7% improvement → proves X works even when Y fails" |
| Filler slide | Slide restates previous slide's point weaker | Merge with adjacent or replace with new insight |
| Jargon barrier | Technical term used without explanation | Add brief in-context clarification |
| Missing visual | Dense data without visual marker | Add `[CHART:...]` or `[COMPARISON:...]` marker |
| Logic gap | Slide N+1 doesn't follow from Slide N | Add transitional bullet or restructure order |
| Audience alienation | Assumes domain knowledge the audience lacks | Add a bridging analogy or brief framing sentence |
| Unsubstantiated claim | Strong statement without paper evidence | Either add the specific evidence or soften the claim |
