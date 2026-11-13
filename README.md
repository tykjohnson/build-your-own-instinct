# build-your-own-instinct

Text your coding agent from your phone. You send a text to a Google Voice number, the agent
(Claude Code by default, or Codex, or any CLI you point it at) runs the request on your Mac
with your tools, files and skills, and texts you back the answer. Each text gets its own
agent process, so you can fire off several tasks and they run side by side.

It is one Python script with no dependencies beyond the standard library. It drives your own
logged-in Chrome, so there are no API keys, no Twilio account and no server.

## Install

Pick one. All three end in the same place: a `config.json`, a passing `--check`, and the
watcher running in its own Terminal window.

### A. Paste this prompt into your agent

Works in Claude Code (in cmux, a terminal, or the Claude desktop app's Code tab) and in
Codex (the ChatGPT desktop app's Codex, the Codex app, or the `codex` CLI). The agent needs
to be able to run shell commands on your Mac.

```text
Set up https://github.com/tykjohnson/build-your-own-instinct on this Mac so I can text my
coding agent from my phone through Google Voice.

1. Clone it to ~/build-your-own-instinct (if that folder already exists, git pull instead)
   and read README.md.
2. Ask me for these and wait for my answers. Do not guess them:
   - my cell phone number (the one I will text from)
   - my Google Voice number
   - the Google account email that owns that Google Voice number
3. Pick the agent command: "claude" if you are Claude Code, "codex exec" if you are Codex,
   unless I say otherwise. Then run, from the repo folder:
   python3 watcher.py --init --phone "<my cell>" --gv-number "<GV number>" --email "<email>" --agent-cmd "<agent command>"
4. Run python3 test_watcher.py and python3 watcher.py --check. Fix anything marked FAIL.
   If the agent CLI is not found, find it (which claude / which codex) and rerun step 3
   with its full path.
5. Walk me through every WARN line and the "Manual steps only you can do" list. I have to
   click those myself. Wait until I say they are done.
6. Run python3 watcher.py --start. It opens the watcher in its own Terminal window, outside
   your session, so it keeps running after you finish. Do not run run.sh or watcher.py in
   your own shell. If --start fails, tell me to open Terminal and run:
   cd ~/build-your-own-instinct && ./run.sh
7. Tell me to click Allow when macOS asks "Terminal wants to control Google Chrome", then
   text my Google Voice number "status". I should get "Here. Nothing running." back.
```

If your chat app cannot run commands (plain ChatGPT chat, Claude chat without Code), use B.

### B. Terminal, copy and paste

```bash
git clone https://github.com/tykjohnson/build-your-own-instinct ~/build-your-own-instinct
cd ~/build-your-own-instinct
python3 watcher.py --init --phone "+1 555 555 0123" --gv-number "+1 555 555 0100" --email "you@gmail.com"
#   add --agent-cmd "codex exec" to use Codex instead of Claude Code
python3 test_watcher.py
python3 watcher.py --check
```

Do the manual steps `--check` lists (below), then start it:

```bash
python3 watcher.py --start      # opens it in a new Terminal window
# or keep it in the terminal you are in (cmux, iTerm, Terminal):
./run.sh
```

Text your Google Voice number `status`. You should get "Here. Nothing running." back.

### Manual steps (nobody can do these for you)

1. **Google Voice forwards texts to Gmail.** voice.google.com > Settings > Messages > turn on
   "Forward messages to email". Then text your Google Voice number once from your phone so
   a thread exists in Gmail.
2. **Chrome lets scripts run JavaScript.** In Chrome's menu bar: View > Developer > Allow
   JavaScript from Apple Events. Without it the watcher cannot read or type into tabs.
3. **macOS Automation permission.** The first time the watcher touches Chrome, macOS asks
   "<app> wants access to control Google Chrome". Click Allow. The app named is whatever
   launched the watcher:
   - `--start`: Terminal (or the app you passed with `--app iTerm`)
   - `./run.sh` inside cmux: cmux
   - run from the Claude desktop app or the ChatGPT/Codex app's own shell: that app. Don't do
     this; those shells end with the session, and Codex runs commands in a sandbox that
     blocks Apple Events. Use `--start`.

   If you clicked Don't Allow: System Settings > Privacy & Security > Automation > that app >
   turn on Google Chrome, then restart the watcher.
4. **Your agent CLI is logged in.** Run `claude` (or `codex login`) once by hand.

## How it works

```
  your phone
      |  SMS
      v
  Google Voice number ----(forward texts to email)----> Gmail inbox
                                                            |
                                         watcher.py polls the Gmail Voice feed
                                         in a dedicated Chrome tab (JXA)
                                                            |
                                                            v
                                         claude -p "<your text>" --resume <last> --fork-session
                                         or: codex exec resume <thread> "<your text>"
                                         (one process per text, up to 6 at once)
                                                            |
                                                            v
  your phone <---- SMS ---- Google Voice web app <---- reply typed into voice.google.com
                            (fallback: reply to the Google Voice email in Gmail)
```

1. Google Voice forwards each incoming text to your Gmail.
2. `watcher.py` finds your real Chrome by PID and runs JavaScript in a marked Gmail tab
   through `osascript -l JavaScript`. It reads the Gmail Google Voice feed and the thread's
   print view, so a text is caught even if Gmail already marked it read. Gmail splits one
   Google Voice conversation into several threads over time, so it keeps watching the four
   newest threads for your number (saved in `state.json`), not just one.
3. Texts from numbers outside `allowed_senders` are logged and ignored.
4. Texts sent within a minute of each other are batched into one prompt. A backlog of texts
   sent minutes apart (say, after the watcher was down) becomes one task per text.
   Each batch starts its own agent process in its own thread, alongside any that are
   still running (up to `max_parallel`, default 6). The watcher acks with "on it: " and the
   first 40 characters of the task, plus "(alongside 2 other tasks)" when others are running.
   Past the limit it says so and starts nothing;
   resend once something finishes. The prompt lists the other texts still being worked on,
   so agents don't duplicate each other's work.
5. The answer is typed into the Google Voice web app in a second marked tab and sent. Long
   answers are split into up to four texts. If the web app fails, it replies to the Google
   Voice email in Gmail instead.

On first start it marks every text already in Gmail as seen, so old texts are not run as
new requests (`python3 watcher.py --prime` does the same by hand).

### Parallel tasks and memory

- **Claude:** every job runs `--resume <last finished session> --fork-session`, so it knows
  the recent back-and-forth but writes to its own copy of the conversation. When a job
  finishes, its session becomes the base for the next one (the newest finisher wins), so a
  "yes" to a question still lands in a session that remembers asking it.
- **Codex:** `codex exec` has no fork option the watcher uses, and two processes must not
  write one thread at once. A job resumes the shared thread when no other job holds it.
  A job that starts while another holds it gets a fresh thread with the house rules but
  without the earlier conversation, and that thread is dropped when it ends.
- **Other CLIs** keep no session memory, so each job is independent anyway.

### Text commands

| Text | What happens |
| --- | --- |
| `status` (or `?`) | Lists every running task with its minutes so far, e.g. `2 running:` then `• 4 min - draft the reply`. "Here. Nothing running." when idle. |
| `stop` (or `cancel`, `stop all`, `never mind`) | Kills every running task and replies "Stopped 2 tasks." Stopped tasks send no answer. |

Each task that runs over 10 minutes sends one "Still on it: <its text>" note.

Other scripts on your Mac can text you too: drop a `.txt` file into `outbox/` and the
watcher sends it to the first number in `allowed_senders`, then renames it to `.sent`.

### When Chrome freezes

A JavaScript popup (an `alert`, a `confirm`, a "Leave site?" dialog) left open in any Chrome
tab blocks every AppleScript call to Chrome, so the watcher can neither read nor send. When
its tab lookup times out, the watcher treats Chrome as frozen, not as missing a tab, and
opens no new tabs. Once Chrome has been frozen for `restart_after_seconds` (default 10
minutes):

- If nobody has touched the Mac's keyboard or mouse for `idle_needed_seconds` (default 10
  minutes) and it hasn't restarted Chrome in the last 30 minutes, it quits Chrome (a normal
  quit, so Chrome saves its session; force-quit only if Chrome ignores that for 45 seconds),
  starts it again with `chrome_profile`, reopens your windows, waits until Chrome answers,
  runs your `after_chrome_restart` script if you set one, and texts you that it restarted
  Chrome.
