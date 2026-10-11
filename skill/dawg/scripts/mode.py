#!/usr/bin/env python3
"""Print the dawg messaging mode: "interactive" or "non-interactive".

Usage: mode.py [--non-interactive | --interactive]

Precedence, first match wins:
  1. a flag passed here (from /dawg's arguments)
  2. workspace config: <git root or cwd>/.claude/dawg.toml, .codex/dawg.toml, .agents/dawg.toml
  3. global config:    ~/.claude/dawg.toml, ~/.codex/dawg.toml, ~/.agents/dawg.toml
  4. default:          interactive

A config file holds one setting:
  mode = "non-interactive"   # or "interactive"
The file that decided is printed to stderr.
"""

import subprocess
import sys
import tomllib
from pathlib import Path

MODES = ("interactive", "non-interactive")
AGENT_DIRS = (".claude", ".codex", ".agents")


def workspace_root():
    try:
        out = subprocess.run(["git", "rev-parse", "--show-toplevel"],
                             capture_output=True, text=True, timeout=5)
        if out.returncode == 0:
            return Path(out.stdout.strip())
    except (OSError, subprocess.TimeoutExpired):
        pass
    return Path.cwd()


def mode_from(path):
    try:
        mode = tomllib.loads(path.read_text()).get("mode")
    except (OSError, tomllib.TOMLDecodeError) as e:
        print(f"dawg: ignoring {path}: {e}", file=sys.stderr)
        return None
    if mode not in MODES:
        print(f"dawg: ignoring {path}: mode must be one of {MODES}", file=sys.stderr)
        return None
    return mode


def main():
    for arg in sys.argv[1:]:
        if arg.lstrip("-") in MODES:
            print(arg.lstrip("-"))
            print("dawg: mode from arguments", file=sys.stderr)
            return
    for root in (workspace_root(), Path.home()):
        for d in AGENT_DIRS:
            path = root / d / "dawg.toml"
            if path.is_file() and (mode := mode_from(path)):
                print(mode)
                print(f"dawg: mode from {path}", file=sys.stderr)
                return
    print("interactive")
    print("dawg: default mode", file=sys.stderr)


if __name__ == "__main__":
    main()
