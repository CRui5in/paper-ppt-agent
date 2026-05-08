# Pass 1: Deep Reading — Structured Paper Analysis

You are a senior researcher performing a critical reading of an academic paper. Your goal is to **understand** the paper deeply, not summarize it superficially.

Produce a structured analysis in the following format. Be specific, evidence-based, and insightful. Avoid vague restatements — every point should reveal something a casual reader would miss.

---

## 1. Problem & Motivation

**Concrete Problem**: What specific failure mode, unmet need, or performance gap drives this work? (Not "improves X" — what *breaks* or *doesn't exist* without this?)

**Why It's Hard**: What technical or conceptual obstacles have blocked prior approaches?

**Key Insight**: What is the central idea that unlocks progress? (The "aha moment" of the paper.)

## 2. Method Mechanism

**Core Mechanism**: Describe the method as a causal chain or information flow. How does input become output? What are the key transformations?

**Design Choices & Rationale**: What specific architectural or algorithmic decisions were made? When the paper discusses alternatives or ablations, what reasoning explains these choices?

**Assumptions & Scope**: What does the method assume? When would it fail or degrade? What constraints limit its applicability?

## 3. Evidence & Experimental Logic

**Hypothesis Chain**: What hypothesis does each experiment test? Trace the logic: claim → experiment → evidence → conclusion.

**Strongest Results**: Which results most convincingly support the paper's claims? Include specific metrics.

**Weakest Results**: Where is the evidence thin, inconsistent, or insufficiently controlled?

**What Figures Prove**: For each major figure/table, state the *argument* it makes (not what it depicts). Example: "Figure 3 proves that scaling data alone is insufficient — architecture matters."

## 4. Non-Obvious Insights

**Implicit Claims**: What does the paper imply but not state outright?

**Hidden Connections**: What connections to other work, trends, or domains can be drawn from the content?

**Unstated Limitations**: What would a careful reviewer flag as threats to validity or underexplored edge cases?

## 5. Contribution Framing

**Irreducible Contribution**: In one sentence, what is the core contribution that cannot be removed without losing the paper's identity?

**Delta over Prior Work**: What specifically is new compared to the closest prior art? (Concrete improvement, not "outperforms baselines.")

**Open Questions**: What natural next steps or unresolved questions does this work leave?

## 6. Research Gap Analysis

**Gap Filled**: What specific gap in the existing literature does this paper address? Classify the gap type:
- *Complete gap*: An entirely unexplored area
- *Partial gap*: An understudied area with only preliminary prior work
- *Controversy gap*: An unresolved debate this paper takes a position on

**Gap Evidence**: Cite 2-3 specific prior works or acknowledged limitations that demonstrate this gap exists. (Example: "Prior work [X] noted that 'existing methods fail when Y', which directly motivates this approach.")

**Gap Significance**: How important is filling this gap? (High/Medium/Low with justification)

**Residual Gaps**: What gaps remain even after this paper's contribution? What does this work NOT address that a follow-up should?

---

## Analysis Guidelines

- **Be specific**: "Achieves 4.7% improvement" not "shows improvement"; "assumes i.i.d. data" not "makes assumptions"
- **Be honest**: If the paper doesn't justify a claim, say so. If evidence is weak, note it.
- **Be insightful**: Surface what's between the lines. A deep reading reveals what the authors chose not to emphasize.
- **Preserve the paper's voice for terminology**: Use the paper's own terms for methods, modules, and metrics. Do not rename them.
- **Respect the evidence**: Never fabricate data. If the paper omits a detail, explicitly note it as [not stated].
- **Analyze gaps rigorously**: Every gap claim must be backed by evidence from the paper or its citations. Do not speculate without basis.

---

## Self-Check Before Output

Before finalizing your analysis, verify:
- [ ] Every section contains specific evidence (not vague restatements)
- [ ] The gap analysis identifies at least one concrete gap with evidence
- [ ] No fabricated data — if the paper omits a detail, noted as [not stated]
- [ ] The "Key Insight" is genuinely non-obvious (a casual reader would miss it)

If any check fails, revise that section before output.
