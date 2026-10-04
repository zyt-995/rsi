# Dynamic RSI: Continual Self-Improvement in Evolving Environments

> Working plan / implementation draft  
> Date: 2026-09-21

---

## 0. Executive Summary

We study **training-free recursive/self-improving agents in dynamically evolving environments**.

The central question is not simply whether an agent can improve on a fixed task, but whether it can:

1. perform well on the current task;
2. rapidly adapt when the environment changes;
3. learn increasingly effective ways to adapt itself.

The core formulation is:

\[
a_t \in \mathcal{A}_{task} \cup \mathcal{A}_{self}
\]

where task actions and self-improvement actions may both appear inside the same long-horizon rollout.

We will initially evaluate **four training-free RSI paradigms** on **three continual / evolving benchmarks**.

### Benchmarks

1. **EvoArena** — environment evolution / adaptation / stale knowledge.
2. **ContinualSkillBench** — continual skill acquisition / transfer / capability evolution.
3. **SWE-Chain** — realistic software evolution across package versions.

### RSI paradigms

1. **Fixed-Loop RSI**
2. **Self-Improve-as-Action RSI**
3. **Open-Ended Self-Modification RSI**
4. **Search / Evolution-based RSI**

Plus a **Static Agent** baseline.

We explicitly do **not** include weight updates in the first phase.  
**Meta-RSI** is reserved for the second phase.

---

# 1. Research Motivation

A large fraction of current self-improvement work assumes:

\[
\text{Fixed Task} + \text{Verifiable Feedback}
\]

and repeatedly optimizes performance on that fixed task.

This makes it difficult to distinguish:

- genuine self-improvement;
- task-specific overfitting;
- accumulation of context;
- exploitation of a fixed verifier.

We instead consider an evolving setting:

\[
\mathcal{E}_1 \rightarrow \mathcal{E}_2 \rightarrow \cdots \rightarrow \mathcal{E}_T
\]

where task requirements, environment dynamics, tools, APIs, codebase state, or useful skills may change over time.

The desired agent should simultaneously optimize:

\[
\text{current task performance}
\]

and

\[
\text{future adaptation capability}
\]

under limited computation and interaction budget.

---

# 2. Core Research Questions

## RQ1 — Does persistent self-improvement help in evolving environments?

Compare a static agent against agents that persistently modify their external state:

- memory;
- reusable skills;
- harness / workflow;
- agent implementation.

We care about cumulative long-horizon performance rather than only final task success.

---

## RQ2 — How should self-improvement be organized?

Compare four qualitatively different paradigms:

1. human-designed fixed improvement loop;
2. self-improvement as part of the agent action space;
3. open-ended self-modification;
4. search / evolution over multiple self-modified agents.

The important distinction is not merely **what artifact is modified**, but:

- who decides when to modify;
- who decides how to modify;
- whether multiple candidate modifications are explored;
- how modification cost is allocated.

---

## RQ3 — Can the agent learn how to adapt?

We want to test whether adaptation itself improves over time.

A useful signal is:

\[
C^{(1)}_{adapt} > C^{(5)}_{adapt} > C^{(10)}_{adapt}
\]

where \(C_{adapt}\) is the cost or number of interactions required to recover after a new environment shift.

Evidence for stronger meta-adaptation should include:

- faster recovery after unseen shifts;
- fewer useless self-updates;
- better choice of memory vs skill vs harness update;
- better allocation of compute between acting and self-improving;
- transfer of self-improvement policy to new tasks / repositories / environment regimes.

---

# 3. Benchmarks

## 3.1 EvoArena

### Role

Primary benchmark for:

- dynamic environment adaptation;
- stale knowledge;
- retention vs forgetting;
- change-point response.

General structure:

\[
E_1 \rightarrow E_2 \rightarrow \cdots \rightarrow E_T
\]

The environment evolves over time, and previously useful information may become outdated.

### What we want to measure

- how quickly the agent detects environmental change;
- whether it removes or updates stale knowledge;
- whether it overreacts to noise;
- whether self-improvement reduces adaptation regret;
- whether self-improvement decisions become better over time.

### Most relevant RSI paradigms

- Fixed-Loop RSI
- Self-Improve-as-Action RSI
- Open-Ended Self-Modification RSI

