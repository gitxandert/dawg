#!/usr/bin/env python3
"""dawgd - reply to Bark notifications from your phone.

Subcommands:
  dawgd.py serve                     run the reply server (Tailscale-only)
  dawgd.py ask [opts] MESSAGE OPT..  send a request with reply options
  dawgd.py ask [opts] --questions F  send several questions behind one link
  dawgd.py send -T ID [opts] MESSAGE post to a thread, and text it unless muted
  dawgd.py status -T ID STATUS [NOTE] set a thread's status: busy, blocked, idle
  dawgd.py inbox -T ID [-w]          print messages queued for a thread
  dawgd.py thread new -t TITLE       start a thread; prints its id
  dawgd.py statusline                Claude Code status line: reports model and usage

A request is a notification whose tap opens a page with one button per
option. A request with several questions shows them one at a time, then
a review step, and returns all the answers together. Options whose name
matches an action in actions.toml run that action's fixed command when
chosen; other options are just recorded so a waiting script can read them.

A thread is one agent's conversation. The inbox page (/m/<token>) lists
every thread with its status, shows its messages and asks, answers asks
inline, queues messages for the agent, mutes a thread's texts, and starts
new agents in herdr.

Files:
  ~/.config/dawg/actions.toml   documented action keys -> fixed commands
  ~/.config/dawg/dawgd.toml     server settings (bind, port, base_url)
  ~/.config/dawg/token          secret for the standing-actions page
  ~/.local/state/dawg/          pending requests and answers
  ~/.local/state/dawg/threads/  thread metadata and message logs
"""

import argparse
import contextlib
import fcntl
import hashlib
import hmac
import html
import json
import os
import re
import secrets
import shlex
import shutil
import subprocess
import sys
import threading
import time
import tomllib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

CONFIG_DIR = Path.home() / ".config" / "dawg"
STATE_DIR = Path.home() / ".local" / "state" / "dawg"
ACTIONS_FILE = CONFIG_DIR / "actions.toml"
SETTINGS_FILE = CONFIG_DIR / "dawgd.toml"
TOKEN_FILE = CONFIG_DIR / "token"
THREADS_DIR = STATE_DIR / "threads"
USAGE_FILE = STATE_DIR / "usage.json"
INBOX_PAGE = Path(__file__).with_name("inbox.html")
ICON = Path(__file__).with_name("icon.png")
DAWG = shutil.which("dawg") or str(Path(__file__).with_name("dawg"))
HERDR = shutil.which("herdr")

STATUSES = ("busy", "blocked", "idle")
# Texts sent without a thread land here.
NOTIFICATIONS = "notifications"
COOKIE = "dawg_inbox"
# Agents the inbox can start in herdr, and the keys it can press for them.
AGENT_KINDS = ("claude", "codex")
HERDR_KEYS = ("enter", "esc", "up", "down", "tab", "ctrl+c", "1", "2", "3", "y", "n")

# Option recorded when the user sends their own message instead of a listed option.
MESSAGE = "message"

