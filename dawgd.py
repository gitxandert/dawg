#!/usr/bin/env python3
"""dawgd - reply to Bark notifications from your phone.

Subcommands:
  dawgd.py serve                     run the reply server (Tailscale-only)
  dawgd.py ask [opts] MESSAGE OPT..  send a request with reply options
  dawgd.py ask [opts] --questions F  send several questions behind one link

A request is a notification whose tap opens a page with one button per
option. A request with several questions shows them one at a time, then
a review step, and returns all the answers together. Options whose name
matches an action in actions.toml run that action's fixed command when
chosen; other options are just recorded so a waiting script can read them.

Files:
  ~/.config/dawg/actions.toml   documented action keys -> fixed commands
  ~/.config/dawg/dawgd.toml     server settings (bind, port, base_url)
  ~/.config/dawg/token          secret for the standing-actions page
  ~/.local/state/dawg/          pending requests and answers
"""

import argparse
import html
import json
import os
import secrets
import shutil
import subprocess
import sys
import threading
import time
import tomllib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs

CONFIG_DIR = Path.home() / ".config" / "dawg"
STATE_DIR = Path.home() / ".local" / "state" / "dawg"
ACTIONS_FILE = CONFIG_DIR / "actions.toml"
SETTINGS_FILE = CONFIG_DIR / "dawgd.toml"
TOKEN_FILE = CONFIG_DIR / "token"
DAWG = shutil.which("dawg") or str(Path(__file__).with_name("dawg"))

# Option recorded when the user sends their own message instead of a listed option.
MESSAGE = "message"

DEFAULT_SETTINGS = {
    "bind": "127.0.0.1",
    "port": 8787,
    "base_url": None,  # set in dawgd.toml; install.sh fills it in
    "max_output": 1500,
    "keep_days": 7,
}

answer_lock = threading.Lock()


def load_settings():
    settings = dict(DEFAULT_SETTINGS)
    if SETTINGS_FILE.exists():
        settings.update(tomllib.loads(SETTINGS_FILE.read_text()))
    return settings


def load_actions():
    """Read actions.toml fresh each time so edits apply without a restart."""
    if not ACTIONS_FILE.exists():
        return {}
    actions = tomllib.loads(ACTIONS_FILE.read_text()).get("action", {})
    for key, a in actions.items():
        if not isinstance(a.get("command"), list) or not a["command"]:
            raise ValueError(f"action {key!r}: command must be a non-empty list")
    return actions


