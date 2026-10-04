# DeepSeek Flash: Qwen-order ten-chain run

Started 2026-09-24 11:10 Asia/Shanghai in
`runs/deepseek-flash-qwen-order-10chain-20260924`.

The chain order is the frozen Qwen scheduler order restricted to the first ten
environment-ready chains: `break-filter-js-from-html`, `chess-best-move`,
`bn-fit-modify`, `build-cython-ext`, `build-pmars`, `caffe-cifar-10`,
`cancel-async-tasks`, `circuit-fibsqrt`, `cobol-modernization`, and
`compile-compcert`. These contain 48 tasks (144 arm-task comparisons).
`adaptive-rejection-sampler`, `build-pov-ray`, and `code-from-image` were
deferred because their LFS assets were not fully materialized.

Each chain runs baseline, skill-RSI, and harness-RSI on the same tasks; each arm
preserves state within its chain and resets at the next chain. The complete
five-task `build-pmars` DeepSeek pilot is linked read-only into the batch and
is not repeated. No Qwen solve result is reused. The frozen clean worker,
budgets, agent, and grader are the same as in the pilot.

The coordinator listens on localhost port 8056. Its upstream API key exists
only in the coordinator process memory; workers receive a disposable local
gateway token. The gateway records request status and token counts but not
prompts, responses, or upstream credentials. `status.json` and `batch.log`
show progress. `PAUSE` stops *new* dispatch and lets active work finish. The
coordinator also stops new dispatch once its conservative peak-rate token
estimate reaches ¥40; this is not an exact account-billing limit. Environment
or worker failures are marked blocked and require review, not blind retries.

At launch, all 48 tasks passed readiness, LFS, and Docker-image checks. The
Docker disk gate was healthy. The Qwen experiment continued independently.