- Reopening windows: if Chrome's "On startup" setting is "Open the New Tab page", a restart
  would come back with none of your windows. So for each profile in Chrome's `Local State`,
  the watcher reads the newest `Sessions/Session_*` file saved before the quit (`snss.py`
  parses Chrome's session format) and opens each normal window again with its tabs in order,
  up to 40 per window. It leaves out its own Gmail and Voice tabs (it reopens those itself),
  repeated URLs and New Tab pages. Closed tabs, popups and app windows stay closed. Profiles
  set to "Continue where you left off" are skipped, since Chrome restores those itself. Set
  `"restore_windows_after_restart": false` to turn this off. Only each tab's current page
  comes back, not its back/forward history.
- If someone is using the Mac (or `auto_restart_chrome` is `false`), it sends one alert
  instead, through iMessage to `alert_imessage` if you set it, otherwise only to the log.
  Quit and reopen Chrome yourself and the watcher picks up again on its own.

The built-in system prompt also tells the agent to stub `window.confirm`, `window.alert` and
`onbeforeunload` in a tab before a click that might pop a dialog, to close tabs it opens, and
to say so if Chrome stops answering instead of retrying.

To restart safely after editing the script, `touch restart.flag`. The watcher exits once no
tasks are running and `run.sh` starts the new code.

## Requirements

- macOS (uses `osascript`, JXA and `caffeinate`)
- Google Chrome, signed in to the Google account that owns the Google Voice number
- A US Google Voice number
- An agent CLI, installed and logged in: Claude Code (`claude`), Codex (`codex`), or another
  CLI that takes a prompt and prints an answer
- Python 3.8 or newer

## Choosing the agent

The watcher runs whatever `agent_cmd` says (config.json), or the `AGENT_CMD` environment
variable if set, which wins over the config. The binary is looked up on PATH and in
`~/.local/bin`, `/opt/homebrew/bin`, `/usr/local/bin`, `~/.claude/local`, `~/.npm-global/bin`,
`~/.bun/bin`, `~/.volta/bin` and `~/bin`, because apps started from the Dock often get a
short PATH. A full path always works.

| `agent_cmd` | What runs |
| --- | --- |
| `claude` (default) | `claude -p <text> --output-format json --resume <session> --fork-session` with `model`, `effort`, `permission_mode` and the built-in system prompt. Extra flags you add (`claude --model sonnet`) are kept and win over the config. |
| `codex exec` | `codex exec --skip-git-repo-check --sandbox workspace-write --json -o <file> <text>`, then `codex exec ... resume <thread_id> <text>` for follow-ups. Add `-m <model>` or your own `--sandbox` to the command to change those. The house rules go at the top of the first message of each thread, since Codex has no system-prompt flag. |
| anything else | Your command with the prompt in place of a `{prompt}` argument, or added as the last argument. Its stdout is the reply. No session memory. |

`fallback_agent_cmd` takes the same forms. It runs only when the main agent fails with a
usage or rate limit error, e.g. `"agent_cmd": "claude", "fallback_agent_cmd": "codex exec"`.

## Config

`config.json` sits next to `watcher.py` and is gitignored. `--init` writes it, keeping any
values already in it, so you can rerun `--init` to change one thing. Set `WATCHER_CONFIG` to
use a different path. The watcher exits with a clear message if the file is missing or a
required value is wrong.

| Key | Required | What it is |
| --- | --- | --- |
| `allowed_senders` | yes | Phone numbers allowed to text the agent. Replies and outbox texts go to the first one. |
| `gv_number` | yes | Your Google Voice number. Any format; it is normalized to 11 digits. |
| `google_account_email` | yes | The Google account signed in to Gmail and Google Voice. The watcher refuses to send from a Voice tab signed in as anyone else. |
| `reply_from_email` | no | From address for the Gmail fallback reply. Defaults to `google_account_email`. |
| `gmail_account_index`, `gv_account_index` | no | The `N` in `mail.google.com/mail/u/N` and `voice.google.com/u/N` if you are signed in to several Google accounts. Default 0. |
| `chrome_app`, `chrome_profile` | no | Chrome binary and the profile folder name used when the watcher has to open its tab or restart Chrome. |
| `chrome_user_data_dir` | no | Chrome's data folder, where `Local State`, profile `Preferences` and session files live. Default `~/Library/Application Support/Google/Chrome`. |
| `agent_cmd` | no | The agent CLI. See Choosing the agent. Default `claude`. |
| `agent_cwd` | no | Working directory for the agent. Default is your home folder. |
| `model`, `effort` | no | Claude only: passed to `claude --model` and `--effort`. |
| `permission_mode` | no | Claude only: passed to `claude --permission-mode`. See Safety. |
| `fallback_agent_cmd` | no | Second agent to try when the first is out of usage. `null` to disable. |
| `poll_seconds`, `settle_seconds`, `job_timeout_seconds` | no | Poll interval, batching window, and the per-job time limit. |
| `max_parallel` | no | How many agent processes may run at once. Default 6. Texts past the limit are refused with a note, not queued. |
| `system_prompt_extra` | no | Your own lines appended to the built-in system prompt (house rules, tone, tools to prefer). |
| `auto_restart_chrome` | no | Restart Chrome when it has been frozen a while and nobody is using the Mac. Default `true`. See When Chrome freezes. |
| `restart_after_seconds`, `idle_needed_seconds` | no | How long Chrome must be frozen, and how long the Mac must be untouched, before an auto-restart. Default 600 each. |
| `after_chrome_restart` | no | A script to run after an auto-restart, e.g. to restart other jobs that drive Chrome. Run directly if executable, else with `bash`. Output goes to `after-restart.log`. Default none. |
| `restore_windows_after_restart` | no | After an auto-restart, reopen the windows and tabs each profile had open. Default `true`. |
| `alert_imessage` | no | Phone number or Apple ID to iMessage (through Messages.app) when Chrome is frozen and the watcher won't restart it. Google Voice can't send while Chrome is frozen. Default none: the alert only goes to the log. The first alert makes macOS ask to let the watcher's app control Messages. |

## Gotchas

- **Keep the watcher's Gmail tab open.** It opens a Gmail tab marked `?gvclaude=1` and a
  Google Voice tab marked `gvclaudesend=`. Closing them is fine (it reopens them), but it
  costs about 12 seconds and can steal focus. Leave them in a window you don't use.
- **A headless or test Chrome can hijack AppleScript.** If a script or test runner left a
  `--headless` Chrome or one with its own `--user-data-dir` running, `tell application
  "Google Chrome"` may talk to that one instead of yours. The watcher addresses your real
  Chrome by PID to avoid this, and `--check` warns when such instances are running. If texts
  stop being read, quit them.
- **Don't run it inside the agent's shell.** A watcher started from Claude Code's or
  Codex's own command tool dies when that session ends, and Codex's sandbox can block Apple
  Events. `--start` hands it to Terminal instead.
- **Started from inside Claude Code?** The watcher removes the `CLAUDECODE` variable before
  running `claude -p`, so a nested run is not refused.
- **Chrome must stay open and signed in**, and the Mac awake (`run.sh` uses `caffeinate`).

## Safety

- Only numbers in `allowed_senders` can start a job. Everyone else is logged and ignored.
  Caller ID can be spoofed, so treat the allowlist as a filter, not as authentication.
- The agent runs non-interactively. For Claude, `permission_mode` from your config decides
  what it may do: `plan` is the most conservative, `bypassPermissions` the least. For Codex,
  the default `--sandbox workspace-write` limits writes to the working folder and turns off
  network access for shell commands; widen it in `agent_cmd` only if you mean to.
- The built-in system prompt tells the agent to ask before anything outward-facing or hard to
  undo (messaging other people, posting, buying, deleting, paying). It replies with what it
  would do and waits for you to text "yes". This is an instruction, not a hard guarantee.
- Everything runs on your own machine, in your own browser session. The bridge itself only
  talks to Gmail, Google Voice and the local agent CLI. What the agent does from there
  depends on your agent setup.
- Never commit `config.json`, `state.json`, `watcher.log` or `outbox/`. They hold your
  numbers, session ids and message text. `.gitignore` covers them.

## Known limits

- It automates the Gmail and Google Voice web UIs by selectors and labels ("Type a
  message", "Send message", "Reply", "Pop out reply"). When Google changes those pages,
  sending breaks until the selectors are updated.
- Google Voice's email gateway drops everything after the first blank line of an emailed
  reply and joins line breaks with spaces. The Gmail fallback removes blank lines so whole
  reports get through. The Voice web app path keeps formatting.
- The Voice web app goes stale when idle, so the watcher reloads it before sending if it sat
  for more than 10 minutes, and retries once after a reload.
- US numbers only (the Voice URL is built with `+1`).
- Built for one owner. Replies go to the first number in `allowed_senders`.
- Parallel jobs share one Mac. Two tasks that edit the same files can collide; the prompt
  tells each agent what the others are doing, but nothing locks files.
- Parallel jobs use your agent's usage limits faster.
- Auto-restart quits your whole Chrome, not just the watcher's tabs. The watcher reopens
  your windows from Chrome's session files, but unsaved form input, tab history and the open
  popup are lost. It only happens when the Mac has been idle; set
  `"auto_restart_chrome": false` to turn it off.

Run the offline tests (no Chrome, no config, no texts sent, fake agent CLIs) with
`python3 test_watcher.py`.

## License

MIT. See LICENSE.