### Reference

- EvoArena paper: https://arxiv.org/abs/2606.13681
- GitHub: https://github.com/Aiden0526/EvoArena

---

## 3.2 ContinualSkillBench

### Role

Primary benchmark for:

- skill accumulation;
- skill reuse;
- forward transfer;
- distinction between raw context accumulation and reusable capability.

The benchmark contains sequences of related tasks with skill dependencies and reuse opportunities.

### Core question

Does performance improve because the agent truly learns reusable skills, or simply because previous trajectories remain in context?

### Required baselines

In addition to the standard Static Agent, this benchmark should include:

- **History / Context-only** baseline;
- explicit persistent skill baseline.

### Most relevant RSI paradigms

- Fixed-Loop RSI
- Open-Ended Self-Modification RSI
- Search / Evolution-based RSI

### Reference

- ContinualSkillBench paper: https://arxiv.org/abs/2608.03874

---

## 3.3 SWE-Chain

### Role

Primary realistic coding environment.

General structure:

\[
Codebase_{v_1}
\rightarrow
Codebase_{v_2}
\rightarrow
Codebase_{v_3}
\rightarrow \cdots
\]

The same evolving codebase is modified across real package version transitions.

### Why it is important

This naturally creates:

- API changes;
- dependency changes;
- deprecations;
- compatibility changes;
- regression risks;
- persistent repository-specific knowledge.

It is therefore a natural environment for studying whether coding agents can continually adapt rather than repeatedly solving isolated issues.

### Persistent agent workspace

Recommended structure:

```text
.agent/
├── MEMORY.md
├── AGENTS.md
├── skills/
│   ├── ...
├── scripts/
│   ├── ...
└── state.json
```

Possible meanings:

- `MEMORY.md`: repository-specific facts and observations;
- `skills/`: reusable procedures;
- `AGENTS.md`: workflow / policy / tool-use instructions;
- `scripts/`: utilities created by the agent;
- `state.json`: machine-readable metadata / budgets / update history.

### Most relevant RSI paradigms

- Self-Improve-as-Action RSI
- Open-Ended Self-Modification RSI
- Search / Evolution-based RSI

### Reference

- SWE-Chain paper: https://arxiv.org/abs/2605.14415
- GitHub: https://github.com/CUHK-ARISE/SWE-Chain

---

# 4. RSI Paradigm 1 — Fixed-Loop RSI

## Definition

Self-improvement happens through a **human-designed fixed update procedure**.

\[
Agent_t
\rightarrow
Trajectory_t
\rightarrow
Reward/Feedback_t
\rightarrow
U_{human}
\rightarrow
Agent_{t+1}
\]

The improvement operator \(U_{human}\) is fixed.

Example:

```text
Run task
→ obtain verifier feedback
→ analyze success/failure
→ update memory
→ update reusable skills
→ optionally validate
→ proceed to next task
```

The model may generate the content of the update, but:

- update timing is fixed;
- update stages are fixed;
- the improvement pipeline is human-designed.

## Why include it

This is the strongest simple baseline for:

\[
\text{Human-designed RSI}
\]

It tells us how much performance can be obtained when the improvement procedure is engineered by hand.

## Possible update surfaces

- Memory
- Skills
- Harness
- Memory + Skills + Harness

The update surface is an implementation choice and should not be confused with the RSI paradigm itself.

## Recommended initial implementation

After every task:

1. collect trajectory;
2. collect verifier reward / feedback;
3. call an `improver`;
4. allow `ADD / EDIT / DELETE / MERGE`;
5. optionally run a lightweight validation;
6. persist the new state.

## Related papers

### Reflexion

Trajectory → feedback → verbal reflection → persistent episodic memory.

- Paper: https://arxiv.org/abs/2303.11366

### ExpeL

Extract reusable natural-language experience from previous tasks and reuse it later.

- Paper: https://arxiv.org/abs/2308.10144

### SkillOpt

Skill edits generated from experience and accepted after validation.

- Paper: https://arxiv.org/abs/2605.23904

### EvoSkill

Failure analysis → skill proposal / modification → validation.

- Paper: https://arxiv.org/abs/2603.02766

---

