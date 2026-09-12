# codesage-orchestrator

A drop-in multi-agent setup for [OpenAI Codex](https://github.com/openai/codex).
A strong root agent plans, delegates, integrates, and verifies. Five named
subagents do bounded work: **scout** maps code, **builder** implements,
**verifier** tests, **scholar** checks external facts, and **critic** reviews.

```text
                     root (orchestrator)
             plans · decides · integrates · verifies
                              │
      ┌───────────┬───────────┼───────────┬───────────┐
    scout      scholar     builder     verifier     critic
  read-only   read-only    writes      tests       read-only
    map        facts      one scope   evidence   independent
                                                    review
```

By [@phcodesage](https://github.com/phcodesage). Inspired by and partly adapted
from [donvito/codex-astra-luna-orchestrator](https://github.com/donvito/codex-astra-luna-orchestrator)
(see [NOTICE](NOTICE)).

## What's different

- **Three plans generated from one source.** `pro`, `plus`, and `lite` are built by
  `scripts/build_profiles.py`, so model choices, role prompts, and the skill text never drift. CI fails if
  `profiles/` is stale.
- **One cross-platform installer.** `install.py` holds all the logic. `setup.sh` and `setup.ps1` are
  ten-line wrappers. It supports non-interactive flags, `--dry-run`, and per-component installs.
- **Safe re-runs.** Unchanged files are skipped, and changed files are kept unless you pass `--overwrite`. It never
  writes through symlinks. The `AGENTS.md` block sits between markers, so updates replace it in place
  instead of appending duplicates.
- **`doctor` command.** Validates the installed TOML, role pins, and read-only sandboxes. It also tells you whether the files
  match a bundled plan and whether Codex actually trusts the project (untrusted projects ignore `.codex/`).
- **Faster token reports.** `scripts/token_usage.py` pre-filters rollout lines before parsing and adds CSV
  output.
- **Tighter orchestration skill.** It covers a solo-vs-orchestrate triage, a six-line delegation contract,
  a failure playbook, and a pre-answer checklist.

## Plans

| | `pro` | `plus` | `lite` |
|---|---|---|---|
| root | GPT-6 Astra · medium | GPT-5.6 Luna · max | GPT-5.6 Luna · medium |
| scout, builder, verifier, scholar | GPT-5.6 Luna · max | GPT-5.6 Luna · medium | GPT-5.6 Luna · low |
| critic | GPT-6 Astra · low | GPT-6 Astra · low | GPT-5.6 Luna · medium |
| concurrent subagents | 4 | 3 | 2 |
| pick it when | quality first | most Plus users | routine work, near a rate limit |

The root is the biggest token consumer in any orchestrated session, since it stays in the loop and
re-reads its context every turn. Moving the root to a cheaper model therefore saves the most. See
[guides/token-usage.md](guides/token-usage.md).

## Install

Requires Python 3.8+ (3.11+ for full `doctor` validation) and an existing target project.

```bash
git clone https://github.com/phcodesage/codesage-orchestrator.git
cd codesage-orchestrator

./setup.sh                                   # interactive (macOS / Linux)
./setup.sh --target ../my-app --plan plus --yes
./setup.sh --target ../my-app --dry-run      # preview only
```

Windows:

```powershell
powershell -ExecutionPolicy Bypass -File .\setup.ps1 --target ..\my-app --plan plus --yes
```

You can also call `python3 install.py ...` directly on any OS.

| Flag | Meaning |
|---|---|
| `--target PATH` | project to install into (prompted if omitted) |
| `--plan pro\|plus\|lite` | plan to install (default `pro`) |
| `--only codex,skill,agents-md` | install a subset |
| `--yes` | no prompts; changed files are kept |
| `--overwrite` | replace files that differ from the plan |
| `--dry-run` | print `+ new`, `! overwrite`, `~ merge`, and `= unchanged` without writing |

What lands in the target:

```text
my-app/
├── .codex/
│   ├── config.toml            root model + [agents] settings
│   └── agents/                scout, builder, verifier, scholar, critic (.toml)
├── .agents/skills/sage-orchestrator/SKILL.md
└── AGENTS.md                  orchestration block (merged, never clobbered)
```

Then check it:

```bash
python3 install.py doctor --target ../my-app
```

Codex only loads project-scoped `.codex/` for **trusted** projects. Open Codex in the project once and
trust it. `doctor` warns you if you haven't.

## Use

Codex picks up the skill automatically for multi-file work. You can also invoke it explicitly:

```text
$sage-orchestrator

Add CSV export to the invoices page.
Scout the API route and the React table first, then one builder per side,
verifier for the export endpoint, and critic on the final diff.
```

## Troubleshooting

- **Root runs on your global model, not the plan's.** The project is not trusted yet. Codex loads skills in
  untrusted projects, but ignores project `.codex/config.toml` until trust is saved. A one-off
  `-c projects."<path>".trust_level="trusted"` override does not count. Open the project in Codex once and accept
  the trust prompt, then run `doctor`.
- **`--yolo` / `--dangerously-bypass-approvals-and-sandbox`** gives every subagent full access, including
  scout, scholar, and critic, which the role files mark read-only. Use it only in disposable or sandboxed checkouts.
- **"Full-history forked agents inherit the parent agent type"** means the root spawned a named role with
  `fork_context: true`. The skill tells it not to. Codex retries on its own, but update the skill if you edited it.

## Measure token usage

```bash
scripts/token_usage.py sessions --date 2026-09-12          # orchestrated sessions that day
scripts/token_usage.py report                              # newest session with subagents
scripts/token_usage.py report 01a0954b --format csv > run.csv
```

It reads the rollout logs Codex already writes to `~/.codex/sessions` (or `$CODEX_HOME/sessions`) and never
modifies them.

## Customize

Don't hand-edit `profiles/`, because it is generated.

1. Change models, efforts, or concurrency in `PROFILES`, or role sandboxes and descriptions in `ROLES`, in
   [`scripts/build_profiles.py`](scripts/build_profiles.py).
2. Change role prompts in [`templates/roles/`](templates/roles) or the skill in [`templates/skill.md`](templates/skill.md).
3. Run `python3 scripts/build_profiles.py`, then `python3 -m unittest discover -s tests`.

For global (all projects) setup and per-task presets, see [guides/profiles.md](guides/profiles.md).

## Contributing

Issues and PRs are welcome. Keep the installer and scripts standard-library only, regenerate profiles, and
make sure `python3 -m unittest discover -s tests` passes.

## License

[Apache License 2.0](LICENSE). Copyright 2026 phcodesage. See [NOTICE](NOTICE) for attribution.
