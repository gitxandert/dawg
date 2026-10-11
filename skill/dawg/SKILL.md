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
  - On `That's all`, stop and wait. Away mode stays on until they're back.
- **While away (non-interactive):** send one-way `dawg` messages for
  milestones and completion.
- **Exit** when any new message arrives in the terminal: the user is back.
  Stop the pending background `dawg ask` (TaskStop), say in the terminal that
  away mode is off, and handle any open question there. A phone reply like
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
dawg ask -t "<project>" -g claude --text -w --json -e 3600 "Question?" Reply
```

- `--text` adds a free-text box. `Reply` is the submit button. For choices,
  list them as buttons instead, e.g. `"Push to GitHub" "Add tests" "That's all"`.
  Keep each label to a few words.
- Run it with the Bash tool's `run_in_background: true`. A foreground call
  would be killed after 10 minutes, and you're re-invoked when it exits.
- On exit 0, stdout is JSON: `option` is the button tapped and `text` is what
  they typed (may be empty). Treat it as the user's instruction, the same as a
  terminal message.
- Exit 3 means it expired unanswered. Send at most one follow-up, then stop
  and wait for the terminal.
- `-e` is the expiry in seconds. Use longer (e.g. `14400`) if the user said
  they'll be gone a while.

## Send a message without a reply (non-interactive only)

```bash
dawg -t "<project>" -g claude "Tests pass, PR branch pushed"
```

Add `-l timeSensitive` only when it needs attention right now.

## Rules

- Short messages, question first, context after. Lock screens truncate, but
  the reply page shows the full text.
- One outstanding `ask` at a time.
- Never use an option name that matches a key in `~/.config/dawg/actions.toml`
  (`ping`, `uptime`, `disk`, `memory`, ...): tapping it runs that command.
- Never send secrets, tokens, or large code dumps. Notifications pass through
  a third-party push server.
- An abandoned `ask` stays tappable on the phone until it expires, and nobody
  reads that reply. If you stop waiting on one, tell the user so they don't
  answer it.
