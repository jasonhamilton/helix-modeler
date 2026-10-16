<!-- Owner's specification for the status report (provided by the human, 2026-10-15). The status-reporter agent, the status-report skill and scripts/status_report.py implement it. Change only on the human's instruction. -->

# Status Reporter Agent — Human-Centered Reporting Redesign

## Mission

Redesign the project's status-reporting system to produce professional, human-readable status reports in **both HTML and Markdown**.

The existing reports contain valuable technical information but are difficult to understand from a high-level perspective. They contain too much implementation jargon, undefined abbreviations, repetitive content, dense summaries, and insufficient emphasis on actual problems and their consequences.

The purpose of these reports is to help a human understand the project's condition and make informed decisions—not to provide a raw activity log for other agents.

**The central question every report must answer is:**

> How close are we to achieving the project's actual objective, what is preventing us from getting there, and what should happen next?

Implement these changes permanently in the reporting agent, report generator, templates, and any supporting data structures.

Do not simply rewrite the current report.

---

# 1. Core Reporting Philosophy

Prioritize meaningful outcomes over implementation activity.

A human should be able to:

- **Within 30 seconds:** Understand the project's overall condition, major accomplishments, and most important problem.
- **Within 60 seconds:** Understand what works, what doesn't, and the major blockers.
- **Within 2 minutes:** Understand the next milestone, what needs to happen, and which decisions require human attention.
- **On demand:** Drill down into component details, technical evidence, tests, tasks, and implementation notes.

Use progressive disclosure. Important information comes first; supporting details appear later.

The report should read like a well-written engineering project review, not a transcript of agent activities.

Every significant statement should answer at least one of these questions:

1. What does this mean?
2. Why does it matter?
3. What is the consequence?
4. What should happen next?

If information answers none of these questions and is not needed for evidence or traceability, omit it from the main report.

---

# 2. Separate Project Health, Technical Progress, and Product Readiness

**This is a critical requirement.**

The existing reporting system tends to equate completed tasks, passing unit tests, and implemented components with actual project success.

These are not equivalent.

Every report must explicitly distinguish three dimensions.

## A. Project Health

Project Health evaluates whether the project is progressing toward its objectives and whether major risks threaten its success.

Statuses:

- **On Track:** Progress is consistent with established milestones, major risks are controlled, and the next important milestone is achievable under the current plan.
- **At Risk:** Important unresolved problems threaten the next milestone or overall objective.
- **Blocked:** Critical work cannot advance because of an unresolved dependency, failure, or required decision, with no viable alternative path currently available.
- **Complete:** The agreed project objectives and acceptance criteria have been satisfied.
- **Insufficient Information:** There is not enough reliable information to assess health.

Project Health must be based on actual risks, dependencies, milestones, and evidence.

Do not mark the project On Track simply because agents are completing tasks.

Do not claim schedule performance unless an established schedule exists.

If the project is at risk or blocked, explain the specific reason in plain English.

## B. Technical Progress

Technical Progress describes how far implementation and engineering work have advanced.

Show what has been:

- Implemented
- Integrated
- Tested
- Verified
- Left unfinished

Passing unit tests establishes that the tested behavior works under those conditions. It does not automatically establish real-world functionality.

Likewise, a component being marked "working" does not mean the overall product can accomplish its intended objective.

Technical Progress can be moving forward even while Project Health is At Risk.

Do not invent an overall completion percentage. Only use percentages when they have a documented, meaningful calculation.

## C. Product Readiness

Product Readiness measures whether the system accomplishes the actual user-facing objective.

Use clear readiness stages:

1. **Foundation:** Core infrastructure and supporting components exist.
2. **Prototype:** Important functionality can be demonstrated in isolation.
3. **Integrated:** The major components work together.
4. **Validated:** The system meets measurable success criteria under representative conditions.
5. **Ready:** The agreed product requirements have been satisfied and the system is suitable for its intended use.

These stages describe the highest demonstrated level of readiness, not a percentage estimate.

Always explain what is preventing progression to the next stage.

Do not infer that the full system is integrated merely because its individual components exist.

## Dashboard Presentation

Present these three dimensions together near the top of both reports:

**Project Health:** At Risk  
Reason: The primary tone-matching method has not yet been validated.

**Technical Progress:** Advancing  
Reason: Both audio engines work, and the system can generate hardware-compatible presets.

**Product Readiness:** Foundation / Prototype  
Reason: Individual components exist, but the application cannot yet automatically turn a song into a verified matching preset.

