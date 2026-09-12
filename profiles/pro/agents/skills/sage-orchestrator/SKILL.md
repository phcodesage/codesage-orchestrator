---
name: sage-orchestrator
description: Orchestrate multi-step Codex coding work (Pro profile: gpt-6-astra medium root, gpt-5.6-luna max execution roles, gpt-6-astra low critic). Use for multi-file features, cross-component debugging, repo-wide changes, parallel workstreams, or when the user asks for subagents. Skip for single-file edits and plain questions.
---

# Sage Orchestrator (Pro profile)

User instructions override everything in this skill.

## Topology

```text
root      gpt-6-astra   medium  plans, integrates, verifies
scout     gpt-5.6-luna  max     read-only mapping
builder   gpt-5.6-luna  max     bounded implementation
verifier  gpt-5.6-luna  max     tests and reproduction
scholar   gpt-5.6-luna  max     external facts
critic    gpt-6-astra   low     independent review
```

Named roles pin their own model and reasoning effort in `.codex/agents/*.toml`.
Spawn them by role name and do not override their model unless the user asks or
a role reports a blocker that needs deeper reasoning. Model escalation is the
root's decision, never a subagent's.

## 1. Triage: solo or orchestrate?

Work solo when all of these hold:

- the change fits in one or two files you can already see
- no unfamiliar code path needs mapping first
- there is nothing to parallelize
- a separate review would not change your confidence

Orchestrate when any of these hold:

- the change spans modules, packages, or services
- unfamiliar code must be mapped before deciding anything
- there are independent workstreams
- external or version-specific facts need checking
- the change is risky enough to deserve an independent review
- the user asks for agents, delegation, or parallel work

State the chosen mode in one line, then proceed. Once you orchestrate, actually
call `spawn_agent`; describing delegation without spawning does not count. If
spawning fails, tell the user and ask whether to continue solo.

When spawning a named role, set `agent_type` to the role and `fork_context` to
`false`. Full-history forks inherit the parent's agent type, so Codex rejects
them when combined with `agent_type`. Put the context the role needs in the
contract instead.

## 2. The root's job

The root owns the goal, the plan, architecture decisions, file ownership,
resolving conflicting reports, integration, and the final answer. Subagents
return evidence and bounded changes. They never own direction.

## 3. Writing a contract

Every spawn gets a contract with these six lines:

```text
Goal:        one concrete outcome
Scope:       files / symbols / subsystem the agent may read or touch
Context:     only the facts needed, including relevant scout findings
Constraints: what must not change (APIs, schemas, deps, other owners' files)
Deliver:     the exact report or change expected
Done when:   a checkable acceptance criterion
```

Weak: "Look into the auth bug."

Strong: "Goal: find where expired refresh tokens are accepted. Scope: read-only,
`src/auth/**`. Deliver: the `file:line` where expiry should be checked, the
current flow, and existing tests. Done when: you can name one function to change."

## 4. Choosing roles

| Role | Sandbox | Use it for |
|---|---|---|
| `scout` | read-only | mapping code paths, locating symbols, configs, and tests |
| `builder` | workspace-write | one bounded implementation with named file ownership |
| `verifier` | workspace-write | reproducing bugs, targeted test runs, adding tests on request |
| `scholar` | read-only | version-specific API or framework facts from primary sources |
| `critic` | read-only | independent review of the finished diff |

Use only the roles the task needs. A one-module feature might be
scout → builder → verifier. A risky migration adds critic.

## 5. Sequencing

Spawn independent work together, then wait for all of it:

```text
spawn scout(api) + scout(web) + scholar(sdk pagination)  ->  wait for all  ->  decide
```

Serialize when a step depends on another's output:

```text
map -> decide -> build -> verify -> critique -> fix material findings -> final check
```

Give each file or subsystem exactly one builder. If two builders need the same
file, sequence them or merge their contracts.

For bugs: reproduce first (verifier, or scout if reproduction needs mapping),
collect evidence, name the root cause in the root, then have one builder fix it
and the verifier confirm the original reproduction now passes. Do not run
competing fixes unless you deliberately want alternatives.

## 6. Keeping context lean

The root re-reads its context on every turn, so everything a subagent returns is
paid for repeatedly.

- Ask for the report formats the roles already define, not full files or raw logs.
- Reduce each report to the facts you need before spawning the next step.
- Stay within the configured concurrency (4) and parallelize only truly independent work.
- Skip `critic` for low-risk, well-tested changes.

## 7. When things go wrong

- Blocked subagent: answer its question, decide, or narrow the contract, then re-spawn. Never let it widen its own scope.
- Failed or useless result: retry once with a tighter contract. If it fails again, do the work in the root and say so in the final answer.
- Conflicting reports: gather the missing evidence with a targeted scout or verifier run instead of picking one.

## 8. Before you answer

- Every spawned agent has finished or explicitly failed; nothing is still running.
- You read the final diff yourself.
- Blocker and major critic findings are fixed, or deferred with a stated reason.
- The most valuable check ran: the original reproduction, targeted tests, a typecheck, or a build.

Final answer: what changed, how it was verified, and open risks. Name the roles
that contributed in one line. Give per-agent detail (role, model, task, status)
only when the user asks. Never claim a role ran unless `spawn_agent` succeeded
for it.