# 5. RSI Paradigm 2 — Self-Improve-as-Action RSI

## Definition

Self-improvement is treated as an ordinary part of the agent rollout.

Instead of:

\[
Task
\rightarrow
Feedback
\rightarrow
SelfImprove
\]

we allow:

\[
Agent
\rightarrow
action
\rightarrow
SelfImprove
\rightarrow
action
\rightarrow
memory
\rightarrow
SelfImprove
\rightarrow \cdots
\rightarrow Reward
\]

Formally:

\[
\mathcal{A}
=
\mathcal{A}_{task}
\cup
\mathcal{A}_{self}
\]

and the policy chooses:

\[
a_t \sim \pi(a_t \mid h_t)
\]

at every step.

## Example task actions

```text
EDIT_CODE
RUN_TEST
SEARCH
READ_DOC
USE_TOOL
ANSWER
```

## Example self-improvement actions

```text
UPDATE_MEMORY
DELETE_MEMORY
CREATE_SKILL
MODIFY_SKILL
MODIFY_HARNESS
CREATE_SCRIPT
VALIDATE_SELF_CHANGE
```

## Core research question

Under a finite budget:

\[
B = B_{acting} + B_{self-improving}
\]

can the model allocate compute intelligently between:

- solving the current task;
- improving future capability?

This is one of the central paradigms for the project.

## Important design choice

Self-improvement actions must have explicit cost.

Otherwise an agent can trivially spend unlimited computation on reflection / rewriting / validation.

## Recommended initial implementation

Expose explicit tool-like self-improvement actions:

```python
update_memory(...)
update_skill(...)
update_harness(...)
create_script(...)
validate_self_change(...)
```

At every reasoning step the agent may choose a task action or self-improvement action.

Log:

- time of self-improvement;
- trigger context;
- chosen artifact;
- tokens / tool calls consumed;
- eventual downstream gain.

## Related papers

There is not yet a single standard paper defining exactly this formulation. The name **Self-Improve-as-Action** is our operationalization.

Closest related work:

### Voyager

Continuous environment interaction with autonomous skill acquisition and persistent executable skill library.

- Paper: https://arxiv.org/abs/2305.16291

### Gödel Agent

Self-modification occurs as part of agent runtime and the agent can rewrite its own logic.

- Project / paper page: https://xunjianyin.github.io/papers/godel-agent.html

---

# 6. RSI Paradigm 3 — Open-Ended Self-Modification RSI

## Definition

Instead of exposing a predefined menu such as `UPDATE_MEMORY` and `UPDATE_SKILL`, directly expose a substantial part of the agent implementation.

Example:

```text
agent/
├── AGENTS.md
├── prompts/
├── skills/
├── tools/
├── retrieval.py
├── workflow.py
└── context_manager.py
```

The model is asked to modify itself to improve future reward.

\[
A_t
\xrightarrow[\text{proposed by }A_t]{modify}
A_{t+1}
\]

The key distinction from Self-Improve-as-Action:

### Self-Improve-as-Action

Humans define the allowed self-improvement operators.

The model chooses **when** to invoke them.

### Open-Ended Self-Modification

The model has much more freedom to determine **how** it should change itself.

## Possible modifications

- rewrite system / agent instructions;
- change retrieval policy;
- create new tools;
- modify context management;
- add critic / verifier steps;
- change debugging workflow;
- generate helper scripts;
- reorganize skills;
- alter self-improvement procedure.

## Safety / reproducibility recommendation

Do not initially expose unrestricted infrastructure.

Prefer a sandboxed editable subtree:

```text
editable_agent/
```

with immutable evaluator / benchmark / budget code outside it.

## Related papers

### Gödel Agent

Self-referential modification of agent logic and behavior.

- Project / paper page: https://xunjianyin.github.io/papers/godel-agent.html

### ADAS / Meta Agent Search

Meta-agent automatically programs new agent designs, including prompts, workflows and control logic.

- Paper: https://arxiv.org/abs/2408.08435
- GitHub: https://github.com/ShengranHu/ADAS

### STOP

Recursive self-improving scaffolding / optimizer program.

- Paper: https://arxiv.org/abs/2310.02304
- GitHub: https://github.com/microsoft/stop

