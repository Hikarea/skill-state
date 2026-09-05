"""Install standalone SKILL.state plugin into a Hermes home."""

from __future__ import annotations

import argparse
import os
import json
import shutil
import subprocess
from pathlib import Path


FILES = ("plugin.yaml", "__init__.py", "hermes_state_engine.py", "hermes_step_engine.py", "context_policy.py")


def resolve_home(hermes: str, requested: Path | None) -> tuple[Path, dict[str, str]]:
    env = dict(os.environ)
    if requested is not None:
        env["HERMES_HOME"] = str(requested)
        return requested, env
    result = subprocess.run(
        [hermes, "config", "path"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode or not result.stdout.strip():
        raise SystemExit("Could not locate Hermes home; pass --home explicitly.")
    return Path(result.stdout.strip()).parent, env


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--home", type=Path)
    parser.add_argument("--hermes", default="hermes")
    parser.add_argument("--hermes-source", type=Path, help="Installed Hermes source; required for the explicit step-transition bridge")
    parser.add_argument("--mode", choices=["step", "turn"], default="step")
    parser.add_argument("--context-mode", choices=["strict", "evidence"], default="strict")
    parser.add_argument("--schema", type=Path, help="Optional domain JSON Schema (requires jsonschema in Hermes)")
    parser.add_argument("--state", type=Path, help="Initial state for the domain schema")
    args = parser.parse_args()
    if args.mode == "step" and args.hermes_source is None:
        parser.error("--mode step requires --hermes-source; no observer-hook mutation or two-call fallback is used")
    if bool(args.schema) != bool(args.state):
        parser.error("--schema and --state must be supplied together")
    domain = None
    if args.schema:
        from jsonschema import Draft202012Validator
        domain = (json.loads(args.schema.read_text(encoding="utf-8")),
                  json.loads(args.state.read_text(encoding="utf-8")))
        Draft202012Validator.check_schema(domain[0])
        Draft202012Validator(domain[0]).validate(domain[1])
    home, env = resolve_home(args.hermes, args.home)
    if args.mode == "step":
        from hermes_transition_bridge import install_bridge
        install_bridge(args.hermes_source)

    root = Path(__file__).resolve().parents[1]
    target = home / "plugins" / "skill-state"
    target.mkdir(parents=True, exist_ok=True)
    for name in FILES:
        source = root / name
        if not source.is_file():
            raise SystemExit(f"Missing plugin source: {source}")
        temporary = target / f"{name}.tmp"
        shutil.copy2(source, temporary)
        os.replace(temporary, target / name)

    config_path = home / "skill-state" / "config.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config = json.loads(config_path.read_text(encoding="utf-8")) if config_path.exists() else {}
    config.update(mode=args.mode, context_mode=args.context_mode)
    if domain is not None:
        config.update(state_schema=domain[0], initial_state=domain[1])
    config_temp = config_path.with_suffix(".tmp")
    config_temp.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    os.replace(config_temp, config_path)

    commands = [[args.hermes, "config", "set", "context.engine", "skill-state"]]
    if (home / "plugins" / "skill-state-native").exists():
        commands.append([args.hermes, "plugins", "disable", "skill-state-native"])
    commands.append([args.hermes, "plugins", "enable", "skill-state"])
    for command in commands:
        result = subprocess.run(command, env=env, text=True, check=False)
        if result.returncode:
            return result.returncode

    print(f"Installed standalone SKILL.state plugin in {target}")
    print("Restart Hermes Desktop, gateway, or CLI sessions to activate it.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
