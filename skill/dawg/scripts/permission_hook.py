#!/usr/bin/env python3
"""Claude Code PermissionRequest hook: route permission prompts to the dawg app.

Only acts when DAWG_THREAD is set (agent started from the dawg inbox).
Anything else, or any failure, prints nothing so Claude falls back to its
normal terminal prompt.
"""
import json, os, subprocess, sys

thread = os.environ.get("DAWG_THREAD")
if not thread:
    sys.exit(0)
try:
    ev = json.load(sys.stdin)
    tool = ev.get("tool_name", "?")
    desc = json.dumps(ev.get("tool_input", {}))[:600]
    r = subprocess.run(
        ["dawg", "ask", "-T", thread, "-t", "permission", "-g", "claude", "--text",
         "-w", "--json", "-e", "900", f"Allow {tool}? {desc}", "Allow", "Deny"],
        capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit(0)
    ans = json.loads(r.stdout)
except Exception:
    sys.exit(0)

if ans.get("option") == "Allow":
    decision = {"behavior": "allow"}
else:
    decision = {"behavior": "deny",
                "message": ans.get("text") or "Denied by user from the dawg app"}
print(json.dumps({"hookSpecificOutput": {
    "hookEventName": "PermissionRequest", "decision": decision}}))
