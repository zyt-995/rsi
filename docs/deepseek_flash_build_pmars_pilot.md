# DeepSeek Flash single-chain pilot — 2026-09-24

Scope: exactly `build-pmars` plus EVO-1 through EVO-4, each run once under
baseline, skill-RSI, and harness-RSI (15 scored trials). Root:
`runs/deepseek-flash-build-pmars-20260924`. The existing Qwen campaign was not
modified. All five task environments passed preflight before launch. The
existing clean agent, method budgets, Docker environments, and graders were
reused; the baseline was solved fresh rather than imported from Qwen.

The pilot ran 10:26:53–10:43:38 Asia/Shanghai (16.76 minutes elapsed with
three arms concurrent). All 15 records completed and all five tasks are paired.
There were 221 successful API requests and no API errors. The DeepSeek API key
was held in the coordinator's memory, not written to the run or source files;
the temporary local gateway token/config were removed at completion.

| Method | DeepSeek passed | Qwen passed on this chain | Sum of stage times |
| --- | ---: | ---: | ---: |
| Baseline | 4/5 | 0/5 | 12.6 min DeepSeek; Qwen had historical reuse |
| Skill-RSI | 5/5 | 2/5 | 13.0 min DeepSeek vs 103.9 min Qwen |
| Harness-RSI | 4/5 | 2/5 | 16.6 min DeepSeek vs 99.4 min Qwen |

The stage-time comparisons are descriptive, not controlled throughput ratios:
the models generated different trajectories and outputs. The score difference
also does not isolate RSI effects from model strength. DeepSeek's high baseline
on this chain leaves little headroom for RSI.

API usage: 1,820,160 cache-hit input tokens, 393,483 cache-miss input tokens,
128,757 output tokens. At DeepSeek Flash's 2026-09-24 peak rates of ¥0.04,
¥2, and ¥8 per million respectively, the token-based estimate is **¥1.8898**
for this complete three-arm chain; it is not an account invoice. The off-peak
equivalent is ¥0.9449. Request logs omit prompts, responses, and credentials.

Important validity notes:

- Skill-RSI's first improvement proposal was rejected because DeepSeek returned
  malformed JSON with one extra trailing character. Three later skill
  improvements were accepted. No proposal was repaired or resampled.
- The final harness-RSI trial stopped after the evolved harness exceeded the
  protocol's 8 KB per-task state limit while retaining recent tool output.
  The grader returned 0. This is a generated-harness/controller failure, not
  an API, Docker, or grader failure. Earlier harness improvements were accepted.
- No completed trial was repeated. The Qwen campaign remained alive with four
  healthy endpoints and zero blocked tasks at the closing check.

Reproduce inspection with the read-only `status.json`, per-stage `record.json`,
and `api_usage.jsonl` in the pilot root. The isolated runner is
`rsi_harness/deepseek_pilot.py`; four focused tests passed in
`tests/test_deepseek_pilot.py`.
