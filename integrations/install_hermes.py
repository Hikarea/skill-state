"""Install standalone SKILL.state plugin into a Hermes home."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
from pathlib import Path


FILES = ("plugin.yaml", "__init__.py", "hermes_state_engine.py")


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
    args = parser.parse_args()
    home, env = resolve_home(args.hermes, args.home)

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
