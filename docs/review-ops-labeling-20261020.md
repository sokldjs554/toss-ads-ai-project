# Operations and labeling review — 2026-10-20

## Scope and evidence

This is a new, bounded review of the current code on `codex/complete-ads-20261020`, based on `d3ef1848f5e4df4f426f97f120bedfd9b96b9ae6`. The interrupted session supplied task titles, including a labeling remapping mutation and load-test/report failure cases; it did not supply detailed reviewer reports. The findings below were independently reproduced in this session and are not reconstructed claims about an earlier review.

Reviewed contracts in `docs/FEATURES.md`, `docs/OPERATIONS.md`, and `docs/LABELING.md`, then traced the relevant operations, queue, report, and UI code and existing tests. No provider calls, external network load, production data, or paid API calls were used. The changes do not add persistence, authentication, independent labelers, or production operating experience.

## Verified findings and fixes

### 1. Failed local load tests could look successful or lose their report

**Reproduction.** A workload that failed during the timed phase produced a positive throughput and exit status zero even when every timed task failed. Failures in warmup or memory measurement escaped before the JSON report was written. With an even number of repeats, the reported median was the upper middle result rather than the average of the two middle results.

**Cause.** Only `_timed` caught task exceptions; warmup and memory called the task directly. Throughput used attempted calls, the CLI returned zero unconditionally, and the median selected an array element.

**Fix.** Every task phase records failures. The JSON report retains warmup, timed, and memory error counts and examples. Successful throughput uses successful calls; attempted throughput is reported separately, alongside both call counts. The CLI still writes the report and returns status one when any phase has a failed task. The median now uses the standard arithmetic definition.

**Regression evidence.** Tests inject real task failures separately into warmup, timed, and memory phases and check the saved report and exit status. A deterministic wall-time fixture checks successful and attempted throughput plus the even-repeat median. Existing successful workload and no-network tests remain passing.

Files: `scripts/loadtest.py`, `tests/test_ops_loadtest.py`.

### 2. Malformed measurement reports could crash or become invalid effect estimates

**Reproduction.** Nested lists/scalars in comparison report fields raised exceptions. Invalid counts, inconsistent denominators, negative or non-finite costs/times, failed system summaries, unknown baseline labels, and case-level provider failures could be accepted as measurements. An absent baseline cost became a measured cost of zero. A baseline declaring `dataset: holdout` was accepted as development data and rejected when holdout was requested.

**Cause.** The loader trusted nested report shapes, coerced counts, defaulted absent values to zero, checked failures mainly at the top level, and hardcoded the baseline as development data.

**Fix.** Validate nested report structure, integer counts, rate denominators and totals, finite nonnegative numeric values, allowed decisions, and failure records before creating measurements. Parsing errors become an unavailable reason and permit a valid baseline fallback. Missing baseline cost/time stays unmeasured. Explicit split metadata or the recognized holdout dataset name/path controls baseline selection; metadata-free historical baselines retain the existing development default. The fallback note describes the actual split.

**Regression evidence.** Parameterized malformed-report tests check unavailability without exceptions or fabricated estimates, plus fallback to a valid baseline. Other tests cover unknown/failed baseline cases, absent cost/time, and holdout selection using a dataset name, dataset path, or explicit split. Existing arithmetic tests and independent recounts of the stored real comparison report remain passing; the stored measurements were not regenerated.

Files: `ops/impact.py`, `tests/test_ops_impact.py`.

### 3. The blinded labeling view retained source fact identifiers

**Reproduction.** An ordinary item and a gold item containing a fact such as `G-901-source-fact` exposed that identifier through `AnnotatorView.facts[].id` and `ad_copy.claims[].fact_id`, although other source identifiers were hidden. Existing tests used generic `F1` identifiers and missed this case.

**Cause.** The view copied facts and claims without replacing their identifiers.

**Fix.** Assign neutral view-only fact IDs and remap claim references consistently. A reference to a missing fact remains ungrounded (`None`). Stored queue records and their original fact IDs are unchanged.

**Regression evidence.** Both ordinary and gold items exercise source-bearing IDs, an existing `F1` collision, linked claims, missing references, and mutation of the returned view. The test verifies the original record is unchanged. An in-memory mutation that removed only claim-reference remapping was rejected for both item types; no mutated production file was saved.

Files: `ops/review_queue.py`, `tests/test_labeling_queue.py`.

### 4. Plain Korean wording in the operations screen

Changed the visible `오경보율` phrase to `이상이 없는 상황에서 잘못 알린 비율`. The displayed counts, rates, and caveats are unchanged.

File: `ui/ops_view.py`.

## Verification

Environment: Python 3.11.17 in `.venv311`.

```bash
.venv311/bin/python -m pytest tests/test_ops_*.py tests/test_labeling_*.py --tb=short
```

Result: **492 passed in 33.08s**. This includes the operations and labeling Streamlit AppTest flows, local load testing, stored-report recounts, stable export splits, leakage checks, queue transitions, and label consistency checks. The new functional cases were observed failing before their fixes; the queue fixture was corrected to provide a normal item before exercising gold injection.

The no-remap mutation check rejected the mutation for both ordinary and gold views. `git diff --check` also passed. Full project tests, end-to-end checks, deployment, and consolidated documentation belong to the integrating review and are not claimed by this scoped report.

## Limits preserved

- Load-test numbers measure local deterministic code and the mock pipeline, without real model/network latency. They do not establish service capacity or the absence of memory leaks.
- Effect estimates remain conditional on stored evaluation measurements and user assumptions; they are not measured production benefits. This review does not establish the authenticity of an externally supplied report or turn it into independent evidence.
- Blinding removes structural identifiers; a person who authored the examples or guidelines can still recognize their contents. There are still no independent labelers.
- The review queue remains session-local and in memory. It has no production authorization, notification, or durable storage layer.
- Existing synthetic evaluations, known detector limitations, and holdout results were not relabeled or tuned by this review.
