# Evidence index · 2026-10-23

All model-like execution in this folder is offline unless explicitly linked to historical live evidence. No provider API calls in this task. No production DB changes.

- `development-before`, `original-before`, `historical80-before`: baseline d484, results under evals/reports/strengthening-20261023.
- `level-shift-red`: 2 failed / 7 passed; `level-shift-green`: 66 passed on the experimental candidate.
- `development-candidate-frozen`: exact a545 candidate, 8/12 detection, 0/36 false alerts.
- `final96-before` / `final96-after`: frozen final input on d484 / a545; candidate was rejected and reverted, not deployed.
- `development-after`, `original-after`, `historical80-after`: intermediate candidate evidence, not final deployed improvement.
- `insights-regression`: candidate had 372 pass / 1 failure from old saved numbers becoming stale. `historical-replay-recheck` tested rejection of stale reference numbers. Both candidate and that test modification were subsequently reverted.
- `operations-red`: three reproduced failures. `spending-red`: two failures. `server-access-red`: two absent-interface failures before adding the access gate.
- `operations-final`: test fixture omitted required LLMResponse.parsed; one failure/10 pass. `operations-final-recheck`: fixture fixed, 11 passed. Do not count fixture failure as a product bug or omit it from history.
- `app-operations`: 29 pass. `ui-and-replay`: 19 pass.
- `full-local`: full local attempt; exact final counts and environment limitations in release evidence. CI is authoritative for PostgreSQL/Chromium.
- `full-local`: 8 failed, 4382 passed, 51 skipped, 11 errors (PostgreSQL unset / Chromium unavailable / local socket restrictions); `ci-c6c8bba-summary`: 8 failed, 4444 passed, 0 skipped in isolated CI. Eight UI fixture failures were fixed to supply the test access token and configured response model, without weakening the production gate. `ui-auth-final`: 135 passed, including denied access before provider invocation.
- `policy-final`: frozen 24, rules and DEMO each unsafe pass 4/16; unavailable live boundary 19 hold/5 block/0 approve.
- `participant-inputs`, `human-workflow-measurement`: prepared only, no human result rows; hashes frozen separately.

Source commits in candidate reports can refer to their input-freeze parent if generated from an in-progress working tree. Prefer `final96-after` and `development-candidate-frozen` for exact frozen candidate attribution. Historical evaluation files outside this new folder remain unchanged.
