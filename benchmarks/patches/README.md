# Local benchmark changes

The benchmark checkouts under `benchmarks/vendor/` are upstream projects and are not mirrored here. [../lock.json](../lock.json) pins the three top-level checkouts. The current Terminal-Bench-Evo worktree also uses these nested revisions:

| Checkout | Upstream revision | Local changes |
| --- | --- | --- |
| `benchmarks/vendor/evoarena` | `17983e68eeaa26e78575566836aa679ea6b6bf80` | `evoarena-local.patch` |
| `benchmarks/vendor/evoarena/EvoMem-TerminalBench-Evo/harbor` | [`harbor-framework/harbor`](https://github.com/harbor-framework/harbor) `812fbcf8c8de0568d6b8fda8279b352d92293689` | `harbor-local.patch` |
| `benchmarks/vendor/evoarena/EvoMem-TerminalBench-Evo/Terminal-Bench-Evo` | [`wufeiwu/Terminal-Bench-Evo`](https://huggingface.co/datasets/wufeiwu/Terminal-Bench-Evo) `18ecf86f67ad9208eccaf195c63ceb42f4095dc0` | `terminal-bench-local.patch` |
| `benchmarks/vendor/continual-skill-bench` | `083f14de18227a1eed15758eb1a80632966525c0` | `continual-skill-bench-rootless.patch` |

After checking out those revisions, apply each patch inside its matching checkout. For example, from the repository root:

```bash
patch_root="$(pwd)/benchmarks/patches"
git -C benchmarks/vendor/evoarena apply "$patch_root/evoarena-local.patch"
git -C benchmarks/vendor/evoarena/EvoMem-TerminalBench-Evo/harbor apply "$patch_root/harbor-local.patch"
git -C benchmarks/vendor/evoarena/EvoMem-TerminalBench-Evo/Terminal-Bench-Evo apply "$patch_root/terminal-bench-local.patch"
git -C benchmarks/vendor/continual-skill-bench apply "$patch_root/continual-skill-bench-rootless.patch"
```

The Terminal-Bench-Evo patch contains text edits only. Materialized LFS assets, task images, local model endpoint `.env` files, and runtime state are not included. The patches record project code changes; applying them does not by itself recreate the active experiment.