These are illustrative statements based on the current report, not fixed values. Derive the actual assessment from current project evidence.

---

# 3. Required Report Structure

Both HTML and Markdown outputs must follow the same logical structure and communicate the same conclusions.

## Section 1 — Executive Dashboard

This is the most important section.

It must immediately answer:

- What are we building?
- Where do we stand?
- What actually works today?
- What doesn't work yet?
- What's the biggest problem?
- What's next?

Include:

**Project Goal**

One or two sentences describing the desired final product in plain English.

**Project Health, Technical Progress, and Product Readiness**

Three distinct assessments with concise explanations.

**Executive Summary**

A maximum of approximately 100–150 words explaining the project's present state in plain English.

The summary should focus on outcomes, not the names of implementation components.

Do not include a wall of technical details, measurements, file names, test IDs, agent names, or unexplained abbreviations.

**Biggest Problem**

Prominently identify the single most consequential issue.

Explain:

- What is wrong or missing.
- Why it matters.
- What it prevents.
- What needs to happen to resolve it.

If no major problem exists, explicitly say so rather than manufacturing one.

**Next Milestone**

Describe the next meaningful milestone, its current readiness, and what must happen to achieve it.

## Section 2 — Key Problems & Blockers

Present the most important unresolved issues, prioritized by their consequences.

Each issue must include:

- **Problem:** What is wrong or missing?
- **Impact:** What does it prevent or jeopardize?
- **Cause:** What is known about the reason?
- **Resolution:** What needs to happen?
- **Owner / Next Step:** Who is responsible and what is the next action?
- **Priority:** Critical, High, Medium, or Low.
- **Classification:** Active Blocker, Confirmed Problem, Risk, or Unknown/Unverified.

Avoid treating all incomplete features as blockers.

Separate confirmed failures from missing evidence.

Example:

**High Priority — Tone-matching accuracy is not validated**

The system has several ways to measure differences between guitar tones, but none has passed the required accuracy tests on audio generated by the actual guitar-processing engines.

**Impact:** The optimization system cannot yet be trusted to select a genuinely matching preset.

**Next step:** Complete the known-answer benchmark and evaluate the comparison methods against actual engine output.

Do not overwhelm the reader with raw metric names unless they materially explain the issue.

Limit the executive-level issue list to approximately 3–5 meaningful issues. Keep additional items in the detailed section.

## Section 3 — Milestone Progress

Show progress toward the project's actual functional objectives.

For each milestone, include:

- Goal
- State: Complete, In Progress, At Risk, Blocked, or Not Started
- What has been demonstrated
- What remains
- What's blocking completion, if anything
- Next required action

Distinguish foundational milestones from product-level milestones.

Make the upcoming milestone more prominent than distant milestones.

Use existing milestone definitions and acceptance criteria. Do not invent milestones, deadlines, or passed criteria.

## Section 4 — What Works / What Doesn't

Create a simple, scannable overview.

Use three categories:

**Working and Verified**

Capabilities demonstrated under the claimed conditions.

**Implemented but Not Yet Proven**

Capabilities that exist but lack sufficient realistic testing or outcome validation.

**Missing or Not Working**

Capabilities not implemented, currently failing, or unavailable.

Use concrete capabilities, not vague development activities.

Example:

| Capability | Current Reality |
|---|---|
| Control audio-processing engines | Working on the tested platform |
| Generate hardware-compatible presets | Demonstrated on a limited set |
| Reliably identify a matching guitar tone | Not yet proven |
| Convert a song into a matching preset automatically | Not implemented end to end |

Avoid large tables containing paragraphs of technical explanation.

## Section 5 — Human Decisions & Actions Needed

Create one authoritative, deduplicated action list.

The current report repeats the same action across multiple features. This must be eliminated.

Group actions into:

**Blocking Now**

Actions immediately required to enable important work.

**Needed Soon**

Decisions or inputs that should be addressed before an upcoming stage.

**Later / Optional**

Valid actions that do not require attention yet.

For each action include:

- Clear human-readable description.
- Why it is needed.
- What it unblocks.
- Recommended choice, if supported by evidence.
- Consequence of deferring it.
- Priority.

Do not include routine tasks owned by agents as human actions.

Do not ask for a decision prematurely when it can safely wait.

Do not repeat the same action in different wording.

For example, if three components require the same reference recordings, show one consolidated action explaining all the capabilities those recordings will unblock.

## Section 6 — What Changed Since Last Report

Show meaningful progress since the previous report.

Include:

