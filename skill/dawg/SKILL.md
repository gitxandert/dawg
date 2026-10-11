---
name: dawg
description: Dispatched Alerts While Gone. Text the user on their phone and get their replies while they're away from the terminal. Use when the user asks to be texted or pinged, says they're stepping away and wants to keep directing work from their phone, or asks to be notified when something finishes.
---

# dawg

`dawg` sends push notifications to the user's iPhone through the Bark app.
`dawg ask` sends one whose tap opens a reply page served by `dawgd` over
HTTPS on the user's tailnet, and can block until they answer. Together they
make a text channel: you send, they reply from the phone.

## Mode

Decide the mode when away mode starts, passing on any arguments you were
invoked with:

```bash
python3 <this skill's directory>/scripts/mode.py [ARGUMENTS]
```

It prints `interactive` (the default) or `non-interactive`. The order is:
`--non-interactive` or `--interactive` passed to `/dawg`, then
`dawg.toml` in the workspace's `.claude/`, `.codex/` or `.agents/` directory,
then the same file in `~`. A config file contains `mode = "non-interactive"`.
The mode is also non-interactive if the user says they want updates only.

- **Interactive:** every message is a `dawg ask` the user can answer. Never
  send a one-way `dawg` message, because it leaves them unable to reply.
- **Non-interactive:** send one-way `dawg` updates only. Questions wait in
  the terminal.

## Thread

Each agent session has its own thread in the user's inbox page, which shows
every message you send, your status, and lets them answer from the browser.
At the start of away mode:

- If `$DAWG_THREAD` is set (you were started from the inbox), use it.
- Otherwise run `dawg thread new -t "<project>"` once and keep the id it
  prints for the rest of the session.

Pass `-T <id>` on every `dawg` call: `dawg -T`, `dawg ask -T`,
`dawg status -T`, `dawg inbox -T`. The Bash tool doesn't keep exported
variables between calls, so write the id out each time.

- **Status:** `dawg status -T <id> busy "<what you're doing>"` when you start
  or change tasks, `idle` when you're waiting with nothing to do. An open
  ask shows as `blocked` automatically; don't set it yourself.
- **Inbox:** the user can send you messages from the browser at any time.
  Between steps, run `dawg inbox -T <id>`. It prints queued messages (nothing
  if there are none). Treat them as user instructions. `dawg status` and
  `dawg -T` also tell you on stderr when messages are waiting.
- **Muted threads:** the user can turn off texts for a thread. Asks and
  messages still reach the inbox and work the same; `dawg ask` just notes on
  stderr that no text went out.

## Away mode

You can't tell whether the user is at the terminal, so presence is explicit.
Never infer it from screen lock or idle time: the desktop session reports
locked while the user is active over SSH.

- **Enter** when the user says they're stepping away, asks you to text them,
  or invokes `/dawg`. If you have no task, ask what to work on (interactive)
  or send "Away mode on" (non-interactive). If you have a task, start on it.
- **While away (interactive):**
  - A question or blocker goes out as a `dawg ask`. Keep working on whatever
    doesn't depend on the answer.
  - When a task is done, never just report it. Send an ask that summarizes
    what you did and asks whether there's anything else. Offer the obvious
    next steps as buttons, plus `That's all`; `--text` lets them type
    something else. Then do what they pick.
  - On `That's all`, set your status to `idle` and wait for the inbox: run
    `dawg inbox -T <id> -w -e 14400` in the background. When it returns a
    message, act on it as you would a reply. Away mode stays on until
    they're back.
- **While away (non-interactive):** send one-way `dawg` messages for
  milestones and completion.
- **Plan mode:** a phone reply can't take you out of plan mode. Only the
  terminal approval prompt can. If away mode starts in plan mode, say so in
  the terminal. When the user approves a plan by phone, finish everything
  plan mode allows. Then ask for approval and send an ask saying the approval
  is waiting in the terminal.
- **Exit** when any new message arrives in the terminal: the user is back.
  Stop the pending background `dawg ask` or `dawg inbox -w` (TaskStop), set
  your status to `idle`, say in the terminal that away mode is off, and
  handle any open question there. A phone reply like
  "I'm back" or "stop texting" also ends away mode.

Outside away mode, use dawg only for one-off requests ("ping me when the
build finishes"). Ask in that case too, unless the mode is non-interactive.

## Check it works

```bash
command -v dawg && systemctl --user is-active dawgd
```

If dawgd isn't active, one-way `dawg` still works but replies won't arrive.
Tell the user in the terminal instead of asking questions you can't receive.

## Ask and wait for a reply

```bash
dawg ask -T <id> -t "<project>" -g claude --text -w --json -e 3600 "Question?" [OPTION..]
```

- Always pass `--text`. It adds a text box, plus a button for sending typed
  text as its own instruction: "Do this instead" when there are options,
  "Send" when there are none. Options are buttons, e.g.
  `"Push to GitHub" "Add tests" "That's all"`. Keep each label to a few words.
  With no options, the ask is a plain open question.
- Run it with the Bash tool's `run_in_background: true`. A foreground call
  would be killed after 10 minutes, and you're re-invoked when it exits.
- On exit 0, stdout is JSON: `option` is the button tapped and `text` is what
  they typed. `option` is `"message"` when they sent their own instruction
  instead of picking one; follow `text` then. Otherwise `text` is an optional
  note on their choice. Treat either as the user's instruction, the same as a
  terminal message.
- Exit 4 means the notification couldn't be sent, so nobody will answer. Tell
  the user in the terminal.
- The user may answer from the phone or from the inbox page; the output is
  the same either way.
- Exit 3 means it expired unanswered. Send at most one follow-up, then stop
  and wait for the terminal.
- `-e` is the expiry in seconds. Use longer (e.g. `14400`) if the user said
  they'll be gone a while.

## Several questions at once

When you have more than one question, send them all in one ask, never
several asks. The page shows them one at a time, with Back, then a review
step before they submit:

```bash
dawg ask -T <id> -t "<project>" -g claude --text -w --json -e 3600 --questions - <<'EOF'
[{"message": "Which database?", "options": ["Postgres", "SQLite"]},
 {"message": "Add auth now?", "options": ["Yes", "Later"]},
 {"message": "Anything else?"}]
EOF
```

`--json` then prints `{"answers": [...], "answered_at": ...}` with one
`{"question", "option", "text", "result"}` per question, in order. Read each
the same way as a single answer.

## Send a message without a reply (non-interactive only)

```bash
dawg -T <id> -t "<project>" -g claude "Tests pass, PR branch pushed"
```

Add `-l timeSensitive` only when it needs attention right now.

## Rules

- Short messages, question first, context after. Lock screens truncate, but
  the reply page shows the full text.
- One outstanding `ask` or `inbox -w` at a time.
- `message` is reserved and can't be an option.
- Never use an option name that matches a key in `~/.config/dawg/actions.toml`
  (`ping`, `uptime`, `disk`, `memory`, ...): tapping it runs that command.
- Never send secrets, tokens, or large code dumps. Notifications pass through
  a third-party push server.
- An abandoned `ask` stays tappable on the phone until it expires, and nobody
  reads that reply. If you stop waiting on one, tell the user so they don't
  answer it.
