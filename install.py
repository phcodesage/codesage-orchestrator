#!/usr/bin/env python3
"""Install, update, uninstall, or bundle codesage-orchestrator.

  python3 install.py                                    # interactive
  python3 install.py --target ../app --plan plus --yes  # non-interactive
  python3 install.py --target ../app --dry-run          # show what would change
  python3 install.py update --target ../app --plan plus
  python3 install.py uninstall --target ../app --yes
  python3 install.py global --plan plus --yes
  python3 install.py bundle --plan plus --output sage-orchestrator-plus.zip
  python3 install.py doctor --target ../app             # validate an install

Standard library only; Python 3.8+.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import zipfile
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
MANIFEST_REL = Path(".codesage-orchestrator/manifest.json")
BEGIN = "<!-- codesage-orchestrator:begin -->"
END = "<!-- codesage-orchestrator:end -->"
GLOBAL_MANIFEST_REL = Path("codesage-orchestrator/manifest.json")
ROOT_CONFIG_KEYS = ("model", "model_reasoning_effort", "approval_policy", "sandbox_mode")
AGENTS_CONFIG_KEYS = (
    "enabled",
    "max_concurrent_threads_per_session",
    "default_subagent_model",
    "default_subagent_reasoning_effort",
)

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


class Removal:
    def __init__(self, path: Path, expected_hash: str | None = None, label: str | None = None):
        self.path = path
        self.expected_hash = expected_hash
        self.label = label or str(path)


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
        check_path_safe(change.dest.parent, tmp)
        tmp.write_bytes(change.data)
        os.replace(tmp, change.dest)


def file_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def manifest_path(target: Path) -> Path:
    return target / MANIFEST_REL


def global_manifest_path() -> Path:
    return codex_home() / GLOBAL_MANIFEST_REL


def read_manifest(path: Path) -> dict:
    if not path.exists():
        return {}
    if path.is_symlink() or not path.is_file():
        raise SetupError(f"refusing to read an unsafe manifest path: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SetupError(f"manifest does not contain valid JSON: {path} ({exc})") from None
    if not isinstance(data, dict) or data.get("version") != 1 or not isinstance(data.get("files", {}), dict):
        raise SetupError(f"unsupported or malformed manifest: {path}")
    return data


def write_manifest(path: Path, data: dict, safety_root: Path) -> None:
    check_path_safe(safety_root, path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".codesage-tmp")
    check_path_safe(safety_root, tmp)
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def update_project_manifest(
    target: Path,
    plan: str,
    original_changes: list[Change],
    components: list[str],
    replace_updates: bool,
    agents_md_created: bool,
    existed_before: dict[str, bool],
) -> None:
    path = manifest_path(target)
    existing = read_manifest(path)
    files = dict(existing.get("files", {}))
    if "agents-md" in components:
        files.pop("AGENTS.md", None)
    for change in original_changes:
        if change.component not in components or change.component == "agents-md":
            continue
        if change.kind in ("create", "same") or (change.kind == "update" and replace_updates):
            relative = str(change.dest.relative_to(target))
            previous = files.get(relative, {})
            if not isinstance(previous, dict):
                previous = {}
            remove_if_pristine = previous.get("remove_if_pristine")
            if remove_if_pristine is None:
                remove_if_pristine = not existed_before.get(relative, True)
            files[relative] = {
                "component": change.component,
                "sha256": file_hash(change.data),
                "remove_if_pristine": remove_if_pristine,
            }

    data = {
        "version": 1,
        "scope": "project",
        "plan": plan,
        "files": files,
    }
    if "agents-md" in components:
        data["agents_md"] = True
        data["agents_md_created"] = existing.get("agents_md_created", agents_md_created)
    else:
        for key in ("agents_md", "agents_md_created"):
            if key in existing:
                data[key] = existing[key]
    write_manifest(path, data, target)


def update_global_manifest(
    plan: str,
    original_changes: list[Change],
    replace_updates: bool,
    existed_before: dict[str, bool],
    config_created: bool,
) -> None:
    path = global_manifest_path()
    existing = read_manifest(path)
    files = dict(existing.get("files", {}))
    for change in original_changes:
        if change.component == "config" or change.kind in ("create", "same") or (change.kind == "update" and replace_updates):
            absolute = str(change.dest)
            previous = files.get(absolute, {})
            if not isinstance(previous, dict):
                previous = {}
            remove_if_pristine = previous.get("remove_if_pristine")
            if remove_if_pristine is None:
                if change.component == "config":
                    remove_if_pristine = existing.get("config_created", config_created)
                else:
                    remove_if_pristine = not existed_before.get(absolute, True)
            files[absolute] = {
                "component": change.component,
                "sha256": file_hash(change.data),
                "remove_if_pristine": remove_if_pristine,
            }
    data = {
        "version": 1,
        "scope": "global",
        "plan": plan,
        "files": files,
        "config_created": existing.get("config_created", config_created),
    }
    write_manifest(path, data, codex_home())


def remove_empty_parents(path: Path, stop: Path) -> None:
    current = path.parent
    while current != stop and current != current.parent:
        try:
            current.rmdir()
        except OSError:
            break
        current = current.parent


def remove_agents_block(path: Path, created_by_installer: bool = False) -> tuple[bool, bool]:
    """Remove the marked block. Return (changed, file_removed)."""
    if not path.is_file() or path.is_symlink():
        return False, False
    raw = path.read_bytes()
    bom = b"\xef\xbb\xbf" if raw.startswith(b"\xef\xbb\xbf") else b""
    try:
        existing = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return False, False
    start, end = existing.find(BEGIN), existing.find(END)
    if start == -1 or end < start:
        return False, False
    end += len(END)
    before, after = existing[:start], existing[end:]
    newline = "\r\n" if "\r\n" in existing else "\n"
    before, after = before.rstrip("\r\n"), after.lstrip("\r\n")
    if before and after:
        cleaned = before + newline * 2 + after
    elif before:
        cleaned = before + newline
    else:
        cleaned = after
    if created_by_installer and not cleaned.strip():
        path.unlink()
        return True, True
    check_path_safe(path.parent, path)
    tmp = path.with_name(path.name + ".codesage-tmp")
    check_path_safe(path.parent, tmp)
    tmp.write_bytes(bom + cleaned.encode("utf-8"))
    os.replace(tmp, path)
    return True, False


def hash_matches(path: Path, expected_hash: str | None) -> bool:
    if not expected_hash or path.is_symlink() or not path.is_file():
        return False
    try:
        return file_hash(path.read_bytes()) == expected_hash
    except OSError:
        return False


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


def run_project_install(args: argparse.Namespace, action: str = "install") -> int:
    if interactive() and not args.yes:
        print(BANNER)
    target = resolve_target(args.target)

    plan = args.plan
    project_manifest = read_manifest(manifest_path(target))
    if plan is None:
        if action == "update":
            plan = project_manifest.get("plan") or matching_plan(target) or "pro"
        else:
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
    original_changes = list(changes)
    existed_before = {
        str(change.dest.relative_to(target)): change.dest.exists() or change.dest.is_symlink()
        for change in original_changes
        if change.component != "agents-md"
    }
    symbols = {"create": "+", "update": "!", "merge": "~", "same": "="}
    label = "Update" if action == "update" else "Target"
    print(f"\n{label}: {target}\nPlan:   {plan} - {PLANS[plan]}\n")
    for change in changes:
        print(f"  {symbols[change.kind]} {change.dest.relative_to(target)}")
    print("\n  + new   ! overwrite changed file   ~ merge block into AGENTS.md   = unchanged\n")

    updates = [c for c in changes if c.kind == "update"]
    if args.dry_run:
        print("Dry run: nothing was written.")
        return 0

    replace_updates = bool(args.overwrite)
    if updates and not args.overwrite:
        if interactive() and not args.yes:
            replace_updates = confirm(f"Overwrite the {len(updates)} changed file(s) marked '!'?", False)
        else:
            replace_updates = False
        if not replace_updates:
            print(f"Keeping {len(updates)} existing file(s). Re-run with --overwrite to replace them.")
            changes = [c for c in changes if c.kind != "update"]

    pending = [c for c in changes if c.kind != "same"]
    if not pending:
        update_project_manifest(
            target,
            plan,
            original_changes,
            components,
            replace_updates,
            not (target / "AGENTS.md").exists(),
            existed_before,
        )
        print("Already up to date.")
        return 0
    if interactive() and not args.yes and not confirm(f"Write {len(pending)} file(s)?", True):
        print("Cancelled. Nothing was written.")
        return 1

    agents_md_created = not (target / "AGENTS.md").exists()
    apply(pending)
    update_project_manifest(
        target,
        plan,
        original_changes,
        components,
        replace_updates,
        agents_md_created,
        existed_before,
    )
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


def profile_config_assignments(text: str) -> tuple[dict[str, str], dict[str, str]]:
    """Extract the scalar settings this kit owns without requiring tomllib."""
    root: dict[str, str] = {}
    agents: dict[str, str] = {}
    section = "root"
    wanted_root, wanted_agents = set(ROOT_CONFIG_KEYS), set(AGENTS_CONFIG_KEYS)
    for line in text.splitlines():
        header = re.match(r"^\s*\[([^\]]+)\]\s*$", line)
        if header:
            section = header.group(1)
            continue
        match = re.match(r"^\s*([A-Za-z0-9_-]+)\s*=\s*(.*?)\s*$", line)
        if not match:
            continue
        key, value = match.groups()
        if section == "root" and key in wanted_root:
            root[key] = value
        elif section == "agents" and key in wanted_agents:
            agents[key] = value
    missing = wanted_root - root.keys() | wanted_agents - agents.keys()
    if missing:
        raise SetupError(f"profile config is missing expected settings: {', '.join(sorted(missing))}")
    return root, agents


def replace_config_section(text: str, section: str, assignments: dict[str, str], newline: str) -> str:
    """Replace owned scalar keys in one TOML section while preserving other text."""
    lines = text.splitlines(keepends=True)
    if section == "root":
        start, end = 0, next((i for i, line in enumerate(lines) if re.match(r"^\s*\[\[?[^\]]+\]\]?", line)), len(lines))
    else:
        header = re.compile(rf"^\s*\[{re.escape(section)}\]\s*$")
        start = next((i for i, line in enumerate(lines) if header.match(line.rstrip("\r\n"))), -1)
        if start == -1:
            suffix = "" if not text or text.endswith(("\n", "\r")) else newline
            return text + suffix + f"[{section}]" + newline + "".join(
                f"{key} = {value}{newline}" for key, value in assignments.items()
            )
        end = next(
            (i for i in range(start + 1, len(lines)) if re.match(r"^\s*\[\[?[^\]]+\]\]?", lines[i])),
            len(lines),
        )

    found: set[str] = set()
    output: list[str] = []
    for line in lines[:start]:
        output.append(line)
    if section != "root":
        output.extend(lines[start : start + 1])
    for line in lines[start if section == "root" else start + 1 : end]:
        match = re.match(r"^(\s*)([A-Za-z0-9_-]+)\s*=\s*.*?(\r?\n)?$", line)
        if match and match.group(2) in assignments:
            key = match.group(2)
            if key in found:
                continue
            output.append(f"{match.group(1)}{key} = {assignments[key]}{newline}")
            found.add(key)
        else:
            output.append(line)
    missing = [key for key in assignments if key not in found]
    if missing:
        if output and not output[-1].endswith(("\n", "\r")):
            output.append(newline)
        output.extend(f"{key} = {assignments[key]}{newline}" for key in missing)
    output.extend(lines[end:])
    return "".join(output)


def merge_global_config(existing: str, profile: str) -> str:
    root, agents = profile_config_assignments(profile)
    newline = "\r\n" if "\r\n" in existing else "\n"
    merged = replace_config_section(existing, "root", root, newline)
    return replace_config_section(merged, "agents", agents, newline)


def backup_path(path: Path) -> Path:
    candidate = path.with_name(path.name + ".codesage-backup")
    index = 1
    while candidate.exists() or candidate.is_symlink():
        candidate = path.with_name(path.name + f".codesage-backup.{index}")
        index += 1
    return candidate


def backup_file(path: Path) -> Path:
    destination = backup_path(path)
    check_path_safe(path.parent, destination)
    shutil.copy2(path, destination)
    return destination


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


def build_global_changes(plan: str) -> list[Change]:
    profile = HERE / "profiles" / plan
    home = Path.home()
    codex_root = codex_home()
    changes = plan_tree(codex_root, profile / "codex" / "agents", codex_root / "agents", "codex")
    changes += plan_tree(home, profile / "agents", home / ".agents", "skill")

    config_path = codex_root / "config.toml"
    check_path_safe(codex_root, config_path)
    profile_config = (profile / "codex" / "config.toml").read_text(encoding="utf-8")
    if config_path.exists():
        try:
            existing_config = config_path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            raise SetupError(f"{config_path} is not UTF-8; merge the plan settings by hand") from None
        merged_config = merge_global_config(existing_config, profile_config)
        data = merged_config.encode("utf-8")
        kind = "same" if data == config_path.read_bytes() else "update"
    else:
        data = profile_config.encode("utf-8")
        kind = "create"
    changes.append(Change(kind, config_path, data, "config"))
    return changes


def run_global_install(args: argparse.Namespace, action: str = "global") -> int:
    if interactive() and not args.yes:
        print(BANNER)
    existing_manifest = read_manifest(global_manifest_path())
    plan = args.plan
    if plan is None:
        if action == "update":
            plan = existing_manifest.get("plan") or "pro"
        else:
            plan = choose_plan() if interactive() and not args.yes else "pro"

    changes = build_global_changes(plan)
    original_changes = list(changes)
    existed_before = {str(change.dest): change.dest.exists() or change.dest.is_symlink() for change in original_changes}
    config_created = not (codex_home() / "config.toml").exists()
    print(f"\nGlobal setup\nPlan:   {plan} - {PLANS[plan]}\n")
    symbols = {"create": "+", "update": "!", "same": "=", "config": "~"}
    for change in changes:
        symbol = "~" if change.component == "config" and change.kind == "update" else symbols[change.kind]
        print(f"  {symbol} {change.dest}")
    print("\n  + new   ! overwrite changed role/skill file   ~ merge global config   = unchanged\n")

    if args.dry_run:
        print("Dry run: nothing was written.")
        return 0

    updates = [c for c in changes if c.kind == "update" and c.component != "config"]
    replace_updates = bool(args.overwrite)
    if updates and not args.overwrite:
        if interactive() and not args.yes:
            replace_updates = confirm(f"Overwrite the {len(updates)} changed role/skill file(s) marked '!'?", False)
        if not replace_updates:
            print(f"Keeping {len(updates)} existing role/skill file(s). Re-run with --overwrite to replace them.")
            changes = [c for c in changes if c.kind != "update" or c.component == "config"]

    pending = [c for c in changes if c.kind != "same"]
    if not pending:
        update_global_manifest(plan, original_changes, replace_updates, existed_before, config_created)
        print("Already up to date.")
        return 0
    if interactive() and not args.yes and not confirm(f"Write {len(pending)} global file(s)?", True):
        print("Cancelled. Nothing was written.")
        return 1

    config_change = next((c for c in pending if c.component == "config"), None)
    if config_change is not None and config_change.kind == "update":
        backup = backup_file(config_change.dest)
        print(f"Backed up existing config to {backup}")
    apply(pending)
    update_global_manifest(plan, original_changes, replace_updates, existed_before, config_created)
    print(f"Done. Wrote {len(pending)} global file(s).")
    print("Launch Codex from a project and invoke $sage-orchestrator when needed.")
    return 0


def legacy_project_removals(target: Path) -> list[Removal]:
    removals: list[Removal] = []
    seen: set[Path] = set()
    for plan in PLANS:
        profile = HERE / "profiles" / plan
        for source_root, destination_root in (
            (profile / "codex", target / ".codex"),
            (profile / "agents", target / ".agents"),
        ):
            if not source_root.is_dir():
                continue
            for source in source_root.rglob("*"):
                if not source.is_file():
                    continue
                destination = destination_root / source.relative_to(source_root)
                if destination in seen or destination.is_symlink() or not destination.is_file():
                    continue
                try:
                    check_path_safe(target, destination)
                    matches = destination.read_bytes() == source.read_bytes()
                except (OSError, SetupError):
                    continue
                if matches:
                    removals.append(Removal(destination, file_hash(source.read_bytes()), str(destination.relative_to(target))))
                    seen.add(destination)
    return removals


def project_removals(target: Path, manifest: dict) -> tuple[list[Removal], dict[str, dict]]:
    if not manifest:
        return legacy_project_removals(target), {}
    if manifest.get("scope") not in (None, "project"):
        raise SetupError("project uninstall found a global manifest")
    removals: list[Removal] = []
    remaining: dict[str, dict] = {}
    for relative, metadata in manifest.get("files", {}).items():
        if not isinstance(relative, str) or not isinstance(metadata, dict):
            continue
        destination = target / relative
        try:
            destination.relative_to(target)
            check_path_safe(target, destination)
        except (ValueError, SetupError):
            remaining[relative] = metadata
            continue
        if metadata.get("remove_if_pristine", True) is False:
            print(f"Keeping pre-existing file: {destination}")
            continue
        expected = metadata.get("sha256")
        if destination.is_symlink():
            print(f"Keeping symlink: {destination}")
            remaining[relative] = metadata
        elif not destination.exists():
            continue
        elif hash_matches(destination, expected):
            removals.append(Removal(destination, expected, relative))
        else:
            print(f"Keeping modified file: {destination}")
            remaining[relative] = metadata
    return removals, remaining


def run_project_uninstall(args: argparse.Namespace) -> int:
    target = resolve_target(args.target)
    manifest_file = manifest_path(target)
    manifest = read_manifest(manifest_file)
    removals, remaining = project_removals(target, manifest)
    agents_file = target / "AGENTS.md"
    has_agents_block = False
    if agents_file.is_file() and not agents_file.is_symlink():
        try:
            content = agents_file.read_text(encoding="utf-8-sig")
        except UnicodeDecodeError:
            content = ""
        has_agents_block = BEGIN in content and END in content and content.find(END) >= content.find(BEGIN)

    if not removals and not has_agents_block:
        print(f"No removable codesage-orchestrator files found in {target}.")
        return 0
    for removal in removals:
        print(f"  - {removal.label}")
    if has_agents_block:
        print(f"  - orchestration block in {agents_file.relative_to(target)}")
    if args.dry_run:
        print("Dry run: nothing was removed.")
        return 0
    count = len(removals) + int(has_agents_block)
    if interactive() and not args.yes and not confirm(f"Remove {count} codesage-orchestrator item(s)?", False):
        print("Cancelled. Nothing was removed.")
        return 1

    removed = 0
    for removal in removals:
        if hash_matches(removal.path, removal.expected_hash):
            removal.path.unlink()
            remove_empty_parents(removal.path, target)
            removed += 1
        else:
            remaining[str(removal.path.relative_to(target))] = manifest.get("files", {}).get(
                str(removal.path.relative_to(target)), {}
            )

    block_removed = False
    if has_agents_block:
        check_path_safe(target, agents_file)
        changed, file_removed = remove_agents_block(
            agents_file,
            bool(manifest.get("agents_md_created")),
        )
        block_removed = changed
        if file_removed:
            remove_empty_parents(agents_file, target)

    if manifest:
        if block_removed:
            remaining_agents = False
        else:
            remaining_agents = has_agents_block
        if remaining or remaining_agents:
            updated = {
                "version": 1,
                "scope": "project",
                "plan": manifest.get("plan", "pro"),
                "files": remaining,
            }
            if remaining_agents:
                updated["agents_md"] = True
                updated["agents_md_created"] = manifest.get("agents_md_created", False)
            write_manifest(manifest_file, updated, target)
        elif manifest_file.exists():
            check_path_safe(target, manifest_file)
            manifest_file.unlink()
            remove_empty_parents(manifest_file, target)

    print(f"Removed {removed + int(block_removed)} codesage-orchestrator item(s).")
    return 0


def safe_global_file(path: Path) -> bool:
    if not path.is_absolute() or path.is_symlink():
        return False
    for root in (codex_home(), Path.home() / ".agents"):
        try:
            path.relative_to(root)
        except ValueError:
            continue
        return True
    return False


def legacy_global_removals() -> list[Removal]:
    removals: list[Removal] = []
    seen: set[Path] = set()
    home, codex_root = Path.home(), codex_home()
    for plan in PLANS:
        profile = HERE / "profiles" / plan
        for source_root, destination_root in (
            (profile / "codex" / "agents", codex_root / "agents"),
            (profile / "agents", home / ".agents"),
            (profile / "codex", codex_root),
        ):
            if not source_root.is_dir():
                continue
            for source in source_root.rglob("*"):
                if not source.is_file():
                    continue
                destination = destination_root / source.relative_to(source_root)
                if destination in seen or not safe_global_file(destination) or not destination.is_file():
                    continue
                try:
                    matches = destination.read_bytes() == source.read_bytes()
                except OSError:
                    continue
                if matches:
                    removals.append(Removal(destination, file_hash(source.read_bytes()), str(destination)))
                    seen.add(destination)
    return removals


def run_global_uninstall(args: argparse.Namespace) -> int:
    manifest_file = global_manifest_path()
    manifest = read_manifest(manifest_file)
    if manifest and manifest.get("scope") not in (None, "global"):
        raise SetupError("global uninstall found a project manifest")

    removals: list[Removal] = []
    remaining: dict[str, dict] = {}
    if manifest:
        for absolute, metadata in manifest.get("files", {}).items():
            if not isinstance(absolute, str) or not isinstance(metadata, dict):
                continue
            destination = Path(absolute)
            if metadata.get("remove_if_pristine", True) is False:
                print(f"Keeping pre-existing file: {destination}")
                continue
            if not safe_global_file(destination):
                remaining[absolute] = metadata
            elif destination.is_symlink():
                print(f"Keeping symlink: {destination}")
                remaining[absolute] = metadata
            elif not destination.exists():
                continue
            elif hash_matches(destination, metadata.get("sha256")):
                removals.append(Removal(destination, metadata.get("sha256"), absolute))
            else:
                print(f"Keeping modified file: {destination}")
                remaining[absolute] = metadata
    else:
        removals = legacy_global_removals()

    if not removals:
        print("No removable global codesage-orchestrator files found.")
        return 0
    for removal in removals:
        print(f"  - {removal.label}")
    if args.dry_run:
        print("Dry run: nothing was removed.")
        return 0
    if interactive() and not args.yes and not confirm(f"Remove {len(removals)} global file(s)?", False):
        print("Cancelled. Nothing was removed.")
        return 1

    removed = 0
    for removal in removals:
        if hash_matches(removal.path, removal.expected_hash):
            removal.path.unlink()
            stop = codex_home()
            try:
                removal.path.relative_to(Path.home() / ".agents")
            except ValueError:
                pass
            else:
                stop = Path.home()
            remove_empty_parents(removal.path, stop)
            removed += 1
        else:
            remaining[str(removal.path)] = manifest.get("files", {}).get(str(removal.path), {})
    if manifest:
        if remaining:
            write_manifest(
                manifest_file,
                {"version": 1, "scope": "global", "plan": manifest.get("plan", "pro"), "files": remaining},
                codex_home(),
            )
        elif manifest_file.exists():
            check_path_safe(codex_home(), manifest_file)
            manifest_file.unlink()
            remove_empty_parents(manifest_file, codex_home())
    print(f"Removed {removed} global file(s).")
    return 0


def run_bundle(args: argparse.Namespace) -> int:
    plan = args.plan or "pro"
    source = HERE / "profiles" / plan / "agents" / "skills" / "sage-orchestrator"
    output = Path(args.output or f"sage-orchestrator-{plan}.zip").expanduser()
    if output.exists() or output.is_symlink():
        if output.is_symlink():
            raise SetupError(f"refusing to replace symbolic link: {output}")
        if not output.is_file():
            raise SetupError(f"bundle output is not a regular file: {output}")
        if not args.overwrite:
            raise SetupError(f"bundle already exists: {output}; pass --overwrite to replace it")
    if args.dry_run:
        print(f"Would create {output} containing {source.relative_to(HERE)}/")
        return 0
    if not source.is_dir():
        raise SetupError(f"skill source is missing: {source}")
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        output.unlink()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for file in sorted(source.rglob("*")):
            if file.is_file():
                archive.write(file, (Path("sage-orchestrator") / file.relative_to(source)).as_posix())
    print(f"Created {output} with the sage-orchestrator skill.")
    return 0


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


def add_project_options(parser: argparse.ArgumentParser, include_only: bool = True) -> None:
    parser.add_argument("--target", help="existing project directory")
    parser.add_argument("--plan", choices=list(PLANS), help="profile to install (default: pro)")
    if include_only:
        parser.add_argument("--only", metavar="LIST", help=f"comma-separated components: {', '.join(COMPONENTS)}")
    parser.add_argument("--yes", "-y", action="store_true", help="no prompts; never overwrites changed files")
    parser.add_argument("--overwrite", action="store_true", help="replace existing files that differ")
    parser.add_argument("--dry-run", action="store_true", help="show the changes without writing")


def main(argv: list[str]) -> int:
    try:
        if argv[:1] == ["doctor"]:
            return run_doctor(argv[1:])
        if argv[:1] == ["global"]:
            parser = argparse.ArgumentParser(
                prog="install.py global",
                description="Install the selected plan into your global Codex configuration.",
            )
            parser.add_argument("--plan", choices=list(PLANS), help="profile to install (default: pro)")
            parser.add_argument("--yes", "-y", action="store_true", help="no prompts")
            parser.add_argument("--overwrite", action="store_true", help="replace changed role and skill files")
            parser.add_argument("--dry-run", action="store_true", help="show the changes without writing")
            return run_global_install(parser.parse_args(argv[1:]))
        if argv[:1] == ["update"]:
            parser = argparse.ArgumentParser(prog="install.py update", description="Update a project or global installation safely.")
            add_project_options(parser)
            parser.add_argument("--global", dest="global_scope", action="store_true", help="update the global installation")
            args = parser.parse_args(argv[1:])
            if args.global_scope:
                return run_global_install(args, action="update")
            return run_project_install(args, action="update")
        if argv[:1] == ["uninstall"]:
            parser = argparse.ArgumentParser(prog="install.py uninstall", description="Remove a project or global installation safely.")
            parser.add_argument("--target", help="existing project directory")
            parser.add_argument("--global", dest="global_scope", action="store_true", help="remove the global installation")
            parser.add_argument("--yes", "-y", action="store_true", help="no prompts")
            parser.add_argument("--dry-run", action="store_true", help="show what would be removed")
            args = parser.parse_args(argv[1:])
            if args.global_scope:
                return run_global_uninstall(args)
            return run_project_uninstall(args)
        if argv[:1] == ["bundle"]:
            parser = argparse.ArgumentParser(prog="install.py bundle", description="Create a shareable skill ZIP bundle.")
            parser.add_argument("--plan", choices=list(PLANS), default="pro", help="profile whose skill to bundle")
            parser.add_argument("--output", help="output ZIP path (default: sage-orchestrator-<plan>.zip)")
            parser.add_argument("--overwrite", action="store_true", help="replace an existing ZIP")
            parser.add_argument("--dry-run", action="store_true", help="show what would be bundled")
            return run_bundle(parser.parse_args(argv[1:]))
        parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
        add_project_options(parser)
        return run_project_install(parser.parse_args(argv))
    except SetupError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nCancelled.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
