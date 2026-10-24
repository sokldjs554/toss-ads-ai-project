# Current Evidence Update Implementation Plan

> Execute inline in the authorized isolated checkout. User requested GitHub, README, demos and application documents be updated together.

**Goal:** Align current public summaries with the c51d574 handoff without changing policy outcomes or inventing quality improvements.
**Architecture:** CURRENT_STATUS.md is the current evidence index. README and the app link to it; dated experiment files remain historical.
**Tech Stack:** Markdown, existing Streamlit presentation layer, static HTML, DOCX/PPTX/PDF.
**Spec:** User instruction in this session; final-handoff-20261024.md at c51d574.

## Global Constraints
- Preserve actual model, evaluation labels and outcomes. No new paid calls or account/data mutations.
- One person, six requests, six judgments: three approvals and three edits. Ten successful API calls are that person's cohort only.
- Preserve unresolved semantic review, six-condition coverage, policy 4/16 and decline 7/12 and 13/24 limitations in plain language.
- Use current code CI separately from historical model evidence at 3f78335.

## Review Focus
- Historical counts must not appear as current totals.
- Public summary must require neither login nor provider calls.
- Current UI build must remain dynamic, not hardcoded to evidence version.
- LIVE and shared-review databases must not be conflated.
- Evidence links must be pinned or resolve on main.

## Tasks
- [x] Update README, CURRENT_STATUS and claims; identify historical status pages.
- [x] Update app/landing explanatory copy; run existing UI tests and site build.
- [ ] Review changes, commit to main, verify CI/Pages and public server build.
- [ ] Update editable application files and PDFs, render every page, save current versions.

Ruling: This is an authorized copy/evidence synchronization. No new features or policy fixes are claimed; existing tests verify unchanged interaction paths. No new implementation-mirroring tests are added.

Verification: 45 existing UI/site tests passed; build_site.py succeeded. First attempted test command referenced nonexistent test_site_build.py and collected no tests; corrected to existing test_review_persistence_site.py. Fresh read-only review found no critical/important issue.
