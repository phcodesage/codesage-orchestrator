<!-- codesage-orchestrator:begin -->
## Orchestration (codesage-orchestrator)

For non-trivial coding work, use the `sage-orchestrator` skill.

- The root agent owns the plan, the architecture, integration, and final verification.
- Delegate bounded work to the named roles in `.codex/agents/`: `scout` (read-only mapping),
  `builder` (implementation), `verifier` (tests), `scholar` (external facts), `critic` (review).
- Skip delegation for small, single-file, or conversational tasks.
- One writer per file or subsystem. Never let two builders edit the same file.
- The user's explicit instructions always override this policy.
<!-- codesage-orchestrator:end -->