def get_token():
    if not TOKEN_FILE.exists():
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        fd = os.open(TOKEN_FILE, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(secrets.token_urlsafe(24) + "\n")
    return TOKEN_FILE.read_text().strip()


def notify(title, body, url=None, group="dawgd"):
    cmd = [DAWG, "-t", title, "-g", group]
    if url:
        cmd += ["-u", url]
    try:
        return subprocess.run(cmd + [body], check=False, timeout=20).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def run_action(key, action, max_output):
    try:
        p = subprocess.run(
            action["command"],
            capture_output=True,
            text=True,
            timeout=action.get("timeout", 30),
            cwd=Path.home(),
        )
        out = (p.stdout + p.stderr).strip() or "(no output)"
        ok = p.returncode == 0
        status = "ok" if ok else f"exit {p.returncode}"
    except subprocess.TimeoutExpired:
        out, ok, status = "", False, "timed out"
    except OSError as e:
        out, ok, status = str(e), False, "failed to start"
    if len(out) > max_output:
        out = out[:max_output] + "\n…(truncated)"
    return {"action": key, "ok": ok, "status": status, "output": out}


# ---------------------------------------------------------------- requests

def req_path(rid):
    return STATE_DIR / f"{rid}.json"


def ans_path(rid):
    return STATE_DIR / f"{rid}.answer.json"


def draft_path(rid):
    return STATE_DIR / f"{rid}.draft.json"


def valid_id(rid):
    return rid.isalnum() and 16 <= len(rid) <= 64


def load_request(rid):
    if not valid_id(rid) or not req_path(rid).exists():
        return None
    req = json.loads(req_path(rid).read_text())
    if "questions" not in req:  # written before multi-question asks
        req["questions"] = [{"message": req.pop("message"), "options": req.pop("options")}]
    return req


def load_answer(rid):
    p = ans_path(rid)
    if not p.exists():
        return None
    ans = json.loads(p.read_text())
    if "answers" not in ans:  # written before multi-question asks
        ans = {"answers": [{"question": "", "option": ans["option"], "text": ans["text"],
                            "result": ans["result"]}], "answered_at": ans["answered_at"]}
    return ans


def load_draft(rid, n):
    """Answers so far and the question being shown; pos == n means review."""
    p = draft_path(rid)
    if p.exists():
        return json.loads(p.read_text())
    return {"pos": 0, "answers": [None] * n}


def write_json(path, data):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.replace(path)


def purge_old(keep_days):
    cutoff = time.time() - keep_days * 86400
    for p in STATE_DIR.glob("*.json"):
        if p.stat().st_mtime < cutoff:
            p.unlink(missing_ok=True)


# ---------------------------------------------------------------- web

PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title><style>
:root{{--bg:#f5f5f7;--card:#fff;--fg:#1d1d1f;--muted:#6e6e73;--accent:#0a66d8;--ok:#1a7f37;--bad:#c62828}}
@media (prefers-color-scheme:dark){{:root{{--bg:#000;--card:#1c1c1e;--fg:#f5f5f7;--muted:#98989d;--accent:#3d8bfd;--ok:#3fb950;--bad:#f85149}}}}
body{{margin:0;padding:16px;background:var(--bg);color:var(--fg);font:16px -apple-system,system-ui,sans-serif}}
.card{{background:var(--card);border-radius:14px;padding:16px;margin:0 auto 12px;max-width:520px}}
h1{{font-size:20px;margin:0 0 8px}} .muted{{color:var(--muted);font-size:14px}}
.msg{{white-space:pre-wrap;margin:8px 0 4px}}
button{{display:block;width:100%;margin:10px 0 0;padding:14px;border:0;border-radius:10px;
background:var(--accent);color:#fff;font-size:17px;font-weight:600}}
button small{{display:block;font-weight:400;opacity:.85;font-size:13px}}
button.alt{{background:var(--bg);color:var(--fg)}}
hr{{border:0;border-top:1px solid var(--bg);margin:12px 0}}
textarea{{width:100%;box-sizing:border-box;min-height:70px;margin-top:10px;padding:10px;border-radius:10px;
border:1px solid var(--muted);background:var(--bg);color:var(--fg);font:inherit}}
pre{{white-space:pre-wrap;word-break:break-word;background:var(--bg);padding:10px;border-radius:8px;font-size:13px}}
.ok{{color:var(--ok)}} .bad{{color:var(--bad)}}
</style></head><body>{body}</body></html>"""

e = html.escape


def fmt_time(ts):
    return time.strftime("%H:%M:%S", time.localtime(ts))


def option_buttons(options, actions, chosen=None):
    parts = []
    for opt in options:
        hint = ""
        if opt in actions:
            hint = f"<small>runs: {e(actions[opt].get('description') or ' '.join(actions[opt]['command']))}</small>"
        if opt == chosen:
            hint += "<small>your earlier answer</small>"
        parts.append(f'<button name="option" value="{e(opt)}">{e(opt)}{hint}</button>')
    return "".join(parts)


def result_block(result):
    if not result:
        return ""
    cls = "ok" if result["ok"] else "bad"
    return (f'<p class="{cls}">Action <b>{e(result["action"])}</b>: {e(result["status"])}</p>'
            f'<pre>{e(result["output"])}</pre>')


class Handler(BaseHTTPRequestHandler):
    server_version = "dawgd"

    def log_message(self, fmt, *args):
        # Don't log full paths: they contain request ids and the token.
        sys.stderr.write(f"{self.address_string()} {self.command} {self.path.split('/')[1:2]}\n")

    def send_page(self, title, body, code=200):
        data = PAGE.format(title=e(title), body=body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def not_found(self):
        self.send_page("Not found", '<div class="card"><h1>Not found</h1></div>', 404)

    def read_form(self):
        n = min(int(self.headers.get("Content-Length") or 0), 16384)
        form = parse_qs(self.rfile.read(n).decode(errors="replace"))
        return {k: v[0] for k, v in form.items()}

    def route(self):
        parts = [p for p in self.path.split("?")[0].split("/") if p]
        if len(parts) == 2 and parts[0] == "r":
            return "request", parts[1]
        if len(parts) == 2 and parts[0] == "a" and secrets.compare_digest(parts[1], self.server.token):
            return "standing", None
        return None, None

    def do_GET(self):
        kind, rid = self.route()
        if kind == "request":
            self.show_request(rid)
        elif kind == "standing":
            self.show_standing()
        else:
            self.not_found()

    def do_POST(self):
        kind, rid = self.route()
        form = self.read_form()
        if kind == "request":
            self.answer_request(rid, form)
        elif kind == "standing":
            self.run_standing(form)
        else:
            self.not_found()

    # -- one-off requests

    def show_request(self, rid, flash=""):
        req = load_request(rid)
        if not req:
            return self.not_found()
        actions = load_actions()
        ans = load_answer(rid)
        qs = req["questions"]
        multi = len(qs) > 1
        head = f'<h1>{e(req["title"])}</h1><div class="muted">sent {fmt_time(req["created"])}</div>'
        if ans:
            parts = []
            for q, a in zip(qs, ans["answers"]):
                parts.append(f'<div class="msg">{e(q["message"])}</div><p>Answered <b>{e(a["option"])}</b></p>'
                             + (f'<pre>{e(a["text"])}</pre>' if a.get("text") else "")
                             + result_block(a.get("result")))
            body = f'{head}{"<hr>".join(parts)}<p class="muted">Answered at {fmt_time(ans["answered_at"])}</p>'
        elif time.time() > req["expires"]:
            body = f'{head}<div class="msg">{e(qs[0]["message"])}</div><p class="bad">Expired at {fmt_time(req["expires"])}.</p>'
        else:
            draft = load_draft(rid, len(qs))
            pos = draft["pos"]
            back = '<button class="alt" name="back" value="1">Back</button>' if pos > 0 else ""
            if pos == len(qs):
                rows = "".join(f'<div class="msg">{e(q["message"])}</div><p><b>{e(a["option"])}</b></p>'
                               + (f'<pre>{e(a["text"])}</pre>' if a.get("text") else "")
                               for q, a in zip(qs, draft["answers"]))
                form = f'<button name="submit" value="1">Submit</button>{back}'
                body = f'{head}<p class="muted">Review your answers</p>{rows}<form method="post">{form}</form>'
            else:
                q, prev = qs[pos], draft["answers"][pos]
                text = send = ""
                if req.get("allow_text"):
                    hint = "Add a note, or write your own instruction" if q["options"] else "Message"
                    label = "Do this instead" if q["options"] else "Send"
                    text = (f'<textarea name="text" placeholder="{hint}">'
                            f'{e(prev["text"]) if prev else ""}</textarea>')
                    send = f'<button name="option" value="{MESSAGE}">{label}</button>'
                chosen = prev["option"] if prev else None
                step = f'<p class="muted">Question {pos + 1} of {len(qs)}</p>' if multi else ""
                form = (f'<input type="hidden" name="q" value="{pos}">{text}'
                        f'{option_buttons(q["options"], actions, chosen)}{send}{back}')
                body = f'{head}{step}<div class="msg">{e(q["message"])}</div><form method="post">{form}</form>'
            body += f'<p class="muted">Expires {fmt_time(req["expires"])}</p>'
        self.send_page(req["title"], flash + f'<div class="card">{body}</div>')

    def answer_request(self, rid, form):
        req = load_request(rid)
        if not req:
            return self.not_found()
        qs = req["questions"]
        with answer_lock:
            if load_answer(rid):
                return self.show_request(rid, '<div class="card bad">Already answered.</div>')
            if time.time() > req["expires"]:
                return self.show_request(rid)
            draft = load_draft(rid, len(qs))
            if form.get("back"):
                draft["pos"] = max(draft["pos"] - 1, 0)
                write_json(draft_path(rid), draft)
                return self.show_request(rid)
            if not form.get("submit"):
                if form.get("q") != str(draft["pos"]) or draft["pos"] >= len(qs):
                    return self.show_request(rid, '<div class="card bad">That page was out of date.</div>')
                q = qs[draft["pos"]]
                option = form.get("option", "")
                text = form.get("text", "").strip()
                if option == MESSAGE and req.get("allow_text"):
                    if not text:
                        return self.show_request(rid, '<div class="card bad">Write a message first.</div>')
                elif option not in q["options"]:
                    return self.show_request(rid, '<div class="card bad">Unknown option.</div>')
                draft["answers"][draft["pos"]] = {"question": q["message"], "option": option,
                                                  "text": text, "result": None}
                draft["pos"] += 1
                write_json(draft_path(rid), draft)
                # A single question needs no review step.
                if len(qs) > 1:
                    return self.show_request(rid)
            elif draft["pos"] != len(qs):
                return self.show_request(rid, '<div class="card bad">That page was out of date.</div>')
            ans = {"answers": draft["answers"], "answered_at": time.time()}
            write_json(ans_path(rid), ans)
            draft_path(rid).unlink(missing_ok=True)
        actions = load_actions()
        for a in ans["answers"]:
            option = a["option"]
            if option != MESSAGE and option in actions:
                a["result"] = run_action(option, actions[option], self.server.settings["max_output"])
                write_json(ans_path(rid), ans)
                r = a["result"]
                notify(f"{'✅' if r['ok'] else '❌'} {option}", r["output"])
        self.show_request(rid)

    # -- standing actions (bookmarkable)

    def show_standing(self, flash=""):
        actions = {k: a for k, a in load_actions().items() if a.get("standing")}
        if actions:
            form = f'<form method="post">{option_buttons(list(actions), actions)}</form>'
        else:
            form = '<p class="muted">No standing actions in actions.toml.</p>'
        self.send_page("Actions", flash + f'<div class="card"><h1>Actions</h1>{form}</div>')

    def run_standing(self, form):
        actions = load_actions()
        key = form.get("option", "")
        if key not in actions or not actions[key].get("standing"):
            return self.show_standing('<div class="card bad">Unknown action.</div>')
        r = run_action(key, actions[key], self.server.settings["max_output"])
        self.show_standing(f'<div class="card">{result_block(r)}</div>')


def serve(_args):
    settings = load_settings()
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    load_actions()  # fail fast on a bad config
    purge_old(settings["keep_days"])
    srv = ThreadingHTTPServer((settings["bind"], settings["port"]), Handler)
    srv.settings = settings
    srv.token = get_token()
    print(f"dawgd listening on {settings['bind']}:{settings['port']}", file=sys.stderr, flush=True)
    srv.serve_forever()


# ---------------------------------------------------------------- ask

def read_questions(args):
    if args.questions is None:
        if args.message is None:
            sys.exit("dawg ask: give a message, or --questions")
        return [{"message": args.message, "options": args.options}]
    if args.message is not None:
        sys.exit("dawg ask: give a message or --questions, not both")
    src = sys.stdin.read() if args.questions == "-" else Path(args.questions).read_text()
    try:
        qs = json.loads(src)
    except json.JSONDecodeError as err:
        sys.exit(f"dawg ask: --questions is not valid JSON: {err}")
    if not isinstance(qs, list) or not qs:
        sys.exit("dawg ask: --questions must be a non-empty JSON list")
    for q in qs:
        if (not isinstance(q, dict) or not isinstance(q.get("message"), str)
                or not all(isinstance(o, str) for o in q.setdefault("options", []))):
            sys.exit('dawg ask: each question needs "message" (string) and optional "options" (strings)')
    return [{"message": q["message"], "options": q["options"]} for q in qs]


def ask(args):
    settings = load_settings()
    if not settings["base_url"]:
        sys.exit(f"dawg ask: set base_url in {SETTINGS_FILE} (run install.sh)")
    actions = load_actions()
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    qs = read_questions(args)
    for q in qs:
        if not q["options"] and not args.text:
            sys.exit("dawg ask: give each question at least one option, or --text")
        if len(set(q["options"])) != len(q["options"]):
            sys.exit("dawg ask: duplicate options")
        if MESSAGE in q["options"]:
            sys.exit(f"dawg ask: {MESSAGE!r} is reserved for free-text replies")
    rid = secrets.token_hex(16)
    now = time.time()
    req = {"id": rid, "title": args.title, "questions": qs,
           "allow_text": args.text, "created": now, "expires": now + args.expires}
    write_json(req_path(rid), req)
    first = qs[0]
    if len(qs) > 1:
        body = f"{len(qs)} questions. 1: {first['message']}\nTap to answer them one at a time"
    else:
        body = f"{first['message']}\nTap to reply" + (": " + " / ".join(first["options"]) if first["options"] else "")
    runs = sorted({o for q in qs for o in q["options"] if o in actions})
    if runs:
        body += f"  (runs: {', '.join(runs)})"
    if not notify(args.title, body, url=f"{settings['base_url']}/r/{rid}", group=args.group):
        req_path(rid).unlink(missing_ok=True)
        print("dawg ask: notification not sent; nothing to wait for", file=sys.stderr)
        return 4
    if not args.wait:
        print(rid)
        return 0
    deadline = req["expires"]
    while time.time() < deadline:
        ans = load_answer(rid)
        if ans:
            # Give actions a moment to finish so --json can include their results.
            if any(a["option"] != MESSAGE and a["option"] in actions and a["result"] is None
                   for a in ans["answers"]):
                time.sleep(1)
                continue
            if len(qs) == 1:
                a = ans["answers"][0]
                if args.json:
                    print(json.dumps({"option": a["option"], "text": a["text"],
                                      "answered_at": ans["answered_at"], "result": a["result"]}))
                else:
                    print(a["option"])
                    if a.get("text"):
                        print(a["text"])
            elif args.json:
                print(json.dumps(ans))
            else:
                print("\n\n".join(a["option"] + (f"\n{a['text']}" if a.get("text") else "")
                                   for a in ans["answers"]))
            return 0
        time.sleep(1)
    print("dawg ask: expired without an answer", file=sys.stderr)
    return 3


def main():
    p = argparse.ArgumentParser(prog="dawgd")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("serve").set_defaults(func=serve)
    a = sub.add_parser("ask", prog="dawg ask",
                       description="Send a notification with reply options.")
    a.add_argument("-t", "--title", default=os.uname().nodename)
    a.add_argument("-g", "--group", default="dawgd")
    a.add_argument("-e", "--expires", type=int, default=3600, help="seconds (default 3600)")
    a.add_argument("-w", "--wait", action="store_true", help="block until answered; print the option")
    a.add_argument("--json", action="store_true", help="with --wait, print the full answer as JSON")
    a.add_argument("--text", action="store_true",
                   help=f"allow a typed reply: a note with an option, or option {MESSAGE!r} on its own")
    a.add_argument("--questions", metavar="FILE",
                   help='several questions in one ask, answered one at a time: a JSON list of '
                        '{"message": ..., "options": [...]} ("-" reads stdin)')
    a.add_argument("message", nargs="?")
    a.add_argument("options", nargs="*")
    a.set_defaults(func=ask)
    args = p.parse_args()
    sys.exit(args.func(args) or 0)


if __name__ == "__main__":
    main()