DEFAULT_SETTINGS = {
    "bind": "127.0.0.1",
    "port": 8787,
    "base_url": None,  # set in dawgd.toml; install.sh fills it in
    "max_output": 1500,
    "keep_days": 7,
    "thread_keep_days": 30,
    "herdr_session": None,  # run herdr against a named session instead of the default
    # What tapping a text opens. Bark hands every link to the browser as a new
    # tab, so by default a tap does nothing and the inbox stays the one tab.
    "tap": "none",  # or "questions": asks link to the inbox; "all": every text does
    "usage_alerts": [80, 95],  # text when a 5-hour or weekly limit passes these percentages
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


def notify(title, body, url=None, group="dawgd", level=None, sound=None):
    """Send a text. Without a url, tapping it does nothing (Bark's action=none)."""
    cmd = [DAWG, "-t", title, "-g", group]
    for flag, value in (("-u", url), ("-a", None if url else "none"), ("-l", level), ("-s", sound)):
        if value:
            cmd += [flag, value]
    # DAWG_DIRECT: dawg sends the text instead of posting it to a thread again.
    env = {**os.environ, "DAWG_DIRECT": "1"}
    try:
        return subprocess.run(cmd + ["--", body], check=False, timeout=20, env=env).returncode == 0
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


def purge_old(keep_days, thread_keep_days):
    cutoff = time.time() - keep_days * 86400
    for p in STATE_DIR.glob("*.json"):
        if p.stat().st_mtime < cutoff:
            p.unlink(missing_ok=True)
    cutoff = time.time() - thread_keep_days * 86400
    for p in THREADS_DIR.glob("*.json"):
        if p.stat().st_mtime < cutoff:
            for suffix in (".json", ".log.jsonl", ".lock"):
                p.with_name(p.stem + suffix).unlink(missing_ok=True)


def ask_open(rid):
    req = load_request(rid)
    return bool(req) and not ans_path(rid).exists() and time.time() < req["expires"]


def record_answers(rid, req, answers):
    """Save a request's answers. The caller holds answer_lock and checked it's open."""
    ans = {"answers": answers, "answered_at": time.time()}
    write_json(ans_path(rid), ans)
    draft_path(rid).unlink(missing_ok=True)
    tid = req.get("thread")
    if tid and load_thread(tid):
        def answered(meta):
            meta["open_asks"] = [r for r in meta["open_asks"] if r != rid]
            meta["status"] = "busy"  # the agent has its answer and is back at work
        post(tid, {"kind": "answer", "from": "user", "rid": rid, "answers": answers}, answered)
    return ans


def run_answer_actions(rid, req, ans, max_output):
    actions = load_actions()
    for a in ans["answers"]:
        option = a["option"]
        if option != MESSAGE and option in actions:
            a["result"] = run_action(option, actions[option], max_output)
            write_json(ans_path(rid), ans)
            r = a["result"]
            meta = load_thread(req.get("thread", ""))
            if meta:
                post(meta["id"], {"kind": "result", "from": "system", "rid": rid, "result": r})
            notify(f"{'✅' if r['ok'] else '❌'} {option}", r["output"],
                   url=tap_link(load_settings(), meta) if meta else None)


def check_answers(req, answers):
    """Validate inbox answers [{option, text}] against req; return them or an error string."""
    qs = req["questions"]
    if not isinstance(answers, list) or len(answers) != len(qs):
        return "Answer every question."
    out = []
    for q, a in zip(qs, answers):
        option = str(a.get("option") or "")
        text = str(a.get("text") or "").strip() if req.get("allow_text") else ""
        if option == MESSAGE and req.get("allow_text"):
            if not text:
                return "Write a message first."
        elif option not in q["options"]:
            return "Pick an option for every question."
        out.append({"question": q["message"], "option": option, "text": text, "result": None})
    return out


# ---------------------------------------------------------------- threads
#
# A thread is one agent session's conversation: a metadata file and an
# append-only log, written by both dawgd and the CLI, so every change takes
# the thread's file lock. Log entries carry a sequence number; messages the
# user queues are delivered when the agent runs `dawg inbox`.

def thread_path(tid):
    return THREADS_DIR / f"{tid}.json"


def log_path(tid):
    return THREADS_DIR / f"{tid}.log.jsonl"


def valid_thread(tid):
    return bool(tid) and re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", tid) is not None


@contextlib.contextmanager
def thread_lock(tid):
    THREADS_DIR.mkdir(parents=True, exist_ok=True)
    with open(THREADS_DIR / f"{tid}.lock", "a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        yield


def load_thread(tid):
    if not valid_thread(tid) or not thread_path(tid).exists():
        return None
    return json.loads(thread_path(tid).read_text())


def new_thread(title, cwd=None, tid=None, herdr=None):
    tid = tid or secrets.token_hex(6)
    now = time.time()
    meta = {"id": tid, "title": title, "cwd": cwd, "created": now, "updated": now,
            "status": "idle", "status_note": "", "muted": False, "archived": False,
            "seq": 0, "delivered": 0, "unread": 0, "preview": "", "open_asks": [],
            "herdr": herdr, "key": secrets.token_urlsafe(18)}
    with thread_lock(tid):
        if not thread_path(tid).exists():
            write_json(thread_path(tid), meta)
    return load_thread(tid)


def thread_link(settings, meta, rid=None):
    """The link a text opens: this thread in the inbox, at question rid if given.

    It carries the thread's own key, never the inbox token: a browser that
    has opened the inbox bookmark is sent on to the full inbox, any other
    sees just this thread.
    """
    if not settings["base_url"]:
        return None
    if "key" not in meta:  # threads from before thread links
        meta = update_thread(meta["id"], lambda m: m.setdefault("key", secrets.token_urlsafe(18)))
    return f"{settings['base_url']}/i/{meta['key']}#t={meta['id']}" + (f"&r={rid}" if rid else "")


def tap_link(settings, meta, rid=None):
    """The link for a text, if the tap setting gives this kind of text one."""
    if settings["tap"] == "all" or (settings["tap"] == "questions" and rid):
        return thread_link(settings, meta, rid)
    return None


def thread_by_key(key):
    if not re.fullmatch(r"[A-Za-z0-9_-]{16,64}", key):
        return None
    for p in THREADS_DIR.glob("*.json"):
        meta = json.loads(p.read_text())
        if secrets.compare_digest(meta.get("key", ""), key):
            return meta
    return None


def update_thread(tid, change):
    """Apply change(meta) to the thread's metadata under its lock."""
    with thread_lock(tid):
        meta = json.loads(thread_path(tid).read_text())
        change(meta)
        write_json(thread_path(tid), meta)
    return meta


def preview(entry):
    kind = entry["kind"]
    if kind == "text":
        text = entry["body"]
    elif kind == "ask":
        text = entry["questions"][0]["message"]
    elif kind == "answer":
        a = entry["answers"][0]
        text = a["text"] if a["option"] == MESSAGE else a["option"]
    else:
        text = entry.get("body", "")
    who = "You: " if entry["from"] == "user" else ""
    return (who + " ".join(text.split()))[:140]


def post(tid, entry, change=None):
    """Append entry to the thread's log, applying change(meta) in the same step."""
    with thread_lock(tid):
        meta = json.loads(thread_path(tid).read_text())
        meta["seq"] += 1
        entry = {"seq": meta["seq"], "ts": time.time(), **entry}
        meta["updated"] = entry["ts"]
        if entry["kind"] not in ("result", "status"):  # the list shows the status already
            meta["preview"] = preview(entry)
        if entry["from"] == "agent":
            meta["archived"] = False
            if entry["kind"] in ("text", "ask"):
                meta["unread"] += 1
        if change:
            change(meta)
        with open(log_path(tid), "a") as f:
            f.write(json.dumps(entry) + "\n")
        write_json(thread_path(tid), meta)
    return entry


def read_log(tid, after=0):
    if not log_path(tid).exists():
        return []
    entries = (json.loads(line) for line in log_path(tid).read_text().splitlines() if line)
    return [x for x in entries if x["seq"] > after]


def thread_status(meta):
    if any(ask_open(rid) for rid in meta["open_asks"]):
        return "blocked"
    return meta["status"]


def take_queued(tid):
    """Return the user's messages the agent hasn't seen, and mark them delivered."""
    with thread_lock(tid):
        meta = json.loads(thread_path(tid).read_text())
        msgs = [x for x in read_log(tid, meta["delivered"])
                if x["from"] == "user" and x["kind"] == "text"]
        meta["delivered"] = meta["seq"]
        write_json(thread_path(tid), meta)
    return msgs


def queued_count(tid):
    meta = load_thread(tid)
    return sum(1 for x in read_log(tid, meta["delivered"])
               if x["from"] == "user" and x["kind"] == "text")



# ---------------------------------------------------------------- usage
#
# Usage is grouped by service, the provider an agent's model comes from.
# Claude Code reports through `dawg statusline`; Codex writes the same facts
# to its session log, which dawgd reads. A service's limits are account-wide,
# so the inbox shows one set per service. Each report lands on the thread of
# the agent it came from: the one named by DAWG_THREAD, or the thread whose
# dawg commands ran under the same agent process.

CODEX_SESSIONS = Path.home() / ".codex" / "sessions"
# Model and effort choices per agent kind; "" keeps the user's default.
# Codex takes any model name; the inbox suggests ones it has seen.
CHOICES = {
    "claude": {"models": ["fable", "opus", "sonnet", "haiku"],
               "efforts": ["low", "medium", "high", "xhigh", "max"]},
    "codex": {"models": [], "efforts": ["minimal", "low", "medium", "high", "xhigh"]},
}
SERVICE_NAMES = {"claude": "Claude", "codex": "Codex", "ollama": "Ollama (local)",
                 "lmstudio": "LM Studio (local)", "other": "Other"}


def agent_process():
    """(kind, 'pid:starttime') of the nearest Claude Code or Codex ancestor, or (None, None)."""
    pid = os.getppid()
    for _ in range(16):
        if pid <= 1:
            break
        try:
            comm = Path(f"/proc/{pid}/comm").read_text().strip()
            fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        except OSError:
            break
        if comm in AGENT_KINDS:
            return comm, f"{pid}:{fields[19]}"
        pid = int(fields[1])
    return None, None


def proc_alive(proc):
    pid, start = proc.split(":")
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19] == start
    except (OSError, IndexError):
        return False


def thread_agent(meta):
    """{"kind", "proc"} for the agent behind a thread, as far as it's known."""
    agent = dict(meta.get("agent") or {})
    if meta.get("claude") and not agent:  # threads from before Codex support
        agent = {"kind": "claude", "proc": meta["claude"]}
    agent.setdefault("kind", (meta.get("herdr") or {}).get("kind"))
    return agent


def thread_service(meta):
    return (meta.get("usage") or {}).get("service") or thread_agent(meta).get("kind") or "other"


def thread_for_process(proc):
    if not proc:
        return None
    for p in THREADS_DIR.glob("*.json"):
        meta = json.loads(p.read_text())
        if thread_agent(meta).get("proc") == proc:
            return meta
    return None


def load_usage():
    try:
        return json.loads(USAGE_FILE.read_text())
    except (OSError, ValueError):
        return {}


def window_label(minutes):
    return {300: "5 hours", 10080: "Week"}.get(minutes) or (
        f"{minutes // 1440} days" if minutes % 1440 == 0 else f"{minutes / 60:g} hours")


def claude_windows(limits):
    out = []
    for key, label in (("five_hour", "5 hours"), ("seven_day", "Week"), ("spend_limit", "Spend")):
        w = limits.get(key)
        if isinstance(w, dict) and w.get("used_percentage") is not None:
            out.append({"key": key, "label": label, "used": w["used_percentage"],
                        "resets_at": w.get("resets_at")})
    return out


def codex_windows(limits):
    out = []
    for key in ("primary", "secondary"):
        w = limits.get(key)
        if isinstance(w, dict) and w.get("used_percent") is not None:
            out.append({"key": key, "label": window_label(w.get("window_minutes") or 0),
                        "used": w["used_percent"], "resets_at": w.get("resets_at")})
    return out


def check_alerts(usage, service, windows, settings):
    """Alert texts for windows that passed their next level, once per window period."""
    alerts = []
    alerted = usage.setdefault("alerted", {})
    name = SERVICE_NAMES.get(service, service)
    for w in windows:
        key = f"{service}:{w['key']}"
        prev = alerted.get(key, {})
        level = prev.get("level", 0) if prev.get("resets_at") == w["resets_at"] else 0
        passed = max((t for t in settings["usage_alerts"] if w["used"] >= t), default=0)
        if passed > level:
            when = (time.strftime("%a %H:%M", time.localtime(w["resets_at"]))
                    if w["resets_at"] else "later")
            span = {"5 hours": "5-hour", "Week": "weekly"}.get(w["label"], w["label"].lower())
            alerts.append(f"{name} {span} limit at {w['used']:.0f}% (resets {when})")
        alerted[key] = {"resets_at": w["resets_at"], "level": max(passed, level)}
    return alerts


def save_service_usage(service, windows, settings):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with open(STATE_DIR / "usage.lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        usage = load_usage()
        usage.pop("limits", None)  # the single-service layout from before
        alerts = check_alerts(usage, service, windows, settings)
        usage.setdefault("services", {})[service] = {"windows": windows, "at": time.time()}
        write_json(USAGE_FILE, usage)
    for text in alerts:
        # In the background: the status line shouldn't wait on the push server.
        subprocess.Popen([sys.executable, __file__, "send", "-T", NOTIFICATIONS, "-t", "dawg usage",
                          "-l", "timeSensitive", text], start_new_session=True,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def set_thread_usage(meta, report):
    old = {k: v for k, v in (meta.get("usage") or {}).items() if k != "at"}
    if old != report or time.time() - (meta.get("usage") or {}).get("at", 0) > 60:
        update_thread(meta["id"], lambda m: m.update(usage={**report, "at": time.time()}))


def report_usage(data, settings):
    """Record a Claude Code status line report."""
    tid = os.environ.get("DAWG_THREAD")
    meta = load_thread(tid) if tid else thread_for_process(agent_process()[1])
    if meta:
        model = data.get("model") or {}
        set_thread_usage(meta, {
            "service": "claude", "kind": "claude",
            "model": model.get("display_name"), "model_id": model.get("id"),
            "effort": (data.get("effort") or {}).get("level"),
            "context_pct": (data.get("context_window") or {}).get("used_percentage"),
            "cost_usd": (data.get("cost") or {}).get("total_cost_usd"),
            "session_id": data.get("session_id")})
    windows = claude_windows(data.get("rate_limits") or {})
    if windows:
        save_service_usage("claude", windows, settings)


def status_text(data):
    parts = [(data.get("model") or {}).get("display_name"), (data.get("effort") or {}).get("level")]
    ctx = (data.get("context_window") or {}).get("used_percentage")
    if ctx is not None:
        parts.append(f"ctx {ctx:.0f}%")
    for w in claude_windows(data.get("rate_limits") or {}):
        if w["key"] != "spend_limit":
            parts.append(f"{'5h' if w['key'] == 'five_hour' else '7d'} {w['used']:.0f}%")
    return " · ".join(p for p in parts if p)


def codex_rollout(proc):
    """The session log a running Codex process is writing."""
    found = []
    try:
        for fd in Path(f"/proc/{proc.split(':')[0]}/fd").iterdir():
            target = os.readlink(fd)
            if target.startswith(str(CODEX_SESSIONS.resolve())) and Path(target).name.startswith("rollout-"):
                found.append(Path(target))
    except OSError:
        pass
    return max(found, key=lambda f: f.stat().st_mtime, default=None)


def read_codex(path):
    """Session id, provider, model, effort, context and limits from a Codex session log."""
    out = {}
    with open(path, "rb") as f:
        head = json.loads(f.readline() or b"{}").get("payload") or {}
        out.update(session_id=head.get("id"), provider=head.get("model_provider"))
        f.seek(0, 2)
        f.seek(max(0, f.tell() - 512 * 1024))
        lines = f.read().decode(errors="replace").splitlines()
    for line in reversed(lines):
        try:
            e = json.loads(line)
        except ValueError:
            continue
        p = e.get("payload") or {}
        if e.get("type") == "turn_context" and "model" not in out:
            mode = (p.get("collaboration_mode") or {}).get("settings") or {}
            out["model"] = p.get("model") or mode.get("model")
            out["effort"] = p.get("effort") or mode.get("reasoning_effort")
        if p.get("type") == "token_count" and "tokens" not in out:
            info = p.get("info") or {}
            last, window = info.get("last_token_usage") or {}, info.get("model_context_window")
            out["tokens"] = True
            if window and last.get("input_tokens") is not None:
                out["context_pct"] = round(100 * last["input_tokens"] / window)
            if isinstance(p.get("rate_limits"), dict):
                out["limits"] = p["rate_limits"]
        if "model" in out and "tokens" in out:
            break
    return out


codex_seen = {}


def poll_codex(settings):
    """Read the session logs of running Codex agents into their threads."""
    for p in THREADS_DIR.glob("*.json"):
        meta = json.loads(p.read_text())
        agent = thread_agent(meta)
        if agent.get("kind") != "codex" or not agent.get("proc") or not proc_alive(agent["proc"]):
            continue
        path = codex_rollout(agent["proc"])
        if not path or codex_seen.get(meta["id"]) == (path, path.stat().st_mtime):
            continue
        codex_seen[meta["id"]] = (path, path.stat().st_mtime)
        info = read_codex(path)
        provider = info.get("provider") or "openai"
        service = "codex" if provider == "openai" else provider
        set_thread_usage(meta, {"service": service, "kind": "codex", "provider": provider,
                                "model": info.get("model"), "model_id": info.get("model"),
                                "effort": info.get("effort"), "context_pct": info.get("context_pct"),
                                "cost_usd": None, "session_id": info.get("session_id")})
        windows = codex_windows(info.get("limits") or {})
        if windows and service == "codex":
            save_service_usage("codex", windows, settings)


def poll_loop(settings):
    while True:
        try:
            poll_codex(settings)
        except Exception as err:  # keep polling; a log can be mid-write
            print(f"dawgd: reading Codex usage: {err}", file=sys.stderr, flush=True)
        time.sleep(10)


def model_suggestions():
    """Models the inbox offers per agent kind: the fixed Claude aliases, and Codex models seen."""
    seen = {m for p in THREADS_DIR.glob("*.json")
            if (u := json.loads(p.read_text()).get("usage") or {}).get("kind") == "codex"
            and (m := u.get("model"))}
    try:
        seen.add(tomllib.loads((Path.home() / ".codex" / "config.toml").read_text())["model"])
    except (OSError, KeyError, tomllib.TOMLDecodeError):
        pass
    return {"claude": CHOICES["claude"],
            "codex": {"models": sorted(seen), "efforts": CHOICES["codex"]["efforts"]}}


def check_choice(kind, model, effort):
    """An error message if model or effort doesn't suit the agent kind."""
    if kind not in CHOICES:
        return "Only Claude and Codex agents have model settings."
    if effort and effort not in CHOICES[kind]["efforts"]:
        return "Unknown effort."
    if model and (model not in CHOICES[kind]["models"] if kind == "claude"
                  else not re.fullmatch(r"[A-Za-z0-9._:/-]{1,64}", model)):
        return "Unknown model."
    return None


def agent_flags(kind, model, effort):
    if kind == "claude":
        return (["--model", model] if model else []) + (["--effort", effort] if effort else [])
    return (["-m", model] if model else []) + \
           (["-c", f'model_reasoning_effort="{effort}"'] if effort else [])


# ---------------------------------------------------------------- herdr

class HerdrError(Exception):
    def __init__(self, message, code=None):
        super().__init__(message)
        self.code = code


herdr_cache = {"at": 0.0, "agents": {}}
herdr_cache_lock = threading.Lock()


def herdr(settings, *args, timeout=30):
    """Run a herdr command and return its parsed JSON, or plain text output."""
    if not HERDR:
        raise HerdrError("herdr is not installed")
    session = ["--session", settings["herdr_session"]] if settings["herdr_session"] else []
    try:
        p = subprocess.run([HERDR, *session, *args], capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise HerdrError(f"herdr {args[0]} {args[1]} timed out")
    if p.returncode != 0:
        try:
            error = json.loads(p.stderr or p.stdout)["error"]
            raise HerdrError(error["message"], error.get("code"))
        except (ValueError, KeyError, TypeError):
            raise HerdrError((p.stderr or p.stdout).strip() or f"exit {p.returncode}")
    try:
        return json.loads(p.stdout)
    except ValueError:
        return p.stdout


def herdr_agents(settings):
    """Live herdr agents by name, refreshed at most every few seconds."""
    with herdr_cache_lock:
        if time.time() - herdr_cache["at"] > 3:
            try:
                out = herdr(settings, "agent", "list", timeout=5)
                agents = out["result"]["agents"]
                herdr_cache["agents"] = {**{a["pane_id"]: a for a in agents if a.get("pane_id")},
                                         **{a["name"]: a for a in agents if a.get("name")}}
            except (HerdrError, KeyError, TypeError):
                herdr_cache["agents"] = {}
            herdr_cache["at"] = time.time()
        return herdr_cache["agents"]


def wait_ready(settings, pane, seconds, what):
    """Wait until the agent in pane sits idle at its prompt."""
    deadline = time.time() + seconds
    while not any(a.get("pane_id") == pane and a.get("agent_status") in ("idle", "done")
                  for a in herdr(settings, "agent", "list", timeout=5)["result"]["agents"]):
        if time.time() > deadline:
            raise HerdrError(f"{what} didn't become ready; check the terminal")
        time.sleep(1)


def herdr_target(meta):
    """How herdr commands address this thread's agent: its pane, which outlives restarts."""
    h = meta["herdr"]
    return h.get("pane") or h["agent"]


def stop_agent(settings, meta):
    """Exit the thread's herdr agent and close its open asks."""
    kind, pane = thread_agent(meta)["kind"], meta["herdr"]["pane"]
    herdr(settings, "agent", "prompt", herdr_target(meta),
          "/exit" if kind == "claude" else "/quit", timeout=15)
    deadline = time.time() + 30
    while live := [a for a in herdr(settings, "agent", "list", timeout=5)["result"]["agents"]
                   if a.get("pane_id") == pane]:
        if time.time() > deadline:
            raise HerdrError("the agent didn't exit")
        # Claude Code asks before stopping background work, with "Exit and stop tasks" first.
        if live[0]["agent_status"] == "blocked" and "Exit and stop tasks" in str(
                herdr(settings, "agent", "read", pane, "--source", "visible", timeout=10)):
            herdr(settings, "agent", "send-keys", pane, "enter", timeout=10)
        time.sleep(1)
    # Its background commands died with it, so nobody would read answers to open asks.
    with answer_lock:
        for rid in meta["open_asks"]:
            if (req := load_request(rid)) and ask_open(rid):
                write_json(req_path(rid), {**req, "expires": time.time()})
    update_thread(meta["id"], lambda m: m.update(open_asks=[]))


def launch_agent(settings, meta, cmd, prompt):
    """Run cmd in the thread's pane once its agent has exited, then give it prompt."""
    tid, kind = meta["id"], thread_agent(meta)["kind"]
    pane, name = meta["herdr"]["pane"], meta["herdr"].get("agent") or f"dawg-{tid}"
    usage = meta.get("usage") or {}
    if kind == "codex" and usage.get("provider") not in (None, "openai"):
        cmd = [*cmd, "-c", f'model_provider="{usage["provider"]}"']
    herdr(settings, "pane", "run", pane, f"DAWG_THREAD={tid} " + shlex.join(cmd))
    wait_ready(settings, pane, 60, kind)
    herdr(settings, "agent", "rename", pane, name, timeout=10)
    update_thread(tid, lambda m: m["herdr"].update(agent=name))
    herdr(settings, "agent", "prompt", pane, prompt, timeout=15)


def restart_agent(settings, tid, model, effort):
    """Restart an agent on the same conversation with new model settings.

    Claude Code's /model and /effort would also change the user's defaults
    for new sessions; flags on a resumed session change only this one.
    """
    meta = load_thread(tid)
    kind, sid = thread_agent(meta)["kind"], meta["usage"]["session_id"]

    def note(body):
        post(tid, {"kind": "system", "from": "system", "body": body})

    label = " · ".join(x for x in (model or "default model", effort and f"{effort} effort") if x)
    note(f"Restarting on {label}…")
    try:
        stop_agent(settings, meta)
        cmd = ["claude", "--resume", sid] if kind == "claude" else ["codex", "resume", sid]
        launch_agent(settings, meta, cmd + agent_flags(kind, model, effort),
                     f"dawg inbox: this session was restarted to switch to {label}. Background "
                     f"commands such as a pending dawg ask or dawg inbox -w were stopped, and open "
                     f"questions in your thread ({tid}) were closed. Carry on with your task, and "
                     f"re-send anything you still need answered.")
        note(f"Restarted on {label}.")
    except (HerdrError, KeyError, TypeError) as err:
        note(f"Couldn't restart the agent: {err}")


def clear_agent(settings, tid):
    """Start the thread's agent over with an empty context, as /clear would.

    It exits and starts a new session instead of typing /clear, because
    exiting also stops its background commands (a pending dawg ask or
    dawg inbox -w), which would otherwise report into the new conversation.
    The thread keeps its messages, its id and the agent's model settings.
    """
    meta = load_thread(tid)
    kind, usage = thread_agent(meta)["kind"], meta.get("usage") or {}
    model = usage.get("model_id") if kind == "claude" else usage.get("model")
    effort = usage.get("effort") or ""

    post(tid, {"kind": "system", "from": "system", "body": "Clearing the agent's context…"})
    try:
        stop_agent(settings, meta)
        # Messages queued for the old conversation aren't the new one's to act on.
        kept = {k: usage[k] for k in ("service", "kind", "model", "model_id", "effort", "provider")
                if k in usage}
        post(tid, {"kind": "clear", "from": "system", "body": "Context cleared"},
             lambda m: m.update(delivered=m["seq"], status="idle",
                                usage={**kept, "context_pct": 0, "at": time.time()}))
        launch_agent(settings, meta, [kind, *agent_flags(kind, model or "", effort)],
                     agent_prompt(kind, tid))
    except (HerdrError, KeyError, TypeError) as err:
        post(tid, {"kind": "system", "from": "system", "body": f"Couldn't clear the agent: {err}"})


def agent_prompt(kind, tid, task=None):
    """The first prompt for an agent started from the inbox; no task means it was cleared."""
    if task:
        intro = (f"You were started from the dawg inbox. Your dawg thread is {tid} "
                 f"(DAWG_THREAD is set). Go straight into away mode.\n\nTask: {task}")
    else:
        intro = (f"Your context was cleared from the dawg inbox. Your dawg thread is {tid} "
                 f"(DAWG_THREAD is set); its earlier messages belong to the old conversation. "
                 f"Go straight into away mode and ask what to work on.")
    if kind == "claude":
        return f"/dawg {intro}"
    return f"Use the dawg skill ($dawg). {intro}"


def start_agent(settings, tid, cwd, kind, task, model="", effort=""):
    """Open a herdr workspace for the thread and start the agent in it."""
    meta = load_thread(tid)
    name = meta["herdr"]["agent"]

    def note(body):
        post(tid, {"kind": "system", "from": "system", "body": body})

    try:
        out = herdr(settings, "workspace", "create", "--cwd", cwd, "--label", meta["title"],
                    "--env", f"DAWG_THREAD={tid}", "--no-focus")["result"]
        wid, pane = out["workspace"]["workspace_id"], out["root_pane"]["pane_id"]

        def placed(m):
            m["herdr"].update(workspace=wid, pane=pane)
        update_thread(tid, placed)
        flags = agent_flags(kind, model, effort)
        try:
            herdr(settings, "agent", "start", name, "--kind", kind, "--pane", pane,
                  "--timeout", "120000", *(["--", *flags] if flags else []), timeout=150)
        except HerdrError as err:
            if err.code != "agent_not_ready":
                raise
            note("The agent is waiting at a prompt in its terminal, such as whether to trust "
                 "this folder. Open Terminal to answer it; the task goes in once it's ready.")
            wait_ready(settings, pane, 900, kind)
        herdr(settings, "agent", "prompt", pane, agent_prompt(kind, tid, task))
        note(f"Started {kind} as {name} in herdr workspace {wid}.")
    except (HerdrError, KeyError, TypeError) as err:
        note(f"Couldn't start the agent: {err}")
        update_thread(tid, lambda m: m.update(status="idle"))

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
        """Return (kind, arg, scope); scope is the one thread a thread link may see."""
        parts = [p for p in urlsplit(self.path).path.split("/") if p]
        if len(parts) == 2 and parts[0] == "r":
            return "request", parts[1], None
        if parts == ["icon.png"]:  # the home-screen icon; nothing secret
            return "icon", None, None
        if parts[:1] == ["i"] and len(parts) in (2, 4) and (meta := thread_by_key(parts[1])):
            if len(parts) == 2:
                return "link", None, meta["id"]
            if parts[2] == "api":
                return "api", parts[3], meta["id"]
        authed = len(parts) >= 2 and secrets.compare_digest(parts[1], self.server.token)
        if len(parts) == 2 and parts[0] == "a" and authed:
            return "standing", None, None
        if parts[:1] == ["m"] and authed:
            if len(parts) == 2:
                return "inbox", None, None
            if len(parts) == 4 and parts[2] == "api":
                return "api", parts[3], None
        return None, None, None

    def session_cookie(self):
        return hmac.new(self.server.token.encode(), b"inbox session", hashlib.sha256).hexdigest()

    def signed_in(self):
        for part in (self.headers.get("Cookie") or "").split(";"):
            name, _, value = part.strip().partition("=")
            if name == COOKIE and secrets.compare_digest(value, self.session_cookie()):
                return True
        return False

    def do_GET(self):
        kind, arg, scope = self.route()
        if kind == "request":
            self.show_request(arg)
        elif kind == "icon":
            self.send_file(ICON, "image/png")
        elif kind == "standing":
            self.show_standing()
        elif kind == "inbox":
            # Opening the bookmark signs this browser in, so thread links open the full inbox.
            secure = "; Secure" if (self.server.settings["base_url"] or "").startswith("https:") else ""
            self.send_file(INBOX_PAGE, "text/html; charset=utf-8", {
                "Set-Cookie": f"{COOKIE}={self.session_cookie()}; Path=/; Max-Age=31536000; "
                              f"HttpOnly; SameSite=Lax{secure}"})
        elif kind == "link" and self.signed_in():
            # The browser keeps the link's #t=…&r=… fragment across the redirect.
            self.send_response(302)
            self.send_header("Location", f"/m/{self.server.token}")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", "0")
            self.end_headers()
        elif kind == "link":
            self.send_file(INBOX_PAGE, "text/html; charset=utf-8")
        elif kind == "api" and arg == "state":
            self.api_state(parse_qs(urlsplit(self.path).query), scope)
        else:
            self.not_found()

    def do_POST(self):
        kind, arg, scope = self.route()
        if kind == "api":
            return self.api_post(arg, scope)
        form = self.read_form()
        if kind == "request":
            self.answer_request(arg, form)
        elif kind == "standing":
            self.run_standing(form)
        else:
            self.not_found()

    def send_file(self, path, ctype, headers=None):
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def send_json(self, data, code=200):
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # -- inbox (bookmarkable): every thread, its messages, and controls

    def api_state(self, query, scope=None):
        settings = self.server.settings
        agents = herdr_agents(settings) if not scope else {}
        threads = []
        for p in sorted(THREADS_DIR.glob("*.json")):
            meta = json.loads(p.read_text())
            if scope and meta["id"] != scope:
                continue
            meta["state"] = thread_status(meta)
            h = meta.get("herdr")
            if h:
                live = agents.get(h.get("pane") or h.get("agent"))
                h["live"] = live["agent_status"] if live else None
                # herdr sees the agent sitting at its prompt even when its last status was busy.
                if meta["state"] == "busy" and h["live"] in ("idle", "done"):
                    meta["state"] = "idle"
            meta["service"] = thread_service(meta)
            meta["agent_kind"] = thread_agent(meta).get("kind")
            threads.append(meta)
        threads.sort(key=lambda m: m["updated"], reverse=True)
        out = {"threads": threads, "now": time.time(), "kinds": list(AGENT_KINDS),
               "keys": list(HERDR_KEYS), "choices": model_suggestions(),
               "herdr": bool(HERDR) and not scope, "scope": scope,
               "services": load_usage().get("services") if not scope else None,
               "service_names": SERVICE_NAMES, "alert_levels": settings["usage_alerts"]}
        tid = scope or query.get("thread", [""])[0]
        if load_thread(tid):
            actions = load_actions()
            entries = read_log(tid)[-500:]
            for x in entries:
                if x["kind"] == "ask":
                    rid = x["rid"]
                    x["open"] = ask_open(rid)
                    ans = load_answer(rid)
                    x["answers"] = ans["answers"] if ans else None
                    x["runs"] = {o: actions[o].get("description") or " ".join(actions[o]["command"])
                                 for q in x["questions"] for o in q["options"] if o in actions}
            out["entries"] = entries
        self.send_json(out)

    def read_json(self):
        n = min(int(self.headers.get("Content-Length") or 0), 65536)
        try:
            data = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}

    def api_post(self, name, scope=None):
        data = self.read_json()
        settings = self.server.settings
        if scope and (name in ("start", "herdr") or data.get("thread") != scope):
            return self.send_json({"error": "Open your inbox bookmark to do that."}, 403)
        if name == "start":
            return self.api_start(data)
        tid = str(data.get("thread", ""))
        meta = load_thread(tid)
        if not meta:
            return self.send_json({"error": "No such thread."}, 404)
        if name == "send":
            text = str(data.get("text", "")).strip()
            if not text:
                return self.send_json({"error": "Write a message first."}, 400)
            # An agent waiting on a single open question gets the message as its answer.
            for rid in reversed(meta["open_asks"]):
                req = load_request(rid)
                if req and len(req["questions"]) == 1 and req.get("allow_text"):
                    with answer_lock:
                        if ask_open(rid):
                            q = req["questions"][0]["message"]
                            record_answers(rid, req, [{"question": q, "option": MESSAGE,
                                                       "text": text, "result": None}])
                            return self.send_json({"answered": rid})
            post(tid, {"kind": "text", "from": "user", "body": text})
            return self.send_json({"queued": True})
        if name == "answer":
            rid = str(data.get("rid", ""))
            req = load_request(rid)
            if not req or req.get("thread") != tid:
                return self.send_json({"error": "No such question."}, 404)
            answers = check_answers(req, data.get("answers"))
            if isinstance(answers, str):
                return self.send_json({"error": answers}, 400)
            with answer_lock:
                if not ask_open(rid):
                    return self.send_json({"error": "Already answered or expired."}, 409)
                ans = record_answers(rid, req, answers)
            run_answer_actions(rid, req, ans, settings["max_output"])
            return self.send_json({"answered": rid})
        if name == "thread":
            def change(m):
                for key in ("muted", "archived"):
                    if key in data:
                        m[key] = bool(data[key])
                if data.get("read"):
                    m["unread"] = 0
            update_thread(tid, change)
            return self.send_json({"ok": True})
        if name == "herdr":
            return self.api_herdr(meta, data)
        self.send_json({"error": "Unknown call."}, 404)

    def api_start(self, data):
        cwd = Path(str(data.get("cwd", "")).strip() or "~").expanduser()
        kind = data.get("kind", "claude")
        task = str(data.get("task", "")).strip()
        if not HERDR:
            return self.send_json({"error": "herdr is not installed."}, 400)
        if not cwd.is_dir():
            return self.send_json({"error": f"No such directory: {cwd}"}, 400)
        if kind not in AGENT_KINDS:
            return self.send_json({"error": "Unknown agent kind."}, 400)
        if not task:
            return self.send_json({"error": "Say what the agent should do."}, 400)
        model, effort = str(data.get("model") or "").strip(), data.get("effort") or ""
        if err := check_choice(kind, model, effort):
            return self.send_json({"error": err}, 400)
        title = str(data.get("title", "")).strip()[:60] or cwd.name or str(cwd)
        tid = secrets.token_hex(6)
        new_thread(title, cwd=str(cwd), tid=tid, herdr={"agent": f"dawg-{tid}", "kind": kind})
        update_thread(tid, lambda m: m.update(agent={"kind": kind}))
        post(tid, {"kind": "text", "from": "user", "body": task}, lambda m: m.update(
            status="busy", delivered=m["seq"]))
        threading.Thread(target=start_agent, args=(self.server.settings, tid, str(cwd), kind, task,
                                                   model, effort), daemon=True).start()
        self.send_json({"thread": tid})

    def api_herdr(self, meta, data):
        settings, h, op = self.server.settings, meta.get("herdr"), data.get("op")
        if not h:
            return self.send_json({"error": "This agent isn't running in herdr."}, 400)
        try:
            if op == "read":
                out = herdr(settings, "agent", "read", herdr_target(meta), "--source", "recent-unwrapped",
                            "--lines", "80", timeout=10)
                return self.send_json({"output": out if isinstance(out, str) else json.dumps(out)})
            if op == "keys":
                key = data.get("key")
                if key not in HERDR_KEYS:
                    return self.send_json({"error": "That key isn't allowed."}, 400)
                herdr(settings, "agent", "send-keys", herdr_target(meta), key, timeout=10)
                return self.send_json({"ok": True})
            if op == "prompt":
                text = str(data.get("text", "")).strip()
                if not text:
                    return self.send_json({"error": "Write a prompt first."}, 400)
                herdr(settings, "agent", "prompt", herdr_target(meta), text, timeout=15)
                post(meta["id"], {"kind": "system", "from": "system",
                                  "body": f"Typed into the terminal: {text}"})
                return self.send_json({"ok": True})
            if op == "settings":
                model, effort = str(data.get("model") or "").strip(), data.get("effort") or ""
                kind = thread_agent(meta).get("kind")
                if err := check_choice(kind, model, effort):
                    return self.send_json({"error": err}, 400)
                if not (meta.get("usage") or {}).get("session_id"):
                    return self.send_json({"error": "Its session isn't known yet: Claude agents "
                                                    "report it through `dawg statusline` (install.sh "
                                                    "sets it up), Codex agents once they've run a dawg "
                                                    "command."}, 400)
                live = herdr_agents(settings).get(herdr_target(meta))
                if not live or live["agent_status"] not in ("idle", "done"):
                    return self.send_json({"error": "Wait until the agent is idle in herdr."}, 409)
                threading.Thread(target=restart_agent, args=(settings, meta["id"], model, effort),
                                 daemon=True).start()
                return self.send_json({"ok": True})
            if op == "clear":
                if thread_agent(meta).get("kind") not in AGENT_KINDS:
                    return self.send_json({"error": "Only Claude and Codex agents can be cleared."}, 400)
                live = herdr_agents(settings).get(herdr_target(meta))
                if not live or live["agent_status"] not in ("idle", "done"):
                    return self.send_json({"error": "Wait until the agent is idle in herdr."}, 409)
                threading.Thread(target=clear_agent, args=(settings, meta["id"]),
                                 daemon=True).start()
                return self.send_json({"ok": True})
            if op == "close":
                if h.get("adopted"):
                    return self.send_json({"error": "This agent's workspace isn't dawg's to close."}, 400)
                herdr(settings, "workspace", "close", h["workspace"], timeout=15)
                post(meta["id"], {"kind": "system", "from": "system",
                                  "body": "Closed the herdr workspace."},
                     lambda m: m.update(status="idle"))
                return self.send_json({"ok": True})
        except (HerdrError, KeyError) as err:
            return self.send_json({"error": str(err)}, 502)
        self.send_json({"error": "Unknown herdr call."}, 400)

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
            ans = record_answers(rid, req, draft["answers"])
        run_answer_actions(rid, req, ans, self.server.settings["max_output"])
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
    THREADS_DIR.mkdir(parents=True, exist_ok=True)
    purge_old(settings["keep_days"], settings["thread_keep_days"])
    srv = ThreadingHTTPServer((settings["bind"], settings["port"]), Handler)
    srv.settings = settings
    srv.token = get_token()
    threading.Thread(target=poll_loop, args=(settings,), daemon=True).start()
    print(f"dawgd listening on {settings['bind']}:{settings['port']}", file=sys.stderr, flush=True)
    if settings["base_url"]:
        print(f"inbox: {settings['base_url']}/m/<token>  (token in {TOKEN_FILE})", file=sys.stderr, flush=True)
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
    meta = thread_for(args)
    rid = secrets.token_hex(16)
    now = time.time()
    req = {"id": rid, "title": args.title, "questions": qs,
           "allow_text": args.text, "created": now, "expires": now + args.expires}
    req["thread"] = meta["id"]
    write_json(req_path(rid), req)
    first = qs[0]
    if len(qs) > 1:
        body = f"{len(qs)} questions. 1: {first['message']}\nTap to answer them one at a time"
    else:
        body = f"{first['message']}\nTap to reply" + (": " + " / ".join(first["options"]) if first["options"] else "")
    runs = sorted({o for q in qs for o in q["options"] if o in actions})
    if runs:
        body += f"  (runs: {', '.join(runs)})"
    if meta["muted"]:
        print("dawg ask: texts are muted for this thread; the question is in the inbox",
              file=sys.stderr)
    elif not notify(args.title, body, url=tap_link(settings, meta, rid), group=args.group):
        req_path(rid).unlink(missing_ok=True)
        print("dawg ask: notification not sent; nothing to wait for", file=sys.stderr)
        return 4
    post(meta["id"], {"kind": "ask", "from": "agent", "rid": rid, "questions": qs,
                      "allow_text": args.text, "expires": req["expires"]},
         lambda m: m["open_asks"].append(rid))
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


# ---------------------------------------------------------------- thread commands

def thread_for(args):
    """The thread named by -T or DAWG_THREAD, else Notifications; created on first use."""
    tid = args.thread or NOTIFICATIONS
    if not valid_thread(tid):
        sys.exit(f"dawg {args.cmd}: bad thread id {tid!r}")
    meta = load_thread(tid)
    if not meta:
        meta = (new_thread("Notifications", tid=tid) if tid == NOTIFICATIONS
                else new_thread(args.title, cwd=os.getcwd(), tid=tid))
    # Remember which Claude Code or Codex process this agent runs in, so its
    # usage reports reach this thread.
    kind, proc = agent_process()
    if proc and tid != NOTIFICATIONS and thread_agent(meta).get("proc") != proc:
        meta = update_thread(tid, lambda m: m.update(agent={"kind": kind, "proc": proc}))
    # An agent running in herdr can be managed from the inbox through its pane.
    pane = os.environ.get("HERDR_PANE_ID") if os.environ.get("HERDR_ENV") == "1" else None
    if pane and tid != NOTIFICATIONS and (meta.get("herdr") or {}).get("pane") != pane:
        def adopt(m):
            h = m.get("herdr") or {"kind": kind or "claude", "adopted": True}
            h.update(pane=pane, workspace=os.environ.get("HERDR_WORKSPACE_ID"))
            if h.get("adopted"):
                h["agent"] = None  # addressed by pane until a restart names it
            m["herdr"] = h
        meta = update_thread(tid, adopt)
    return meta


def queued_hint(tid):
    if n := queued_count(tid):
        print(f"dawg: {n} message{'s' if n > 1 else ''} queued for you; run: dawg inbox -T {tid}",
              file=sys.stderr)


def send_cmd(args):
    meta = thread_for(args)
    post(meta["id"], {"kind": "text", "from": "agent", "body": args.message})
    if not meta["muted"]:
        url = args.url or tap_link(load_settings(), meta)
        if not notify(args.title, args.message, url=url, group=args.group,
                      level=args.level, sound=args.sound):
            print("dawg: posted to the inbox, but the text wasn't sent", file=sys.stderr)
            return 1
    queued_hint(meta["id"])
    return 0


def status_cmd(args):
    meta = thread_for(args)
    note = " ".join(args.note)
    if (meta["status"], meta["status_note"]) != (args.status, note):
        post(meta["id"], {"kind": "status", "from": "agent", "status": args.status, "note": note},
             lambda m: m.update(status=args.status, status_note=note))
    queued_hint(meta["id"])
    return 0


def inbox_cmd(args):
    meta = thread_for(args)
    deadline = time.time() + args.expires
    while True:
        msgs = take_queued(meta["id"])
        if msgs or not args.wait:
            break
        if time.time() > deadline:
            print("dawg inbox: nothing arrived before the wait expired", file=sys.stderr)
            return 3
        time.sleep(1)
    if args.json:
        print(json.dumps({"messages": [{"text": m["body"], "sent_at": m["ts"]} for m in msgs]}))
    else:
        print("\n\n".join(m["body"] for m in msgs))
    return 0


def statusline_cmd(args):
    raw = sys.stdin.read()
    try:
        data = json.loads(raw or "{}")
    except ValueError:
        data = {}
    try:
        report_usage(data, load_settings())
    except Exception as err:  # never break the status line
        print(f"dawg statusline: {err}", file=sys.stderr)
    print(status_text(data))
    return 0


def thread_cmd(args):
    if args.action == "new":
        print(new_thread(args.title, cwd=os.getcwd())["id"])
    else:
        for p in sorted(THREADS_DIR.glob("*.json")):
            m = json.loads(p.read_text())
            print(f"{m['id']}  {thread_status(m):7}  {m['title']}  {m.get('cwd') or ''}")
    return 0


def main():
    env_thread = os.environ.get("DAWG_THREAD")
    p = argparse.ArgumentParser(prog="dawgd")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("serve").set_defaults(func=serve)

    def threaded(name, **kw):
        c = sub.add_parser(name, prog=f"dawg {name}", **kw)
        c.add_argument("-T", "--thread", default=env_thread,
                       help="inbox thread id (default: $DAWG_THREAD, else Notifications)")
        c.add_argument("-t", "--title", default=os.uname().nodename,
                       help="title; also names a thread it creates")
        return c

    s = threaded("send", description="Post a message to a thread and text it unless muted.")
    s.add_argument("-g", "--group", default="dawgd")
    s.add_argument("-l", "--level")
    s.add_argument("-s", "--sound")
    s.add_argument("-u", "--url")
    s.add_argument("message")
    s.set_defaults(func=send_cmd)
    s = threaded("status", description="Set a thread's status, shown in the inbox.")
    s.add_argument("status", choices=STATUSES)
    s.add_argument("note", nargs="*", help="short note shown with the status")
    s.set_defaults(func=status_cmd)
    s = threaded("inbox", description="Print messages the user queued for this thread.")
    s.add_argument("-w", "--wait", action="store_true", help="block until a message arrives")
    s.add_argument("-e", "--expires", type=int, default=3600, help="with -w, seconds (default 3600)")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=inbox_cmd)
    s = sub.add_parser("statusline", prog="dawg statusline",
                       description="Claude Code status line command: reports model, effort and "
                                   "usage to the inbox, and prints a short status line.")
    s.set_defaults(func=statusline_cmd)
    s = sub.add_parser("thread", prog="dawg thread", description="Start or list inbox threads.")
    s.add_argument("action", choices=("new", "list"))
    s.add_argument("-t", "--title", default=os.uname().nodename)
    s.set_defaults(func=thread_cmd)

    a = threaded("ask", description="Send a notification with reply options.")
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
