# Plans, presets, and global setup

## How settings resolve

1. **Root agent**: `model` and `model_reasoning_effort` in `.codex/config.toml`. For a trusted project,
   this overrides `~/.codex/config.toml`.
2. **Named roles** (`scout`, `builder`, …): the `model` and `model_reasoning_effort` pinned in
   `.codex/agents/<role>.toml`. These win over `[agents]` defaults.
3. **Unnamed spawns**: `[agents] default_subagent_model` and `default_subagent_reasoning_effort`.
4. **Explicit per-spawn choices** made by the root override all of the above. The skill tells the root not
   to do this unless you ask.

To make every role follow the `[agents]` defaults, delete the `model` and `model_reasoning_effort` lines from the role
files. Better still, change `PROFILES` in `scripts/build_profiles.py` and regenerate.

## Choosing a plan

- **pro**: Astra plans and reviews, while Luna at max reasoning does the work. Best results, highest usage.
- **plus**: the root moves to Luna at max reasoning. It is usually the best trade-off because the root is the largest
  line item. The critic stays on Astra, so review comes from a different model than the one that planned and wrote the change.
- **lite**: everything on Luna, low effort for execution roles, two concurrent subagents. Use it for routine
  work, or when a 5-hour window is nearly used up.

## Optional root presets

These are overrides for the root only. Put them in `.codex/config.toml` or `~/.codex/config.toml`. If your
Codex version doesn't support `service_tier`, drop that line.

**Fast iteration** (latency first):

```toml
model = "gpt-6-astra"
model_reasoning_effort = "low"
service_tier = "fast"
```

**Deep work** (architecture changes, nasty cross-component bugs):

```toml
model = "gpt-6-astra"
model_reasoning_effort = "high"
```

**Routine coding** on a Plus plan:

```toml
model = "gpt-5.6-luna"
model_reasoning_effort = "medium"
service_tier = "fast"
```

If you change the root model, the topology table in the installed `SKILL.md` becomes out of date. Update it,
or add a plan to `PROFILES` and regenerate so the skill stays accurate.

## Concurrency

`max_concurrent_threads_per_session` caps parallel subagents. Every concurrent subagent carries and
re-reads its own context, so parallelism trades tokens for wall time. Raise it (6–8) only for large
repositories where scouting or building is truly independent.

## Global setup (all projects)

The installer targets one project. For a personal setup:

```bash
PLAN=plus
mkdir -p ~/.codex/agents ~/.agents/skills
cp profiles/$PLAN/codex/agents/*.toml ~/.codex/agents/
cp -R profiles/$PLAN/agents/skills/sage-orchestrator ~/.agents/skills/
```

Then **merge**, don't copy, the keys from `profiles/$PLAN/codex/config.toml` into `~/.codex/config.toml`.
Your global config likely holds MCP servers, project trust entries, and other settings you don't want to lose.