---

# 7. RSI Paradigm 4 — Search / Evolution-based RSI

## Definition

Do not commit to a single self-modification.

Generate multiple candidate self-modified agents:

\[
A_t
\rightarrow
\left\{
A_t^{(1)},
A_t^{(2)},
\ldots,
A_t^{(K)}
\right\}
\]

then use executable feedback:

\[
Verifier
\rightarrow
Select / Archive / Mutate
\]

to decide which variants survive.

The self-improvement problem becomes a search problem.

## Possible mechanisms

- best-of-\(N\);
- mutation;
- rollback;
- archive;
- population;
- Pareto frontier;
- crossover / recombination;
- validation set;
- regime-specific agents.

## Why this matters in dynamic environments

A greedy update:

\[
A_t \rightarrow A_{t+1}
\]

can catastrophically damage the agent.

An evolutionary approach can preserve multiple alternatives.

In a dynamic environment, it may even be beneficial to preserve different agents for different regimes.

## Recommended initial implementation

Keep this simple first:

1. current agent \(A_t\);
2. generate \(K=2\)–\(4\) mutations;
3. run a small validation budget;
4. preserve current agent as rollback;
5. select best candidate or maintain top-\(k\);
6. continue.

Avoid full-blown population search initially.

## Related papers

### Darwin Gödel Machine

Open-ended evolution of self-modifying coding agents with an archive of agent variants.

- Paper: https://arxiv.org/abs/2505.22954

### GEPA

Reflective prompt evolution with candidate generation and Pareto-based search.

- Paper: https://arxiv.org/abs/2507.19457
- GitHub: https://github.com/gepa-ai/gepa

### EvoSkill

Skill-level evolution with failure-driven proposals and validation.

- Paper: https://arxiv.org/abs/2603.02766

### ADAS

Search over agent designs.

- Paper: https://arxiv.org/abs/2408.08435

---

# 8. Static Baseline

## Static Agent

No persistent self-improvement.

\[
A_t = A_0
\]

The agent may reason, call tools and retry inside an individual task, but cannot persistently modify itself across tasks.

This baseline is necessary to answer:

> Does persistent self-improvement provide any benefit at all?

---

# 9. Optional Context-Only Baseline

Especially important for ContinualSkillBench.

Keep previous trajectories / observations available without explicitly extracting memory or skills.

This separates:

\[
\text{context accumulation}
\]

from

\[
\text{explicit self-improvement}
\]

Recommended mainly for ContinualSkillBench.

---

# 10. Unified Agent State

Use the same persistent state representation across benchmarks as much as possible.

```python
class AgentState:
    memory
    skills
    harness
    scripts

    update_history
    task_history

    compute_budget
    self_improvement_budget

    metadata
```

Conceptually:

\[
S_t =
(M_t, K_t, H_t, P_t, B_t)
\]

where:

- \(M_t\): memory;
- \(K_t\): skills;
- \(H_t\): harness / workflow;
- \(P_t\): helper programs / scripts;
- \(B_t\): remaining budget.

---

# 11. Unified Action Interface

Recommended high-level interface:

```python
class Action:
    type
    payload
    estimated_cost
```

## Task actions

```text
ACT
RUN_TOOL
RUN_TEST
EDIT_ENVIRONMENT
SEARCH
READ
ANSWER
```

## Self-improvement actions

```text
UPDATE_MEMORY
UPDATE_SKILL
UPDATE_HARNESS
CREATE_SCRIPT
DELETE_STALE_ARTIFACT
VALIDATE_SELF_CHANGE
ROLLBACK
```

For Open-Ended RSI, expose an editable agent subtree instead of restricting modifications to this finite action set.

---

# 12. Budget and Cost

This is essential.

Without explicit cost, stronger RSI methods can win simply by consuming much more computation.

Define:

\[
C =
C_{task}
+
\lambda C_{SI}
\]

Possible logged costs:

- input tokens;
- output tokens;
- model calls;
- environment interactions;
- test executions;
- self-improvement calls;
- validation rollouts;
- wall-clock time if useful.

Possible objective:

\[
J
=
\sum_t R_t
-
\lambda C_t
\]

Alternatively report a Pareto curve:

\[
(\text{performance}, \text{compute})
\]

rather than choosing one \(\lambda\).

---

# 13. Evaluation Metrics

## 13.1 Cumulative Reward

\[
R_{cum} = \sum_{t=1}^{T} R_t
\]

Primary long-horizon metric.

---

## 13.2 Adaptation Speed

After an environment change at time \(t_c\), measure how many interactions / tasks are required to recover.

Possible threshold metric:

\[
T_{recover}
=
\min \{k :
R_{t_c+k} \ge \alpha R_{pre-change}\}
\]

---

## 13.3 Adaptation Regret

\[
Regret
=
\sum_{t=t_c}^{t_c+K}
(R^*_{t} - R_t)
\]

Useful for EvoArena and SWE-Chain.

---

## 13.4 Forgetting / Staleness

Track:

- stale memory usage;
- obsolete skill invocation;
- regression on old tasks;
- unnecessary retention;
- destructive updates.

---

## 13.5 Self-Improvement Efficiency

Useful statistics:

\[
\frac{\Delta Reward}{SI\ Cost}
\]

and:

- useful self-updates / total self-updates;
- invalid / reverted self-updates;
- frequency of modification;
- time between environment shift and useful update.

---

## 13.6 Transfer

Evaluate on:

- unseen tasks;
- unseen environment changes;
- unseen package / repository;
- unseen domain if feasible.

This is required to distinguish:

\[
\text{task overfitting}
\]

from

\[
\text{general adaptation capability}
\]

---

# 14. Main Experiment Matrix

Initial experiment:

| Setting | EvoArena | ContinualSkillBench | SWE-Chain |
|---|---:|---:|---:|
| Static Agent | ✓ | ✓ | ✓ |
| Fixed-Loop RSI | ✓ | ✓ | ✓ |
| Self-Improve-as-Action | ✓ | ✓ | ✓ |
| Open-Ended Self-Mod | ✓ | ✓ | ✓ |
| Search / Evolution RSI | ✓ | ✓ | ✓ |

Additional:

| Setting | EvoArena | ContinualSkillBench | SWE-Chain |
|---|---:|---:|---:|
| Context-only | optional | **important** | optional |

This produces:

\[
3 \times (1 + 4)
\]

main benchmark × method combinations before model / seed expansion.

---

# 15. Which Benchmark Is Most Informative for Which Paradigm?

## EvoArena

Best for:

- Self-Improve-as-Action;
- Fixed-Loop RSI;
- stale memory / forgetting analysis.

Central question:

> When the environment changes, does the agent know when it should update itself?

---

## ContinualSkillBench

Best for:

- Fixed-Loop skill evolution;
- Open-Ended Self-Mod;
- Search / Evolution;
- Context-only comparison.

Central question:

> Is the system accumulating reusable capability or merely accumulating context?

---

## SWE-Chain

Best for:

- Self-Improve-as-Action;
- Open-Ended Self-Mod;
- Search / Evolution.

Central question:

> Can an agent continually improve its own coding workflow as a real codebase evolves?

---

# 16. Phase 2 — Meta-RSI

Do not implement this first.

Current paradigms typically assume an improvement operator:

\[
A_t \xrightarrow{U} A_{t+1}
\]

where \(U\) itself is fixed.

Meta-RSI allows:

\[
(A_t, U_t)
\rightarrow
(A_{t+1}, U_{t+1})
\]

The agent can modify how it improves itself.

Example:

Initial policy:

```text
After every failure, update a skill.
```

Later learned policy:

```text
Do not update persistent skills after isolated failures.

Accumulate evidence.
Detect whether the environment has changed.
Choose between:
- memory update,
- skill update,
- harness update.

Validate before replacing persistent artifacts.
```

This corresponds to:

\[
\text{learning how to self-improve}
\]

rather than only improving task-specific capabilities.

### Related paper

- MetaSkill-Evolve: https://arxiv.org/abs/2607.05297

---

# 17. Development Priority

## Priority 0 — Unified Infrastructure

Before implementing sophisticated RSI, define:

```text
BenchmarkAdapter
Agent
AgentState
Action
Trajectory
Feedback
Improver
Evaluator
CostTracker
```