- Important new capabilities.
- Problems resolved.
- New problems discovered.
- Milestones achieved or advanced.
- Significant test results.
- Decisions that changed the plan.

Focus on consequences.

Bad:

"Completed T025, updated envelope v1, fixed cost cache."

Better:

"Search performance improved substantially after correcting a rendering-cost calculation. However, improving the comparison metric has not yet made it reliable enough for tone matching."

Separate genuine improvement from activity that did not improve overall readiness.

Only claim a change when historical records support it.

If a previous baseline is unavailable, say so.

## Section 7 — Component Status

Group features into meaningful functional areas rather than listing every internal module or agent independently.

Each component should contain:

**Component Name**

A descriptive, human-readable name.

**Purpose**

What this component contributes to the overall objective.

**Status**

Working, Partially Working, Not Working, Not Started, or Blocked.

**What Works**

One or two concise sentences.

**What's Missing**

One or two concise sentences.

**Primary Problem**

The most important issue affecting this component.

**Next Step**

The next meaningful action.

**Evidence Confidence**

Indicate the strongest relevant verification, without implying that hardware tests of one operation validate an entire feature.

Avoid repeating the same information across component tables and expanded details.

## Section 8 — Technical Details & Evidence

Preserve access to:

- Source files
- Internal tasks and IDs
- Agent ownership
- Detailed measurements
- Test results
- Raw ledger records
- Technical investigations
- Implementation decisions
- Dependency constraints
- Licensing findings
- Risks and limitations

Technical details should support the report's conclusions rather than dominate its presentation.

Keep references traceable to authoritative project evidence.

---

# 4. Human-Friendly Language Rules

The report must be written for a technically capable human who is not intimately familiar with the codebase.

### Avoid unexplained abbreviations

Spell out or explain abbreviations on first meaningful use.

Examples:

- DI → Direct Input: a clean, unprocessed guitar recording.
- DSP → Digital Signal Processing.
- EULA → Software license agreement.
- CMA-ES → An optimization algorithm used to search for suitable settings.

Do not expand acronyms mechanically if doing so is still confusing. Explain their role.

### Add a glossary

Maintain a glossary of relevant terms.

In HTML, technical terms may have hover/click definitions.

In Markdown, include a compact glossary or links to a definitions section.

Both outputs must provide access to the same terminology explanations.

### Use meaningful headings

Prefer:

"Why the matching algorithm isn't reliable yet"

Over:

"LossKit v1 held-out proxy rank-1 results"

Prefer:

"Preset generation works, but automation is incomplete"

Over:

"Preset schema and snapshot builder"

### Explain technical measurements

Only include metrics prominently when they affect a decision or materially explain progress.

Provide context:

- What is being measured?
- Is higher or lower better?
- What is the target or baseline?
- What does the result imply?

Do not dump unexplained decimal values into summaries.

### Avoid agent-oriented language

Do not center reports around agent cycles, handoff terminology, internal work modes, or task bookkeeping.

Agent-related information belongs in the technical appendix unless it directly affects the project's outcome.

---

# 5. HTML Report Requirements

Generate a polished, responsive HTML dashboard.

The HTML output is intended to be the primary interactive visual report.

### Layout

Use:

1. Clean report header
2. Executive dashboard
3. Prominent primary problem
4. Milestone progress visualization
5. Prioritized issues and blockers
6. Human actions
7. Recent progress
8. Component summaries
9. Expandable technical details

### Design Guidelines

- Professional typography and spacing.
- Clear heading hierarchy.
- Strong visual separation between major sections.
- Concise cards rather than paragraphs inside oversized tables.
- Restrained use of color.
- Visually distinct health, risk, and readiness indicators.
- Responsive desktop and mobile design.
- Accessible contrast and semantic HTML.
- Light and dark mode support.
- Readable status indicators that do not depend on color alone.
- Navigation links or a table of contents for lengthy reports.
- Expandable details using accessible disclosure controls where appropriate.

Use visual emphasis for genuinely important problems, not every metric or status label.

The biggest blocker should be immediately visible.

Do not make the page feel like a technical log rendered as a table.

Keep the executive dashboard compact enough to understand without extensive scrolling on a desktop screen.

Important content must remain readable without JavaScript. JavaScript may enhance filtering or navigation but must not be required to reveal critical status information.

---

# 6. Markdown Report Requirements

The Markdown output is equally important.

It is not a plain-text dump of the HTML dashboard.

It must be a carefully formatted, standalone, readable status report suitable for:

