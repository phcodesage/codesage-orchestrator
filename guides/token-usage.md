# Measuring token usage

Orchestration costs more than a single agent, and how much more depends on the repository, the task, and
how many subagents actually spawn. Measure your own runs rather than trusting anyone's single number.

## Where the data comes from

Codex writes one JSONL rollout per thread to `~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl`, or to
`$CODEX_HOME/sessions` if that is set. The script uses:

| Record | Fields used |
|---|---|
| `session_meta` (first line) | `id`, `session_id` (shared by the root and all its subagents), `source.subagent.thread_spawn.agent_role`, `cwd`, `cli_version` |
| `turn_context` | `model`, `effort`. The last one wins, since subagents start with the parent's settings. |
| `token_usage_record` | per-response `input_tokens`, `cached_input_tokens`, `output_tokens`, `reasoning_output_tokens` |
| `event_msg` / `token_count` | `rate_limits.primary` (5-hour) and `secondary` (7-day) `used_percent`, `plan_type` |

Grouping rollouts by `session_id` gives the full cost of one task with no extra instrumentation.

## Commands

```bash
scripts/token_usage.py sessions                     # orchestrated sessions, newest first
scripts/token_usage.py sessions --all --date 2026-09-12
scripts/token_usage.py report                       # newest session that spawned subagents
scripts/token_usage.py report 01a0954b              # any unique id prefix
scripts/token_usage.py report 01a0954b --format json
scripts/token_usage.py report 01a0954b --format csv >> runs.csv
```

`--date` limits the scan to one day's folder and is much faster on large histories. Codex's own auto-review
(guardian) threads are shown in the thread count but excluded from totals unless you pass
`--include-auto-review`.

## Reading the numbers

- **Look at uncached input and output, not total tokens.** Cache hits are usually more than 90% of input, so totals
  overstate the real work by an order of magnitude.
- **The rate-limit delta is what Plus and Pro users pay with.** Plans are metered on 5-hour and 7-day windows,
  and the token-to-percent mapping is not published. The window is account-wide, so other sessions running
  at the same time inflate it.
- **The root is usually the largest line item.** It stays alive for the whole task and re-reads its context
  on every response. This is why the `plus` and `lite` plans move it to Luna.

## A fair comparison

1. Pick 3–4 representative tasks in one repository, for example a one-file fix, a multi-file feature, a
   cross-component bug, and a change that needs external research. Save the prompts and reuse them verbatim.
2. Run each task with:
   - a baseline root-only run (`[agents] enabled = false`, no skill)
   - each plan you are considering
3. Repeat each combination 2–3 times. Run-to-run variance is large.
4. Append `--format csv` output to one file and compare uncached input, output, subagent count, wall time,
   and rate-limit delta. Note the Codex version, because caching behavior changes between releases.

## Reducing usage

In rough order of impact:

1. Use `plus` or `lite` to take the root off Astra.
2. Don't orchestrate small tasks. The skill's triage step should already keep one-file edits solo.
3. Lower `max_concurrent_threads_per_session`.
4. Keep subagent reports short. The role prompts cap their formats, so don't ask for full files or logs.
5. Skip `critic` for low-risk changes that already have good test coverage.
