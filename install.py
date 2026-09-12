#!/usr/bin/env python3
"""Install codesage-orchestrator into a project, or check an existing install.

  python3 install.py                                    # interactive
  python3 install.py --target ../app --plan plus --yes  # non-interactive
  python3 install.py --target ../app --dry-run          # show what would change
  python3 install.py doctor --target ../app             # validate an install

Standard library only; Python 3.8+.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
PLANS = {
    "pro": "GPT-6 Astra (medium) orchestrates, GPT-5.6 Luna (max) executes, Astra (low) reviews",
    "plus": "GPT-5.6 Luna (max) orchestrates, Luna (medium) executes, Astra (low) reviews",
    "lite": "GPT-5.6 Luna (medium) orchestrates, Luna (low) executes, Luna (medium) reviews",
}
COMPONENTS = {
    "codex": ".codex/  (config.toml + role files)",
    "skill": ".agents/ (sage-orchestrator skill)",
    "agents-md": "AGENTS.md (orchestration block, merged into any existing file)",
}
ROLES = ("scout", "builder", "verifier", "scholar", "critic")
READ_ONLY_ROLES = {"scout", "scholar", "critic"}
SKILL_REL = Path(".agents/skills/sage-orchestrator/SKILL.md")
BEGIN = "<!-- codesage-orchestrator:begin -->"
END = "<!-- codesage-orchestrator:end -->"

BANNER = r"""
   ___          _        ____
  / __\___   __| | ___  / ___|  __ _  __ _  ___
 / /  / _ \ / _` |/ _ \ \___ \ / _` |/ _` |/ _ \
/ /__| (_) | (_| |  __/  ___) | (_| | (_| |  __/
\____/\___/ \__,_|\___| |____/ \__,_|\__, |\___|
     o r c h e s t r a t o r         |___/
"""


class SetupError(Exception):
    pass


# --------------------------------------------------------------------------- prompts


def interactive() -> bool:
    return sys.stdin.isatty()


def ask(prompt: str) -> str:
    try:
        return input(prompt)
    except EOFError:
        raise SetupError("input ended before setup was complete") from None


def confirm(prompt: str, default: bool) -> bool:
    suffix = "[Y/n]" if default else "[y/N]"
    while True:
        answer = ask(f"{prompt} {suffix} ").strip().lower()
        if not answer:
            return default
        if answer in ("y", "yes"):
            return True
        if answer in ("n", "no"):
            return False
        print("Please answer y or n.")


def choose_plan() -> str:
    names = list(PLANS)
    print("Plan:")
    for i, name in enumerate(names, 1):
        print(f"  {i}) {name:<5} {PLANS[name]}")
    while True:
        answer = ask(f"Select plan [1-{len(names)}] (default 1): ").strip().lower()
        if not answer:
            return names[0]
        if answer in PLANS:
            return answer
        if answer.isdigit() and 1 <= int(answer) <= len(names):
            return names[int(answer) - 1]
        print("Please pick one of the listed plans.")


# --------------------------------------------------------------------------- planning


class Change:
    def __init__(self, kind: str, dest: Path, data: bytes, component: str):
        self.kind = kind  # create | update | merge | same
        self.dest = dest
        self.data = data
        self.component = component


def check_path_safe(target: Path, dest: Path) -> None:
    """Refuse to write through symlinks or over mismatched file types."""
    current = target
    for part in dest.relative_to(target).parts[:-1]:
        current = current / part
        if current.is_symlink():
            raise SetupError(f"refusing to write through symbolic link: {current}")
        if current.exists() and not current.is_dir():
            raise SetupError(f"expected a directory but found a file: {current}")
    if dest.is_symlink():
        raise SetupError(f"refusing to replace symbolic link: {dest}")
    if dest.exists() and not dest.is_file():
        raise SetupError(f"expected a file but found something else: {dest}")


def plan_tree(target: Path, source: Path, dest_root: Path, component: str) -> list[Change]:
    if not source.is_dir():
        raise SetupError(f"setup source is missing: {source}")
    changes = []
    for src in sorted(p for p in source.rglob("*") if p.is_file()):
        dest = dest_root / src.relative_to(source)
        check_path_safe(target, dest)
        data = src.read_bytes()
        if not dest.exists():
            kind = "create"
        elif dest.read_bytes() == data:
            kind = "same"
        else:
            kind = "update"
        changes.append(Change(kind, dest, data, component))
    return changes


def merge_agents_md(existing: str | None, block: str) -> str:
    if existing is None:
        return block + "\n"
    newline = "\r\n" if "\r\n" in existing else "\n"
    block = block.replace("\n", newline)
    start, end = existing.find(BEGIN), existing.find(END)
    if start != -1 and end > start:
        return existing[:start] + block + existing[end + len(END) :]
    return existing.rstrip("\r\n") + newline * 2 + block + newline


def plan_agents_md(target: Path) -> list[Change]:
    dest = target / "AGENTS.md"
    check_path_safe(target, dest)
    block = (HERE / "AGENTS.md").read_text(encoding="utf-8").strip("\n")
    if not dest.exists():
        return [Change("create", dest, merge_agents_md(None, block).encode("utf-8"), "agents-md")]
    raw = dest.read_bytes()
    try:
        existing = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise SetupError(f"{dest} is not UTF-8; add the block from AGENTS.md by hand") from None
    merged = merge_agents_md(existing, block)
    bom = b"\xef\xbb\xbf" if raw.startswith(b"\xef\xbb\xbf") else b""
    data = bom + merged.encode("utf-8")
    return [Change("same" if data == raw else "merge", dest, data, "agents-md")]


def build_changes(target: Path, plan: str, components: list[str]) -> list[Change]:
    profile = HERE / "profiles" / plan
    changes: list[Change] = []
    if "codex" in components:
        changes += plan_tree(target, profile / "codex", target / ".codex", "codex")
    if "skill" in components:
        changes += plan_tree(target, profile / "agents", target / ".agents", "skill")
    if "agents-md" in components:
        changes += plan_agents_md(target)
    return changes


def apply(changes: list[Change]) -> None:
    for change in changes:
        if change.kind == "same":
            continue
        change.dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = change.dest.with_name(change.dest.name + ".codesage-tmp")
        tmp.write_bytes(change.data)
        os.replace(tmp, change.dest)


# --------------------------------------------------------------------------- install


def resolve_target(raw: str | None) -> Path:
    if not raw:
        if not interactive():
            raise SetupError("--target is required when not running interactively")
        raw = ask("Target project path: ").strip()
    if not raw:
        raise SetupError("target path cannot be empty")
    target = Path(raw).expanduser()
    if not target.is_dir():
        raise SetupError(f"target must be an existing directory: {raw}")
    target = target.resolve()
    if target == HERE:
        raise SetupError("target must be a different directory from this setup repository")
    return target


def run_install(args: argparse.Namespace) -> int:
    if interactive() and not args.yes:
        print(BANNER)
    target = resolve_target(args.target)

    plan = args.plan
    if plan is None:
        plan = choose_plan() if interactive() and not args.yes else "pro"

    if args.only:
        components = [c.strip() for c in args.only.split(",") if c.strip()]
        unknown = [c for c in components if c not in COMPONENTS]
        if unknown:
            raise SetupError(f"unknown component(s): {', '.join(unknown)}. Choose from: {', '.join(COMPONENTS)}")
    elif interactive() and not args.yes:
        components = [c for c, label in COMPONENTS.items() if confirm(f"Install {label}?", True)]
    else:
        components = list(COMPONENTS)
    if not components:
        print("Nothing selected.")
        return 0

    changes = build_changes(target, plan, components)
    symbols = {"create": "+", "update": "!", "merge": "~", "same": "="}
    print(f"\nTarget: {target}\nPlan:   {plan} - {PLANS[plan]}\n")
    for change in changes:
        print(f"  {symbols[change.kind]} {change.dest.relative_to(target)}")
    print("\n  + new   ! overwrite changed file   ~ merge block into AGENTS.md   = unchanged\n")

    updates = [c for c in changes if c.kind == "update"]
    if args.dry_run:
        print("Dry run: nothing was written.")
        return 0

    if updates and not args.overwrite:
        if interactive() and not args.yes:
            keep = not confirm(f"Overwrite the {len(updates)} changed file(s) marked '!'?", False)
        else:
            keep = True
        if keep:
            print(f"Keeping {len(updates)} existing file(s). Re-run with --overwrite to replace them.")
            changes = [c for c in changes if c.kind != "update"]

    pending = [c for c in changes if c.kind != "same"]
    if not pending:
        print("Already up to date.")
        return 0
    if interactive() and not args.yes and not confirm(f"Write {len(pending)} file(s)?", True):
        print("Cancelled. Nothing was written.")
        return 1

    apply(pending)
    print(f"Done. Wrote {len(pending)} file(s).")
    print(f"Next: python3 {Path(__file__).name} doctor --target {target}")
    print("Then trust the project in Codex and start it from the target directory.")
    return 0


# --------------------------------------------------------------------------- doctor


def load_toml(path: Path):
    try:
        import tomllib  # Python 3.11+
    except ImportError:
        return None
    with path.open("rb") as fh:
        return tomllib.load(fh)


def codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")


def matching_plan(target: Path) -> str | None:
    for plan in PLANS:
        profile = HERE / "profiles" / plan
        pairs = [(profile / "codex", target / ".codex"), (profile / "agents", target / ".agents")]
        if all(
            (dest / src.relative_to(base)).is_file() and (dest / src.relative_to(base)).read_bytes() == src.read_bytes()
            for base, dest in pairs
            for src in base.rglob("*")
            if src.is_file()
        ):
            return plan
    return None


def run_doctor(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="install.py doctor", description="Validate an installed project.")
    parser.add_argument("--target", default=".", help="project directory (default: current directory)")
    args = parser.parse_args(argv)
    target = Path(args.target).expanduser().resolve()

    results: list[tuple[str, str]] = []
    ok = lambda msg: results.append(("ok", msg))  # noqa: E731
    warn = lambda msg: results.append(("warn", msg))  # noqa: E731
    fail = lambda msg: results.append(("FAIL", msg))  # noqa: E731
    have_toml = load_toml(HERE / "profiles" / "pro" / "codex" / "config.toml") is not None
    if not have_toml:
        warn("Python 3.11+ is needed to parse TOML; only file presence was checked")

    config_path = target / ".codex" / "config.toml"
    if not config_path.is_file():
        fail(".codex/config.toml is missing")
    elif have_toml:
        try:
            config = load_toml(config_path)
        except Exception as exc:  # tomllib.TOMLDecodeError
            fail(f".codex/config.toml does not parse: {exc}")
        else:
            agents = config.get("agents", {})
            if agents.get("enabled") is True:
                ok(f"root model {config.get('model', '?')} / {config.get('model_reasoning_effort', '?')}, agents enabled")
            else:
                fail(".codex/config.toml: [agents] enabled is not true, so subagents cannot spawn")

    for role in ROLES:
        path = target / ".codex" / "agents" / f"{role}.toml"
        if not path.is_file():
            fail(f".codex/agents/{role}.toml is missing")
            continue
        if not have_toml:
            continue
        try:
            spec = load_toml(path)
        except Exception as exc:
            fail(f".codex/agents/{role}.toml does not parse: {exc}")
            continue
        problems = []
        if spec.get("name") != role:
            problems.append(f"name is {spec.get('name')!r}")
        if not spec.get("model"):
            problems.append("no model pinned")
        if not spec.get("developer_instructions"):
            problems.append("no developer_instructions")
        if role in READ_ONLY_ROLES and spec.get("sandbox_mode") != "read-only":
            problems.append(f"sandbox_mode is {spec.get('sandbox_mode')!r}, expected 'read-only'")
        if problems:
            fail(f"{role}: " + "; ".join(problems))
        else:
            ok(f"{role:<8} {spec['model']} / {spec.get('model_reasoning_effort', 'default')} ({spec.get('sandbox_mode')})")

    if (target / SKILL_REL).is_file():
        ok("sage-orchestrator skill installed")
    else:
        warn(f"{SKILL_REL} is missing; invoke-by-name ($sage-orchestrator) will not work")

    agents_md = target / "AGENTS.md"
    if agents_md.is_file() and BEGIN in agents_md.read_text(encoding="utf-8", errors="replace"):
        ok("AGENTS.md contains the orchestration block")
    else:
        warn("AGENTS.md has no orchestration block")

    plan = matching_plan(target)
    if plan:
        ok(f"installed files match the '{plan}' profile exactly")
    elif config_path.is_file():
        warn("installed files differ from every bundled profile (customized, or from an older release)")

    global_config = codex_home() / "config.toml"
    if have_toml and global_config.is_file():
        try:
            projects = load_toml(global_config).get("projects", {})
        except Exception:
            projects = {}
        trust = projects.get(str(target), {}).get("trust_level")
        if trust == "trusted":
            ok("project is trusted in Codex, so .codex/ will be loaded")
        else:
            warn("project is not marked trusted in Codex; project .codex/ config is ignored until you trust it")

    for status, message in results:
        print(f"[{status:>4}] {message}")
    failed = sum(1 for status, _ in results if status == "FAIL")
    print(f"\n{'FAILED' if failed else 'Healthy'}: {failed} failure(s), "
          f"{sum(1 for s, _ in results if s == 'warn')} warning(s) in {target}")
    return 1 if failed else 0


# --------------------------------------------------------------------------- main


def main(argv: list[str]) -> int:
    try:
        if argv[:1] == ["doctor"]:
            return run_doctor(argv[1:])
        parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
        parser.add_argument("--target", help="existing project directory to install into")
        parser.add_argument("--plan", choices=list(PLANS), help="profile to install (default: pro)")
        parser.add_argument("--only", metavar="LIST", help=f"comma-separated components: {', '.join(COMPONENTS)}")
        parser.add_argument("--yes", "-y", action="store_true", help="no prompts; never overwrites changed files")
        parser.add_argument("--overwrite", action="store_true", help="replace existing files that differ")
        parser.add_argument("--dry-run", action="store_true", help="show the changes without writing")
        return run_install(parser.parse_args(argv))
    except SetupError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nCancelled.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
