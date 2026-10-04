# Clean skill-RSI / harness-RSI campaign (v1)

The three arms are baseline, skill-rsi, harness-rsi. All use the frozen local
Qwen/Qwen3.8-27B and native Terminus2 / Harbor task containers and official grader.
The initial harness is behaviorally an identity wrapper around Terminus2.

## Intervention

Both RSI arms have one mandatory improvement opportunity after each scored task,
except the last stage of a chain. New chains start with independent empty state.
The improver sees only the completed task, its solver trajectory, scalar score,
and its own current artifact. It receives no future instruction or hidden tests.

Skill-RSI writes up to 16 Markdown skills with name, When, Steps and Cautions.
The fixed loader ranks lexical overlap and injects at most four whole skills,
within 12000 characters. Each version records its skills and checksum. These
injection records establish exposure, not proof that the model followed a skill.

Harness-RSI replaces a Python program with hooks before each main solver round,
after each parsed model decision, and after each tool batch. The generated program
runs in a network-disabled, read-only Docker sandbox. Its validated output can
change context selection, the next model prompt, command batches, completion
decisions, and a tool-then-model retry branch. Retries use the same solve budget.
Tool implementations, outer resource limits, grader and scheduler are trusted.
Internal parser-recovery/summarization calls remain fixed upstream behavior, do
not receive separate controller hooks, and still count toward the 15-call cap.
The host records hook inputs, outputs, hashes, executed commands and actual
behavioral changes. This is different from the old one-time delegate prompt.

Candidates passing protocol checks are committed without score-based task replay.
Checks use synthetic inputs; they do not establish performance improvement.
The protocol gate also exercises the completed instruction with synthetic success
outputs: completion vetoes must end within two checks and retries cannot outlive
the model-call budget. No future task instruction or grader is used for this gate.
Subsequent tasks measure improvement or regression. No-change and rejected
proposals are recorded, not resampled. Runtime policy failures end solving and
are reported separately while the official verifier grades the resulting workspace.

## Budgets and comparison

Solve: max 15 outer rounds, 15 logical model calls (including recovery and
summarization), 120 terminal commands, 32768 output tokens per request, 7200 s
agent time, 600 s grader time. Existing provider transport retry semantics are
unchanged; the call counter counts logical calls, not each HTTP transport attempt.
Improvement: one call, max 4096 output tokens, 600 s, temperature 0.2,
thinking disabled, 24000 trajectory characters, 32768 bytes of persistent content.
The two RSI arms share these limits. Baseline has no improvement calls; report
additional improvement cost instead of claiming equal-total-compute comparisons.

Legacy baseline is referenced only if its task checksum, model, output cap,
turn cap, recovery setting, timeouts and observed call/command counts are compatible.
Require complete grader evidence and no detected infrastructure error. Existing
incompatible scored results are excluded, never rerun or mixed into paired metrics.
Previously unscored baseline tasks can be run normally. Reuse is an audited
historical control; a fresh replicated study would be a separate experiment.

All tasks require a current preflight ticket and matching local image. Unresolved
Git LFS asset pointers are blocked even when the older preflight marked ready.
Infrastructure failures stay quarantined and do not become task failures or
unlimited retries. Valid zero scores are completed and never resampled.
Exhausting the agent's time/output allowance is an agent outcome, not an
infrastructure failure: when the official verifier completed cleanly, keep its
score and the limit-exhaustion evidence. Verifier/environment/API failures are
not treated this way. No limit exhaustion triggers another solve attempt.

## Launch and automatic gate

`scripts/accept-clean-rsi.py` uses two synthetic tasks with reduced solve caps to
verify real-model proposal, commit, and use on a subsequent task. These results
are outside formal metrics. Unit/integration tests also verify that generated
control actually vetoes completion and runs a tool before another model call.
An alternative development fixture uses a historical failed solver trajectory to
exercise real-model controller generation; `scripts/accept-clean-controller.py`
then runs that revised controller on a synthetic file task with the real solver.
Development fixtures, including rejected/ineffective revisions, remain archived.
Formal chains always start empty, never from these development artifacts.
Acceptance proves the mechanism works, not that generated code is semantically
optimal. For example, a generated verifier may misread echoed command text; such
policy mistakes are experimental outcomes, not grounds for replaying the task.

`scripts/launch-clean-rsi.sh --root <new-root>` starts the formal pilot on complete
break-filter-js-from-html and chess-best-move chains. Their ten compatible baseline
results are reused. All healthy existing endpoints are shared by runnable arms.
Each chain/arm remains sequential; parallel work has isolated state.

After both complete chains, expansion to all 89 chains (441 stages, three arms)
requires a revised skill library exposed on a later task, an updated executable
harness affecting tool/completion/output processing on a later task, and no
controller runtime errors. The gate stops for review on failure. It never treats
an unused modification interface as successful RSI.

The campaign manifest freezes source hashes and budgets. Relaunch with the same
root resumes recorded stages; changed campaign code requires a new root. State is
stored under chain/arm, each task has a launch record and final record, and each
improvement has request, response, validation, before/after version and usage.
An interrupted request or trial is not silently repeated.

Run `scripts/watch-clean-rsi.py --root <new-root>` with the project Python and Docker
environment for a persistent scheduler guard. It checks status every 20 seconds,
saves snapshots every 30 minutes, and restarts only an exited scheduler (max five
starts), never solving workers or models. A `PAUSE` file stops new dispatch without
killing running tasks. This is process monitoring, not a periodic AI/Codex review.

## Scheduler-only extension: paired-window-v1

The user approved step one only: pair-first scheduling with bounded lookahead.
`clean_scheduler.py` replaces the supervisor, but every solve still uses the
unchanged `clean_campaign stage` worker. No per-request GPU routing, tool/grader
overlap or extra model concurrency is introduced. A service stays reserved for
the entire solve, official grading and post-task improvement.

The shared order is the existing two pilot chains followed by the remaining
alphabetically ordered chains; all stage names are frozen in
`scheduler_manifest.json`. Within a chain, prioritize the earliest missing stage
among runnable arms. Already running arms are never stopped to synchronize them.

Full dispatch uses a three-chain window anchored at the earliest unfinished,
eligible chain. Prioritize the first two; use the third only when earlier work
cannot use an available service. Completed chains beyond the unfinished frontier
still occupy window positions: if A lags while B/C finish, D must wait. This is
a bound on lead, not just on simultaneously active chains. Some idle capacity
is still possible at that bound and during tools/grading; that tradeoff is explicit.
The pilot remains limited to its original two chains until its existing gate passes.

Non-ready chains are deferred consistently across arms. A recorded infrastructure
block quarantines that chain from new dispatch while preserving live workers and
all results; it does not count as a successful or failed solved task. Quarantined
chains do not stall dispatch of all remaining healthy chains and remain visible
in reports. None of this grants automatic solve retries.

Historical baseline adoption runs as locked, metadata-only supervisor work, even
when all four model services are occupied. It uses the existing compatibility and
readiness checks, respects predecessors, and never overwrites launched trials.

`status.json` / `report-clean-rsi.py` include the explicit matched task list,
same-denominator pass rates and jointly completed chains. Unpaired arm totals are
progress information, not the principal comparison.

The original `manifest.json` and its four source files remain unchanged. The
separate scheduler manifest pins policy, code, order and the original protocol
digest; `scheduler_transition.json` audits the preserved live workers and prior
status. Switching supervisors must preserve PID/start-time ownership and all
completed records. Algorithm or budget changes still require a new experiment.
