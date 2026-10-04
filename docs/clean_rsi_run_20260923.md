# 2026-09-23 clean RSI launch

This is a new Terminal-Bench-Evo experiment, not a relabeling of old fixed-loop,
self-action or open-ended results. The older campaign is paused and preserved.
SWE-Chain and ContinualSkillBench have not been launched under this new protocol.

## Evidence before launch

- 74 regression/integration tests and 20 subtests passed; Ruff checks passed.
- Real-model skill acceptance: first synthetic task generated revision 1; the
  second synthetic task loaded `write-and-verify-file`; both scored 1.
  Evidence: `runs/clean-rsi-acceptance-20260923/skill-rsi/acceptance.json`.
- A trivial successful task legitimately produced a no-change harness proposal.
  A separate historical failure fixture exercised real controller generation.
- The first generated controller could veto completion forever. A new liveness
  gate rejects that pattern; no formal trial was run with it.
- Revised real-model harness acceptance scored 1, loaded revision 1, executed
  13 hooks and changed two non-prompt control/output hooks. It used six model calls
  and made unnecessary investigations after misreading echoed shell text. This
  proves execution, not performance improvement; the proposal prompt now explicitly
  warns about terminal command echo. Generated code was not hand-corrected.
  Evidence: `runs/clean-rsi-controller-acceptance-v2-20260923/acceptance.json`.
- All acceptance runs and states are separate from formal metrics and initialization.

## Running configuration

Root: `runs/clean-rsi-20260923-qwen38-27b`.
Launch: 2026-09-23 21:23 Asia/Shanghai.
Qwen/Qwen3.8-27B, four TP2 services on ports 8040/8041/8042/8044, GPUs 0–7.
GPUs 8–9 are occupied by another user and are not taken over.

The pilot uses two complete chains: `break-filter-js-from-html` and
`chess-best-move`, five stages each. Ten compatible historical baseline stages
are reused (six passes), with twenty newly solved RSI stages. No development
controller/skill is carried into these chains.

The automatic full expansion gate, budgets and limits are specified in
`docs/clean_rsi_design.md`. A failure of the gate pauses expansion for review;
successful pilot stages are retained, never rerun to improve a score.

At launch, environment tickets reported 366 ready, 39 failed, 34 missing and
two running; 59 entire chains also had their assets materialized. This is not a
claim that all 441 environments are ready. Preflight continues independently;
the campaign only dispatches ready tasks and keeps environment errors separate.

## Read-only progress and monitoring

`./.venv-terminal/bin/python scripts/report-clean-rsi.py`

The guard writes `monitor.json`, the scheduler writes `status.json`, and
`monitor_snapshots/` gets a snapshot every thirty minutes. Polling is every twenty
seconds. This is background process monitoring, not an automatic Codex wake-up.
The guard may restart an exited scheduler without restarting its live workers;
interrupted model proposals/solve attempts are not blindly repeated.

Creating a `PAUSE` file in the new root stops dispatch and preserves live workers.
Removing it allows dispatch to resume while the guard is running. Completed or
needs-review queues require explicit investigation/relaunch, not retries in a loop.

Do not modify the four source files pinned in `manifest.json` during this run.
Future changes require a versioned campaign root to avoid mixing protocols.