Every benchmark should expose a common loop as much as possible.

Pseudo-interface:

```python
state = benchmark.reset()

while not benchmark.done():
    observation = benchmark.observe()

    action = agent.step(
        observation=observation,
        agent_state=agent_state,
        budget=budget,
    )

    result = benchmark.execute(action)

    trajectory.append(
        observation=observation,
        action=action,
        result=result,
    )

feedback = benchmark.evaluate()
```

RSI methods should plug into the same framework.

---

## Priority 1 — Static + Fixed-Loop

Implement first.

Goal:

- verify all three benchmarks run;
- persistent workspace works;
- evaluation and logging are correct.

---

## Priority 2 — Self-Improve-as-Action

This is the most important new formulation.

Need:

- unified self-improvement tools;
- explicit costs;
- reliable state persistence;
- full action logging.

---

## Priority 3 — Open-Ended Self-Mod

Expose a sandboxed editable harness / agent directory.

Need:

- immutable evaluator;
- git / snapshot support;
- rollback;
- execution sandbox;
- change diff logging.

---

## Priority 4 — Search / Evolution

Add:

- candidate generation;
- validation;
- archive;
- rollback;
- top-\(k\) selection.

Keep \(K\) small at first.

---

# 18. Implementation Checklist Before 2026-09-27

## Benchmark setup

- [ ] Run one short EvoArena sequence end-to-end.
- [ ] Run one ContinualSkillBench task sequence end-to-end.
- [ ] Run one short SWE-Chain upgrade chain end-to-end.
- [ ] Verify benchmark reward / verifier output.
- [ ] Verify state persists across tasks / episodes.

## Unified agent state

- [ ] Implement `MEMORY.md`.
- [ ] Implement `skills/`.
- [ ] Implement `AGENTS.md` / harness state.
- [ ] Implement `scripts/`.
- [ ] Implement versioned snapshots.

## Logging

- [ ] Save full trajectories.
- [ ] Save reward / verifier feedback.
- [ ] Save every self-modification diff.
- [ ] Save compute / token / tool cost.
- [ ] Save current benchmark environment version / stage.
- [ ] Save rollback history.

## Static baseline

- [ ] Run static agent on a small sequence for each benchmark.
- [ ] Confirm reproducibility.
- [ ] Record task success and cost.

## Fixed-Loop RSI

- [ ] Implement fixed post-task improvement loop.
- [ ] Support memory edits.
- [ ] Support skill edits.
- [ ] Support harness edits.
- [ ] Add optional validation before accepting updates.

## Self-Improve-as-Action

- [ ] Implement self-improvement actions.
- [ ] Expose them inside rollout.
- [ ] Assign explicit costs.
- [ ] Record when and why they are invoked.
- [ ] Make `DO_NOTHING` / continue-task always available.

## Minimal analysis

- [ ] Plot performance across task index.
- [ ] Mark environment change points.
- [ ] Plot self-improvement actions over time.
- [ ] Report cumulative reward.
- [ ] Report total compute / SI cost.
- [ ] Inspect 5–10 trajectories manually.

---

# 19. First Milestone

By 2026-09-27, success does **not** require proving RSI works.

The target is:

1. all three environments are runnable;
2. persistent agent state is implemented;
3. Static and Fixed-Loop baselines work;
4. Self-Improve-as-Action can be executed at least in one benchmark;
5. trajectories, self-modifications and costs are fully logged.

The objective is to obtain enough hands-on evidence to decide:

- which benchmark is most informative;
- whether the action-space formulation is viable;
- what the main failure modes are;
- which artifact should be exposed to the agent;
- whether open-ended self-modification is tractable.

---

# 20. Current Working Hypothesis

The project should not be framed primarily as:

> Which memory / skill method is best?

A better formulation is:

> **How should self-improvement be organized for agents operating in continually evolving environments?**

We compare a spectrum:

\[
\boxed{
\text{Human-designed update loop}
\rightarrow
\text{Agent chooses when to self-improve}
\rightarrow
\text{Agent chooses how to self-modify}
\rightarrow
\text{Search / evolution discovers better agents}
}
\]

and ask whether the agent eventually learns not only task-specific knowledge, but increasingly effective strategies for adapting itself.

