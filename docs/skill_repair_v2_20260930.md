# V2 first step: seen-error skill repair

This is a development sanity check requested by the user, not a new held-out
benchmark or a claim that meta-skill generalization is already implemented.

## Fixed cohort and comparison

Use the 19 completed meta-skill source attempts in the existing four chains:
break-filter-js-from-html, chess-best-move, bn-fit-modify, build-cython-ext.
Validate infrastructure/configuration before selecting the 15 scored failures.
The 4 successes are never replayed. Historical controls and all old scores stay
read-only. Compare each failed attempt to at most one new attempt with its skill
patch. This is NOT a clean causal ablation: another sample alone may solve a task,
and historical attempts may already have pre-existing skills. Report that caveat.

## Learning, freezing, and replay

For each failed task independently, retain the exact skill memory used BEFORE its
old solve. Read only the public solver trace (the full original 24,000-character
window), instruction, scalar reward and that old memory. No hidden verifier logs,
private reasoning, answer keys or other tasks' results enter learning.

Diagnose up to 5 observations PER TASK, propose up to 3 bounded skill updates,
then audit them against exact original spans. One evidence-only revision of
rejected proposals is allowed, followed by one review (maximum 5 calls, 1,800s).
Keep all unrelated old skills. Never concatenate and thin 19 traces into one small
global context. Missing evidence is unknown, not evidence of a causal failure.

Freeze every successful per-task bank before any replay starts. No cross-chain
consolidation or meta-updater evolution is attempted in this first diagnostic.
If no changed skill passes review/retrieval, mark the task as no supported change;
do not waste a solve or silently drop it from the overall error denominator.

Reuse Qwen/Qwen3.8-27B, CleanTerminalAgent, original native task image and verifier,
top-4/12,000-character skill retrieval, 15 model calls, 120 commands, 32,768 tokens
per call, 7,200s agent budget and **600s verifier timeout**. Each replay starts in
a fresh container/context and uses its pre-frozen per-task bank, independently of
other replays. It is NOT an online chain score. No solve-result-driven skill edits.

## Reporting and safety

Report actual grader pass/fail, confirmed repairs out of ALL 15 original errors,
and repair rate among valid completed replays separately. Also report no-skill
cases, retrieval exclusions and infrastructure failures; never turn them into
algorithmic zeros. A separate one-call qualitative audit compares old observations
with new public evidence: avoided/repaired, recurred, or unknown. A positive label
requires an actual cited action/check, not absence of an error string. This audit
does not override the official score or train the bank.

Implementation lives outside the already sealed packages. Hash-pin old inputs,
task checksums, environments, code and new banks. Persist request/response/usage
and one-attempt launch markers. Interrupted model calls and solver attempts are
not automatically repeated. Controller restarts adopt live workers and reserve
orphan Harbor children. Existing tasks finish naturally during the temporary
queue handoff; freed ports are reused (maximum 4 concurrent workers). At the end,
restore the old queue and its watchdog. Diagnostic polling is every 20s with
30-minute snapshots; this is not a promise of an actual Codex wakeup.

```bash
source scripts/docker-env.sh
.venv-terminal/bin/python -m skill_repair_v2.campaign --prepare
.venv-terminal/bin/python -m skill_repair_v2.watchdog --root /data2/ytzhang/rsi-harness-runs/skill-repair-v2-20260930
.venv-terminal/bin/python -m skill_repair_v2.campaign --report
```
