# Preflight evidence strengthening

> Execution: inline, following systematic-debugging and test-driven-development. User authorized implementation, main integration and existing deployment; no additional approval for these steps.

**Goal:** reduce remaining sustained-change misses without hiding false alarms; provide auditable policy evaluation and real-use preparation.

**Baseline:** main/Pages/Render d48405926713eada260eb81049d1cc4ba2f65f68; clean checkout, branch fix/preflight-evidence-20261023. Prior CI 4441 and browser 32 are historical, not new validation.

**Architecture:** retain policy and workflow protections. Investigate the uncertainty of forecasting from short earlier references; test a joint weekday/trend/level-change fit only if it addresses the reproduced failure. Freeze final-check synthetic inputs before implementation, keep development cases separate. Reuse policy comparison, trace, budget, queue and evaluation code.

**Constraints:** no paid calls without new explicit approval; no operational account/review mutations; no deleted keyword restoration; no application-document edits; no claim of independent human truth or production experience.

## Tasks
- [x] Preserve baseline results for original 53, historical 80 and development 48 performance cases. Diagnose missed changes versus noise and sample status.
- [x] Freeze new final-check inputs/labels/scoring and provenance. Add a red test for the identified algorithmic weakness, implement minimum change, run normal-variation, sparse-sample, direction and report guards. Never tune on final results.
- [x] Audit existing policy datasets and same-input comparison. Freeze new policy checks and reviewer worksheet; execute free deterministic evaluation and explicit failure simulations. Real-model comparison remains unexecuted unless approved.
- [x] Audit actual-use controls and record evidence by code/test; fill only observed gaps. Prepare human workflow, feedback form and counterbalanced time experiment. Keep live authenticated persistence unverified where credentials are unavailable.
- [x] Record decisions and contribution provenance; refresh README/evaluation UI with actual results, no historical result replacement.
- [ ] Run full CI and gates, integrate, deploy Pages/Render, verify public builds and workflow in real browser, preserve screenshots, provide claim verification table.

## Review focus
Continuing slopes versus new changes; recovered events versus persistent events; low sample and high variance; model failure versus synthetic fallback; raw input/credential leakage in logs. All evaluation labels are project-defined synthetic hypotheses until independently reviewed.

Outcome: candidate a545f60 failed the pre-frozen final check (11→10/24 detection, 2→3/72 false alarms), reverted in faa7d78. Production insights unchanged; remaining weaknesses explicitly open. No paid API calls or operational DB writes.
