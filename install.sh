#!/usr/bin/env bash
# install.sh - install dawg, dawgd and the agent skill for the current user.
#
# Usage: ./install.sh [--no-service]
#
# Links dawg into ~/.local/bin and the skill into ~/.claude/skills, creates
# per-user config in ~/.config/dawg (never overwriting existing files), and
# installs dawgd as a user systemd service. Safe to re-run.

set -euo pipefail

repo="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
config="$HOME/.config/dawg"
service=1
[[ "${1:-}" == "--no-service" ]] && service=0

link() {  # link TARGET NAME: point NAME at TARGET, replacing an old symlink
  mkdir -p "$(dirname "$2")"
  if [[ -e "$2" && ! -L "$2" ]]; then
    echo "skip: $2 exists and is not a symlink" >&2
    return
  fi
  ln -sfn "$1" "$2"
  echo "linked $2 -> $1"
}

link "$repo/dawg" "$HOME/.local/bin/dawg"
link "$repo/skill/dawg" "$HOME/.claude/skills/dawg"

mkdir -p "$config"
chmod 700 "$config"

if [[ ! -s "$config/key" ]]; then
  if [[ -t 0 ]]; then
    read -rsp "Bark device key (from the Bark app; blank to skip): " key; echo
  else
    key=""
  fi
  if [[ -n "$key" ]]; then
    (umask 077; printf '%s\n' "$key" > "$config/key")
    echo "wrote $config/key"
  else
    echo "note: no device key; write it to $config/key or set DAWG_KEY" >&2
  fi
fi

if [[ ! -e "$config/dawgd.toml" ]]; then
  host=""
  if command -v tailscale >/dev/null; then
    host="$(tailscale status --json 2>/dev/null | python3 -c \
      'import json,sys; print(json.load(sys.stdin)["Self"]["DNSName"].rstrip("."))' 2>/dev/null || true)"
  fi
  {
    echo '# dawgd listens on localhost; `tailscale serve --bg 8787` exposes it to the tailnet over HTTPS.'
    echo 'bind = "127.0.0.1"'
    echo 'port = 8787'
    if [[ -n "$host" ]]; then
      echo "base_url = \"https://$host\""
    else
      echo '# base_url = "https://<machine>.<tailnet>.ts.net"'
    fi
  } > "$config/dawgd.toml"
  echo "wrote $config/dawgd.toml"
  [[ -z "$host" ]] && echo "note: set base_url in $config/dawgd.toml" >&2
fi

if [[ ! -e "$config/actions.toml" ]]; then
  cp "$repo/examples/actions.toml" "$config/actions.toml"
  echo "wrote $config/actions.toml"
fi

if [[ $service -eq 1 ]]; then
  unit="$HOME/.config/systemd/user/dawgd.service"
  mkdir -p "$(dirname "$unit")"
  sed -e "s|@REPO@|$repo|g" -e "s|@HOME@|$HOME|g" "$repo/systemd/dawgd.service.in" > "$unit"
  systemctl --user daemon-reload
  systemctl --user enable dawgd.service >/dev/null
  systemctl --user restart dawgd.service
  echo "dawgd: $(systemctl --user is-active dawgd.service)"
fi

echo
echo "To accept replies from your phone, expose dawgd on your tailnet once:"
echo "  tailscale serve --bg 8787"
base_url="$(sed -n 's/^base_url *= *"\(.*\)"/\1/p' "$config/dawgd.toml")"
if [[ -n "$base_url" && -s "$config/token" ]]; then
  echo
  echo "Inbox (bookmark it; the link is a secret): $base_url/m/$(cat "$config/token")"
fi
