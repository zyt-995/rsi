# Paired-window scheduler: step-one-only deployment

Approved scope: shared chain order, pair-first dispatch, bounded lookahead,
metadata-only historical baseline registration, and same-sample reporting.

Not included: per-request model-service routing, sharing a service while another
task runs tools/grading, changing model parameters/budgets, or repeating trials.

The implementation is `rsi_harness/clean_scheduler.py`; the worker and all four
original manifest-pinned source files are unchanged. The existing run is kept at
`runs/clean-rsi-20260923-qwen38-27b` and adopts its original live processes.

Dispatch policy:

1. Preserve the original two-chain pilot gate.
2. Use one frozen order: pilot chains first, then other chains alphabetically.
3. After the gate, allow at most the frontier chain and the next two eligible
   chains. Completed-ahead chains still occupy these positions until the frontier
   advances, so a fast arm cannot keep opening arbitrarily later chains.
4. Among available work, prefer the earliest chain, then the earliest missing
   stage, excluding chain/arm pairs that already have an active worker.
5. Reuse validated baseline records without allocating a model-service slot.
6. Defer unready environments for all arms; quarantine recorded infrastructure
   blocks without stopping live workers or fabricating zero scores.

Tests cover ordering, an active slow arm not blocking a fast arm's next stage,
third-chain work filling available capacity, fourth-chain admission being denied,
completed-ahead chains not bypassing the lead bound, baseline locks/no-repeat,
fixed-protocol checks, process adoption, and a full mocked dispatch cycle.

Audit files in the run root:

- `manifest.json`: original algorithm and budgets, unchanged.
- `scheduler_manifest.json`: scheduler source hash, policy and common task order.
- `scheduler_transition.json`: prior status and live worker identities at switch.
- `baseline-adoption.jsonl`: audited history registrations without inference.
- `status.json`: active tasks, window, deferred/quarantined chains and paired data.

For progress: `.venv-terminal/bin/python scripts/report-clean-rsi.py`.

## Deployment verification — 2026-09-23 22:16 Asia/Shanghai

91 tests and 20 subtests passed. Scheduler 1740819 exited normally and guard
1740814 started scheduler 1991308. All four worker PIDs/start identities survived
the switch unchanged. The original manifest bytes, its four pinned source hashes,
and all ten pre-switch stage records matched their pre-switch checksums.

The new supervisor registered the three remaining historical chess baseline
stages while all four RSI workers kept their service slots. Baseline now has ten
reused pilot-stage records and zero new solve output tokens. Both pilot chains
remain the active window; the three-chain lookahead applies after the existing
pilot gate. The new heartbeat is fresh and all four model endpoints are healthy.
