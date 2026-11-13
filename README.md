# build-your-own-instinct

Text Claude Code from your phone. You send a text to a Google Voice number, Claude Code runs
the request on your Mac with your tools, files and skills, and texts you back the answer.

It is one Python script with no dependencies beyond the standard library. It drives your own
logged-in Chrome, so there are no API keys, no Twilio account and no server.

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
                                         claude -p "<your text>" --resume <session>
                                         (runs in a worker thread on your Mac)
                                                            |
                                                            v
  your phone <---- SMS ---- Google Voice web app <---- reply typed into voice.google.com
                            (fallback: reply to the Google Voice email in Gmail)
```

1. Google Voice forwards each incoming text to your Gmail.
2. `watcher.py` finds your real Chrome by PID and runs JavaScript in a marked Gmail tab
   through `osascript -l JavaScript`. It reads the Gmail Google Voice feed and the thread's
   print view, so a text is caught even if Gmail already marked it read.
3. Texts from numbers outside `allowed_senders` are logged and ignored.
4. Texts that arrive within a few seconds of each other are batched into one prompt.
   The watcher acks with "on it", then runs `claude -p` in a worker thread, resuming the
   same session each time so Claude remembers the conversation.
5. The answer is typed into the Google Voice web app in a second marked tab and sent. Long
   answers are split into up to four texts. If the web app fails, it replies to the Google
   Voice email in Gmail instead.

Text `status` to see what it is working on, or `stop` to kill the current job. If a job
runs over 10 minutes you get one "still on it" text.

Other scripts on your Mac can text you too: drop a `.txt` file into `outbox/` and the
watcher sends it to the first number in `allowed_senders`, then renames it to `.sent`.

## Requirements

- macOS (uses `osascript`, JXA and `caffeinate`)
- Google Chrome, signed in to the Google account that owns the Google Voice number
- A US Google Voice number with "Forward messages to email" turned on
- Claude Code CLI installed and logged in (`claude --version` works)
- Python 3.8 or newer

## Setup

1. In Google Voice settings, under Messages, turn on "Forward messages to email" for the
   Gmail address of the same account.
2. In Chrome, turn on View > Developer > Allow JavaScript from Apple Events. The watcher
   cannot read or type into tabs without it.
3. Clone this repo and create your config:
   ```
   cp config.example.json config.json
   ```
   Fill in your phone number, the Google Voice number and the account email (see Config).
4. Text the Google Voice number once from your phone so a thread exists in Gmail.
5. Mark existing messages as already seen, so old texts are not run as new requests:
   ```
   python3 watcher.py --prime
   ```
   The first run triggers a macOS prompt to let your terminal control Google Chrome.
   Allow it (System Settings > Privacy & Security > Automation).
6. Start it:
   ```
   ./run.sh
   ```
   `run.sh` keeps the watcher alive with `caffeinate` and restarts it if it crashes. Run it
   in a terminal you leave open, or with `nohup ./run.sh &`.
7. Text the number. You should get "on it" back, then the answer.

To restart safely after editing the script, `touch restart.flag`. The watcher exits once it
is idle and `run.sh` starts the new code.

Run the offline tests (no Chrome, no config, no texts sent) with `python3 test_watcher.py`.

## Config

`config.json` sits next to `watcher.py` and is gitignored. Set `WATCHER_CONFIG` to use a
different path. The watcher exits with a clear message if the file is missing or a required
value is wrong.

| Key | Required | What it is |
| --- | --- | --- |
| `allowed_senders` | yes | Phone numbers allowed to text Claude. Replies and outbox texts go to the first one. |
| `gv_number` | yes | Your Google Voice number. Any format; it is normalized to 11 digits. |
| `google_account_email` | yes | The Google account signed in to Gmail and Google Voice. The watcher refuses to send from a Voice tab signed in as anyone else. |
| `reply_from_email` | no | From address for the Gmail fallback reply. Defaults to `google_account_email`. |
| `gmail_account_index`, `gv_account_index` | no | The `N` in `mail.google.com/mail/u/N` and `voice.google.com/u/N` if you are signed in to several Google accounts. Default 0. |
| `chrome_app`, `chrome_profile` | no | Chrome binary and the profile folder name used when the watcher has to open its tab. |
| `claude_path` | no | Path to the `claude` binary (`which claude`). |
| `claude_cwd` | no | Working directory for Claude. Default is your home folder. |
| `model`, `effort` | no | Passed to `claude --model` and `--effort`. |
| `permission_mode` | no | Passed to `claude --permission-mode`. See Safety. |
| `fallback_agy_path` | no | Optional second CLI to try when Claude reports it is out of usage. It must accept `-p`, `--output-format json`, `--effort` and `--conversation`. Leave `null` to disable. |
| `poll_seconds`, `settle_seconds`, `job_timeout_seconds` | no | Poll interval, batching window, and the per-job time limit. |
| `system_prompt_extra` | no | Your own lines appended to the built-in system prompt (house rules, tone, tools to prefer). |

## Safety

- Only numbers in `allowed_senders` can start a job. Everyone else is logged and ignored.
  Caller ID can be spoofed, so treat the allowlist as a filter, not as authentication.
- Claude runs with `--permission-mode` from your config, non-interactively. With `auto` it
  can act without asking you at the keyboard. Pick the mode that matches what you are
  comfortable with: `plan` or `manual` are the most conservative, `bypassPermissions` the
  least.
- The built-in system prompt tells Claude to ask before anything outward-facing or hard to
  undo (messaging other people, posting, buying, deleting, paying). It replies with what it
  would do and waits for you to text "yes". This is an instruction, not a hard guarantee.
- Everything runs on your own machine, in your own browser session. The bridge itself only
  talks to Gmail, Google Voice and the local Claude Code CLI. What Claude does from there
  depends on your Claude Code setup.
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
- It needs your Mac awake, Chrome open and signed in, and Claude Code logged in.
- Jobs run one at a time. Texts that arrive during a job are queued and folded into the
  next prompt.

## License

MIT. See LICENSE.
