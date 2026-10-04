# Terminal-Bench-Evo offline grading

This is an **environment-only** change for the 19 registered development tasks:
`break-filter-js-from-html` (5), `chess-best-move` (5), `bn-fit-modify` (4),
and `build-cython-ext` (5). Other tasks fail closed until explicitly audited.
It does not change the Self-Skill v2 update rule, retrieval, budgets, or model.

## Preparation versus execution

- Online preparation downloads missing wheels and the upstream pyknotid 0.5.3
  test fixture once. The existing uv 0.9.5 and Python 3.13.9 are reused.
- The bundle locks all resolved wheel versions and hashes, original test hashes,
  image IDs, and grader implementation hashes. Never rebuild a sealed directory.
- Grading installs from this local bundle only. There is no apt, remote pip/uv
  index, interpreter download, remote git fetch, or remote ZIP fetch.
- HTML/statistics/base-chess use isolated Python 3.13.9, matching the previous
  cached grading runtime. Cython keeps the task's Python/ABI and installed agent
  packages. Evolved chess uses its preinstalled native pytest.
- The original pytest assertions, test selection, and binary reward stay intact.
  `test.sh` dependency bootstrap is replaced, not executed. Four Cython versions
  map the exact Git URL to a local bare repository; EVO-4 changes only its one
  ZIP resource URL to a local file. The original host tests are never edited.
- Fixture tag 0.5.3 is pinned to commit
  `441c807dbec2ee32e1da572e24e58d52a4eb7afa`; ZIP and Git test contents are checked
  for equality. Git/ZIP availability is checked before judging the agent output.

## Network and solver separation

`offline_verifier.harbor:OfflineVerifier` uses Harbor's custom-verifier interface.
No frozen Harbor or previous campaign source files were modified.

Only **after solving**, it streams the normalized runtime and official tests into
the existing task container. Normal `docker cp` uploads cannot be used with this
machine's single-UID rootless Docker. Grading uses the same task filesystem; no
Docker commit, new image layer, or workspace reconstruction is involved.

The host adapter enters the exact container's user/mount/PID namespaces, creates
a new network namespace containing only `lo`, enables loopback, drops all
capabilities and enables `no_new_privs`, then runs grading. It checks both external
connection failure and local loopback success. Solver networking/seccomp is not
relaxed or disabled. This adapter is local-Linux/rootless-Docker infrastructure,
not a provider-independent implementation or a hostile-agent security boundary.

Every grading attempt retains:

- `network.json`: isolation, loopback and privilege-drop evidence;
- `runtime-provenance.json`: image, runtime bundle and official test hashes;
- `offline-result.json` plus official `ctrf.json` and `reward.txt`;
- `offline-error.json` on setup/collection/runtime failure (not algorithmic zero).

Only pytest exit 0/1 with a nonempty, internally consistent report containing
only passed/failed tests is accepted. Missing dependencies, missing fixtures,
collection errors, browser setup failures, timeouts and skipped tests fail closed.
Failures caused by missing/wrong task outputs still count as task failures.

## Commands

Run from `/home/ytzhang/rsi-harness`, after `source scripts/docker-env.sh`.

```bash
# Create a NEW artifact directory. Online only during this command.
.venv-terminal/bin/python -m offline_verifier.build \
  --source-manifest SOURCE_RUN/manifest.json --output NEW_RUNTIME

# Add --reuse OLD_RUNTIME to reuse local prepared resources without downloads.

# Real --network none containers, all registered task versions, no LLM calls.
.venv-terminal/bin/python -m offline_verifier.acceptance \
  --manifest NEW_RUNTIME/manifest.json --output NEW_ACCEPTANCE --workers 3

# Also exercise a saved correct HTML answer and incorrect HTML/browser+HTTP cases.
RSI_OFFLINE_TEST_MANIFEST=NEW_RUNTIME/manifest.json \
  .venv-terminal/bin/python -m pytest -q tests/test_offline_verifier_docker.py
```

Attach to Harbor with:

```text
--verifier offline_verifier.harbor:OfflineVerifier
--verifier-kwarg manifest=/absolute/path/to/manifest.json
--verifier-kwarg manifest_sha256=<sha256-of-that-manifest>
```

## Preserving experiment history

`self_skill_rsi_offline.campaign` prepares a separate run directory. It verifies
and retains completed official scores, successful candidate checks, the skill
bank, reviewed pending proposals, and original artifact references. It pauses
the online source controller and records an environment-version transition.
The algorithm remains `self-skill-nonregression-v2`.

```bash
.venv-terminal/bin/python -m self_skill_rsi_offline.campaign \
  --root NEW_RUN --prepare-from OLD_RUN \
  --manifest NEW_RUNTIME/manifest.json \
  --acceptance NEW_ACCEPTANCE/summary.json

# Explicit: may redo ONLY the pending unscored infrastructure-failed validation,
# then continue the remaining stream. Already valid scores are not replayed.
.venv-terminal/bin/python -m self_skill_rsi_offline.campaign \
  --root NEW_RUN --run --allow-native-runs
```

An unsolved-image check establishes that tests execute offline; it is not an
algorithmic score or proof of full semantic equivalence. Positive/negative
browser controls and the native Harbor entrypoint are checked separately.
Results before/after migration retain different grading-environment provenance;
do not relabel historical online scores as offline scores. This change does not
make the model API or the agent's task actions offline.
