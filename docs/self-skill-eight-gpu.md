# Self-skill RSI: eight-GPU inference deployment

This is an inference-only, versioned handoff for the existing sequential,
cross-chain self-skill experiment. It does not add tasks, change the skill
algorithm, raise generation budgets, replay official scores, or change grading.
The current manifest contains 19 development tasks from four chains; this
deployment does not silently expand it to a full benchmark or other datasets.

## Deployment

| Endpoint | GPUs | Use |
| --- | --- | --- |
| `127.0.0.1:8040` | 0, 1, 2, 3 | Official solve, skill update, current-task validation |
| `127.0.0.1:8041` | 4, 5, 6, 7 | Historical-task validation |

Both services use the same local `Qwen/Qwen3.8-27B` checkpoint, BF16,
262,144-token context capacity, one active request per service, and no prefix
cache. Tensor parallelism is four; CUDA Graph captures decoding with batch
size one. Torch compilation stays disabled. GPUs 8 and 9 are outside scope.

The single chronological learning stream cannot use both services for different
official tasks at once without changing which skill library a task receives.
Consequently, four GPUs do useful model work during an official solve or update;
all eight can do useful work when the two candidate-validation tasks overlap.

## Measurements and limitations

The initial shadow service used GPUs 4–7 and port 8050 while the original
experiment continued. Six fixed-512-token probes covered input lengths 394,
4,174 and 16,774 tokens, each repeated twice:

- Previous TP2/eager median decode: **16.247 tokens/s**.
- Shadow TP4/decode-graph median decode: **62.212 tokens/s** (**3.83×**).
- Text, JSON, automatic tool calls, tool-result round trips, and reasoning with
  tool calls passed synthetic API compatibility checks.

This measures the combined deployment change, **not the isolated effect of
adding GPUs**. It is not a 3.83× claim about complete tasks: commands, containers,
scoring and prefill also take time. TP4 changes floating-point reduction order;
the weights, dtype, prompts and budgets remain the same, but generated output
is not guaranteed to be bitwise identical. Scores retain deployment labels.

Before accepting the final deployment, the handoff runs API checks and latency
probes on both final services concurrently. Their evidence, launch identities
and accepted configuration are recorded in:

`/data2/ytzhang/rsi-harness-runs/inference-acceleration-20260930/`

## Safe handoff

1. A `PAUSE` marker requests a stop after the current complete task/update.
2. Confirm the controller, read-only monitor and native children have exited.
3. Verify all completed records, the contiguous task prefix and bank lineage.
4. Stop only the exact registered, idle inference API processes; never force
   kill a native trial or another user's GPU process.
5. Start two TP4 services, then check both concurrently.
6. Copy the complete prefix and skill-bank versions to a new root. Retain
   original evidence references and hashes. Only the active bank HEAD may move
   as future tasks are learned.
7. Seal the new deployment; launch the next uncompleted task with the original
   algorithm and the existing offline verifier. Update the active-run pointer.

The one-shot implementation is `scripts/handoff-eight-gpu-self-skill.py`.
Its current phase is `handoff-status.json` in the evidence directory. A failure
stops the handoff and preserves artifacts; it never automatically repeats a
scored task or silently changes the experimental protocol.

The active run is located through `runs/ACTIVE_SELF_SKILL_RSI.json`. The new
campaign package is `self_skill_rsi_accel`; its read-only watchdog reuses the
existing monitor and adds separate old/new inference score cohorts. Monitoring
is not a claim that a Codex model is automatically awakened.

Read the active state without changing it:

```sh
.venv-terminal/bin/python scripts/self-skill-status.py
```

The accelerated launcher explicitly restores the same Docker environment as
the original controller and checks the registered overlay2 data root before
starting. It does not rely on an operator remembering to source Docker settings.
The watchdog also checks the exact inference process identities and endpoint
health, alongside controller heartbeat, immutable evidence, and disk admission.

## Activated deployment

Activated on 2026-10-01 at 00:08:47 Asia/Shanghai. Both final TP4 services
passed all five API checks. Concurrent fixed-token probes measured median
decode speeds of 62.279 tokens/s (8040) and 62.240 tokens/s (8041), versus
16.247 tokens/s on the previous TP2/eager deployment: **3.83×** using the slower
of the two final services.

The new root is:

`/data2/ytzhang/rsi-harness-runs/self-skill-rsi-20260930-v2-offline-tp4`

Six existing official scores were retained unchanged (4 passes, 2 failures).
The revision-5 bank with two skills was retained. The first accelerated task is
`s000006`, `chess-best-move-EVO-1`; actual model replies and terminal tool
execution were observed after restart. These observations establish deployment
continuity, not a claim that the new skill improves benchmark accuracy.

A startup preflight initially mistook closed TCP `TIME_WAIT` connections for
active listeners. It was corrected using `SO_REUSEADDR`, without enabling
`SO_REUSEPORT`; a regression test verifies that active listeners are still
rejected. Only service deployment was resumed after that failure. No official
task was rerun, and the failed handoff log and explicit recovery audit were kept.

Final project verification: 298 tests passed, 5 skipped, and 20 subtests passed.
