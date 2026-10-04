# RSI Harness

Research code for budgeted, auditable agent self-improvement experiments. This repository contains the current project source tree, scripts, tests, and configuration. Large runtime artifacts and upstream benchmark checkouts are kept outside Git.

## Current experiment

The latest registered run is `three-arm-extension-20261004-v1`. It compares `baseline`, `skill-rsi`, and `self-skill` with `Qwen/Qwen3.8-27B` on Terminal-Bench-Evo. The 441-task inventory continues a prior run: 112 self-skill tasks were preserved, followed by 329 additional tasks. Scores from completed tasks are reused rather than replayed.

- [config.json](config.json) records the settings selected from the sealed run manifest and recovery protocol. Its hashes identify those local records.
- [self_skill_rsi_recover/](self_skill_rsi_recover/) contains the active campaign, scheduler, and worker entrypoints.
- [terminal_offline_recover/](terminal_offline_recover/) contains the current offline verifier and environment recovery code.
- [benchmarks/lock.json](benchmarks/lock.json) pins the top-level upstream benchmark repositories. Their checkouts are not copied into this repository. [Local patch notes](benchmarks/patches/README.md) record the nested revisions and project changes used here.

The three arms differ in memory scope and update compute. This historical continuation is not, by itself, an isolated causal comparison. The run had not completed when the configuration snapshot was captured; no final result is claimed here.

## Project layout

| Path | Contents |
| --- | --- |
| `rsi_harness/` | Shared harness, agents, schedulers, and benchmark interfaces |
| `self_skill_rsi*/`, `cross_chain_rsi/`, `skill_repair_v2/` | Self-improvement controller and its imported implementation layers |
| `offline_verifier/`, `terminal_offline*/` | Offline grading, admission, and environment recovery |
| `scripts/` | Setup, launch, monitoring, and maintenance commands |
| `benchmarks/` | Pinned upstream revisions, local patches, and small smoke fixtures |
| `tests/`, `examples/`, `docs/` | Tests, integration examples, and design notes |

Older implementation modules remain in the source tree because the current campaign imports them. Dated notes in `docs/` and the small configuration under `examples/` describe earlier development stages; `config.json` is the current run configuration.

## Using the source

The general Python package requires Python 3.10 or newer; the active experiment uses a Python 3.12 environment and additional benchmark dependencies. See [pyproject.toml](pyproject.toml) for the package entrypoint and [benchmarks/lock.json](benchmarks/lock.json) for upstream revisions. The current campaign also expects prebuilt Docker images, local model services, and runtime artifacts, so a fresh clone is not a one-command reproduction of its scores.

The local test suite can be run after preparing those dependencies:

```bash
python -m pytest tests -q
```

Runtime directories (`runs/`, virtual environments, caches, model artifacts, Docker data, and upstream vendor checkouts) are intentionally excluded from Git. The active source directory continues to evolve while the experiment runs; this repository is a source snapshot, not a live mirror.