- GitHub
- Code review
- Agent handoffs
- Terminal-oriented workflows
- AI-assisted project review
- Documentation archives

### Markdown Formatting

Use standard GitHub-Flavored Markdown where practical.

Include:

- Clear heading hierarchy.
- Concise introductory summary.
- Bold status labels.
- Short paragraphs.
- Compact tables where useful.
- Bulleted action items.
- Clickable references to project files and supporting documents.
- A glossary for required technical terms.
- Detailed technical evidence in later sections.

Avoid:

- Huge tables with paragraph-length cells.
- Unnecessarily deeply nested lists.
- Excessive emoji.
- Long unstructured paragraphs.
- Repeated component descriptions.
- HTML-only styling needed to understand the document.
- Raw, unexplained benchmark output.

Do not depend on JavaScript, CSS, or browser-specific features.

The Markdown report should remain clear when read as plain text.

### Markdown Executive Dashboard

Use a concise structure such as:

**Project Health:** At Risk  
**Technical Progress:** Advancing  
**Product Readiness:** Prototype

**Summary:** Both processing engines work, but the complete tone-matching workflow has not been demonstrated.

**Biggest Problem:** The comparison method used to identify matching tones has not passed validation.

**Next Milestone:** Demonstrate automatic recovery of known preset settings.

Follow this with prioritized problems, actions, milestones, and component details.

The actual content must be generated from current evidence.

---

# 7. Single Source of Truth for HTML and Markdown

Both outputs must be generated from the same structured report model.

Do not maintain two separately written summaries or independent status interpretations.

Recommended architecture:

**Project sources → Evidence collection → Status analysis → Structured report model → HTML renderer + Markdown renderer**

The structured report model should contain, at minimum:

- Project goal
- Report date and reporting period
- Project Health assessment and rationale
- Technical Progress assessment and rationale
- Product Readiness stage and rationale
- Executive summary
- Primary problem
- Milestones
- Prioritized issues
- Consolidated human actions
- Recent changes
- Component statuses
- Evidence references
- Glossary entries
- Technical details

Use existing project data structures where possible rather than duplicating records unnecessarily.

### Consistency Requirements

Both outputs must agree on:

- Overall status
- Product readiness
- Important accomplishments
- Primary blocker
- Milestone states
- Major risks
- Human actions
- Key technical findings

The HTML report may offer visualizations, expandable sections, and filters.

The Markdown report should express equivalent information through headings, concise summaries, tables, and links.

Neither output should silently omit a critical risk, blocker, or required decision.

### Appropriate Differences

Visual representation may differ by format.

For example:

- HTML: Colored milestone timeline.
- Markdown: Milestone status table.
- HTML: Expandable feature detail cards.
- Markdown: Linked detailed feature sections.
- HTML: Tooltips for terminology.
- Markdown: Compact glossary.

Do not compromise Markdown readability to force identical visual markup.

---

# 8. Accurately Interpret Evidence

Do not exaggerate the project state.

Explicitly distinguish:

- Implemented but untested
- Unit-tested
- Tested using simulated or synthetic data
- Tested using actual hardware
- Tested using representative real-world data
- Validated against defined acceptance criteria

These evidence types are not interchangeable.

For example, a successful hardware smoke test demonstrates a particular hardware operation, not necessarily the complete matching workflow.

A successful test with a fake processing engine does not establish that the matching system works with the real engine.

A feature can have every scheduled task completed and still fail its intended success criteria.

### Status Classification

Determine status from demonstrated capability and requirements, not simply closed-task counts.

When information is missing, report it as unknown or unverified.

Do not convert missing evidence into either a confirmed failure or a success.

Use the existing project acceptance criteria and validation thresholds.

Do not silently change the project's milestones or rewrite requirements to make the report more favorable.

When source documents disagree, identify the discrepancy and use verified evidence for the corresponding conclusion.

---

# 9. Prioritization Rules

Not every unfinished item belongs in the main report.

Rank issues using:

1. Impact on achieving the project's primary objective.
2. Whether the issue blocks other work.
3. Severity if left unresolved.
4. Urgency relative to the next milestone.
5. Availability of a practical resolution.

Do not rank issues merely by task count or time spent.

A single unresolved design or validation problem can be more important than ten completed implementation tasks.

Likewise, a routine documentation issue should not compete visually with an unresolved problem threatening the product's core functionality.

Focus the executive view on the few issues that materially affect progress.

---

# 10. Permanent Implementation Requirements

Inspect and update the existing status-reporting system, including the appropriate:

- Status-reporter agent instructions.
- Report-generation scripts.
- HTML templates and styles.
- Markdown rendering logic.
- Structured reporting data.
- Supporting tests.
- Documentation and usage instructions.

Do not manually edit only the generated output.

Implement the redesign so subsequent report-generation runs automatically produce the improved reports.

Preserve compatibility with the project's existing evidence, ledger, task, and milestone systems wherever practical.

Avoid introducing unnecessary dependencies or a large new reporting framework.

Keep generated reports deterministic where practical.

Ensure both report formats can be regenerated from the same project snapshot without requiring manual intervention.

---

# 11. Validation and Acceptance Tests

The redesign is complete only when all criteria below are satisfied.

### Communication

- A reader can understand project health in 30 seconds.
- The biggest problem is immediately visible.
- The executive summary contains no unexplained technical jargon.
- The report makes it clear what the product can actually do today.
- The next important milestone is obvious.
- Every critical issue explains its impact and next action.

### Accuracy

- Technical Progress and Product Readiness are separate assessments.
- Passing tests are not misrepresented as proven real-world performance.
- Completion percentages are never invented.
- Unknown results are not silently presented as successes or failures.
- Significant conclusions are traceable to evidence.

### Actionability

- Human actions are deduplicated.
- Actions are prioritized by urgency and impact.
- Future hypothetical actions are not presented as immediate obligations.
- Each major blocker has an identified next step.

### HTML

- The dashboard has strong information hierarchy.
- Layout works on desktop and mobile.
- Technical information can be accessed without overwhelming the initial view.
- Accessibility and light/dark presentation remain functional.

### Markdown

- The document is readable independently of HTML.
- Its executive summary is concise and clear.
- Tables are not excessively wide or verbose.
- Detail sections are navigable.
- Evidence references and the glossary remain accessible.

### Cross-Format Consistency

- Both reports use the same structured data.
- Important status and risk conclusions match.
- The human action list is consistent.
- Neither format silently loses important information.

### Regression Testing

Add or update tests to verify the report structure, required fields, status consistency, HTML generation, Markdown generation, and duplicate-action handling.

Where possible, include representative report fixtures that validate both outputs.

---

# 12. Implementation Workflow

Carry out the following steps:

**Step 1 — Audit the existing reporting system.**

Identify problems in the existing narrative, status definitions, information hierarchy, HTML presentation, Markdown presentation, and underlying data model.

**Step 2 — Define the shared report structure.**

Create or adapt the structured report representation so both formats are generated from the same analysis.

**Step 3 — Implement human-readable status interpretation.**

Add the separation between Project Health, Technical Progress, and Product Readiness.

Ensure problems, impacts, milestones, and next actions are derived from actual evidence.

**Step 4 — Redesign the HTML output.**

Implement the new executive dashboard, problem presentation, component summaries, and progressive technical detail views.

**Step 5 — Redesign the Markdown output.**

Create a standalone, concise, navigable report that communicates the same information without relying on HTML-specific presentation.

**Step 6 — Validate against the current project.**

Regenerate both reports and inspect them as a human reader would.

Verify that the key conclusions, blockers, and actions are accurate.

**Step 7 — Update tests and agent instructions.**

Ensure future runs continue to produce the improved output.

**Step 8 — Report the implementation results.**

Provide a concise summary of:

- What was changed.
- Which files were modified.
- Where the generated HTML and Markdown reports are located.
- What was verified.
- Any limitations or unresolved issues.

---

# Final Guiding Principle

**A status report should explain the project's reality, not merely document its activity.**

A useful report can simultaneously communicate:

- Engineering work is advancing.
- Important underlying capabilities are operational.
- The main user-facing functionality is not yet proven.
- A specific unresolved problem is preventing the next milestone.
- Certain actions will unblock progress.

Those statements are not contradictory. Together, they describe the project's actual condition.

Build a reporting system that communicates that distinction clearly, accurately, and efficiently in both HTML and Markdown.

The goal is a concise, professional project-health dashboard backed by detailed, accessible engineering evidence—not a technical audit log disguised as a status report.

---

# Addendum (owner, 2026-10-15): task references

Any report that references a task must give a relative link to that task in the GitHub repository, and every task number must show the task's name on hover. In Markdown: `[T042](../../board/TASKS.md#t042 "Play-along user-DI capture")`. In HTML: `<a href="…#t042" title="Play-along user-DI capture">T042</a>`. Links point at the per-task anchors in `board/TASKS.md` (or `board/CYCLES.md` once archived) defined in `contracts/board.md`.
