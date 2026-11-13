#!/usr/bin/env python3
"""Text your coding agent (Claude Code by default, or Codex) from your phone through Google Voice.

Flow: you text your Google Voice number. Google Voice forwards the text to your Gmail
inbox. This script reads that inbox through a dedicated tab in your logged-in Chrome
(JXA -> JavaScript in the tab), runs the agent CLI (`claude -p`, `codex exec`, or any
command you set) as one process per text, several in parallel, and replies through the
Google Voice web app. If that fails it replies to the Google Voice email instead, which
Google Voice delivers back as a text in the same thread.

All personal values (phone numbers, account email, paths, model) live in config.json,
next to this file. Create it with `python3 watcher.py --init ...` (see README).

  python3 watcher.py --init --phone ... --gv-number ... --email ...   write config.json
  python3 watcher.py --check     check config, agent CLI and Chrome (no texts sent)
  python3 watcher.py --start     launch run.sh in its own Terminal window
  python3 watcher.py --prime     mark existing texts as seen (also done on first run)
  python3 watcher.py             run the watcher (run.sh does this and restarts it)
"""
import json, os, re, subprocess, sys, time, html, datetime, pathlib, threading, signal, hashlib
import shlex, shutil, tempfile, argparse

HOME = pathlib.Path.home()
DIR = pathlib.Path(__file__).resolve().parent
CONFIG_PATH = pathlib.Path(os.environ.get("WATCHER_CONFIG", DIR / "config.json"))
STATE = DIR / "state.json"
LOG = DIR / "watcher.log"
OUTBOX = DIR / "outbox"
MARK = "gvclaude=1"                      # marker in the dedicated Gmail tab URL
TAB = threading.RLock()                    # one Chrome-tab user at a time
SLOCK = threading.RLock()                  # state.json

# Filled in by configure() from config.json.
ALLOWED = []          # 10-digit numbers allowed to text Claude; replies go to the first
CLAUDE_GV = ""         # your Google Voice number as 11 digits, e.g. 15555550100
GV_ACCOUNT = ""        # Google account the GV and Gmail tabs must be signed in as
REPLY_FROM = ""        # From address for the Gmail fallback reply
GMAIL_U = 0            # account index in mail.google.com/mail/u/<n>
GV_U = 0               # account index in voice.google.com/u/<n>
TAB_URL = ""
CHROME_APP = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
CHROME_PROFILE = "Default"
CHROME_DIR = HOME / "Library/Application Support/Google/Chrome"   # Chrome's user data folder
AGENT = None           # {"kind": "claude"|"codex"|"custom", "argv": [...], "name": ...}
FALLBACK = None        # optional second agent, used only when the first is out of usage
AGENT_CWD = str(HOME)
MODEL = "opus"         # Claude only; for Codex put -m <model> in agent_cmd
EFFORT = "high"        # Claude only
PERMISSION_MODE = "auto"  # Claude only
MAX_PARALLEL = 6       # agent processes allowed to run at once
POLL = 4
SETTLE = 5             # wait for rapid-fire texts, then batch
JOB_TIMEOUT = 3600
SYSTEM = ""
AUTO_RESTART = True    # restart a frozen Chrome when nobody is using the Mac
RESTART_AFTER = 600    # Chrome frozen this long (seconds) ...
IDLE_NEEDED = 600      # ... and no keyboard/mouse input this long -> restart Chrome
AFTER_RESTART = None   # optional script to run after an auto-restart
ALERT_IMESSAGE = None  # optional iMessage handle for "Chrome is stuck" alerts
RESTORE_WINDOWS = True # reopen the windows that were open before an auto-restart

BASE_SYSTEM = """You are a coding agent running on the owner's Mac, reached by text message
through a Google Voice number. Your final answer is sent back to them as a text.
- Do the task fully and autonomously using your normal tools, skills and memory.
- Your final answer: plain text, no markdown, no headers, short (aim under 600
  characters for a single ask). Lead with the result.
- Before anything outward-facing or hard to undo (texting or emailing someone else,
  posting, buying, deleting, paying), do NOT do it: reply with exactly what you
  would do and ask them to text back "yes".
- If they text "yes" to a pending question, proceed with what you proposed.
- Always give enough context that the text stands alone (who, what, when, numbers
  with what they cover). If you need something from them, ask one clear question.
- If their texts ask for several things, do every one of them. Reply with a bulleted
  report, one line per item: done (with the result), or blocked (and why).
- For several independent items (a checklist, a list of people, multiple lookups),
  run them in parallel (sub-agents or parallel tool calls) if your tools allow it,
  then merge the results into the report.
- Long answers are fine when there are many items: they go out as several texts.
- Line breaks reach the phone. Format for a phone screen: one item per line,
  short lines, a blank line between sections. Never a wall of text.
- Lists over text: bulleted with "•", one item per line, "Name - short detail". No group
  headers, no parentheticals, no comma-run blobs of names.
- Chrome automation: a JavaScript popup (alert, confirm, "Leave site?") left open in any tab
  freezes ALL Chrome automation, including this text line. Before a click that may open one
  (delete, end listing, leave page), stub them in that tab first, e.g.
  window.confirm=()=>true; window.alert=()=>{}; window.onbeforeunload=null, and close tabs
  you opened. If Chrome stops answering, say so in your reply instead of retrying.
"""

class ConfigError(Exception):
    pass

def _digits(v):
    return re.sub(r"\D", "", str(v or ""))

def _path(v):
    return str(pathlib.Path(os.path.expanduser(str(v))))

# GUI apps (Claude desktop, ChatGPT desktop, cmux) and launchd often start processes with a
# short PATH, so CLIs installed by Homebrew, npm or the Claude installer are also looked for here.
EXTRA_PATH = ["~/.local/bin", "/opt/homebrew/bin", "/usr/local/bin", "~/.claude/local",
              "~/.npm-global/bin", "~/.bun/bin", "~/.volta/bin", "~/bin"]

def search_path():
    parts = os.environ.get("PATH", "").split(os.pathsep) + [_path(x) for x in EXTRA_PATH]
    seen, out = set(), []
    for x in parts:
        if x and x not in seen: seen.add(x); out.append(x)
    return os.pathsep.join(out)

def which(exe):
    if "/" in exe:
        exe = _path(exe)
        return exe if os.path.isfile(exe) and os.access(exe, os.X_OK) else None
    return shutil.which(exe, path=search_path())

def resolve_agent(spec, claude_path=None):
    """Turn "claude", "claude -p --model sonnet", "codex exec -m gpt-5" or any other command
    into {"kind", "argv", "name"}. Raises ConfigError if the binary cannot be found."""
    if not spec: spec = "claude"
    argv = list(spec) if isinstance(spec, list) else shlex.split(str(spec))
    if not argv: argv = ["claude"]
    name = os.path.basename(argv[0])
    kind = "claude" if name == "claude" else "codex" if name == "codex" else "custom"
    if kind == "claude" and claude_path and argv[0] == "claude":
        argv[0] = claude_path                          # old "claude_path" key still works
    exe = which(argv[0])
    if not exe:
        raise ConfigError("Could not find %r on PATH or in %s. Install it, or set \"agent_cmd\" in "
                          "config.json (or the AGENT_CMD env var) to its full path, e.g. "
                          "\"/opt/homebrew/bin/codex exec\" (`which codex` prints it)."
                          % (argv[0], ", ".join(EXTRA_PATH)))
    argv[0] = exe
    if kind == "claude":
        argv = [argv[0]] + [a for a in argv[1:] if a not in ("-p", "--print")]
    if kind == "codex" and "exec" not in argv[1:2]:
        argv.insert(1, "exec")
    return {"kind": kind, "argv": argv, "name": "Codex" if kind == "codex" else
            "Claude" if kind == "claude" else name}

def configure(cfg):
    """Validate a config dict and set the module globals from it."""
    global ALLOWED, CLAUDE_GV, GV_ACCOUNT, REPLY_FROM, GMAIL_U, GV_U, TAB_URL, CHROME_APP, \
        CHROME_PROFILE, AGENT, FALLBACK, AGENT_CWD, MODEL, EFFORT, PERMISSION_MODE, POLL, SETTLE, \
        JOB_TIMEOUT, SYSTEM, MAX_PARALLEL, AUTO_RESTART, RESTART_AFTER, IDLE_NEEDED, AFTER_RESTART, \
        ALERT_IMESSAGE, RESTORE_WINDOWS, CHROME_DIR
    errs = []
    allowed = [_digits(x)[-10:] for x in cfg.get("allowed_senders") or []]
    if not allowed or any(len(x) != 10 for x in allowed):
        errs.append('"allowed_senders" must be a non-empty list of 10-digit US numbers')
    gv = _digits(cfg.get("gv_number"))
    if len(gv) == 10: gv = "1" + gv
    if len(gv) != 11:
        errs.append('"gv_number" must be your Google Voice number, e.g. "+1 555 555 0100"')
    acct = str(cfg.get("google_account_email") or "").strip()
    if "@" not in acct:
        errs.append('"google_account_email" must be the Google account signed in to Gmail and Google Voice')
    agent = fallback = None
    try:
        agent = resolve_agent(os.environ.get("AGENT_CMD") or cfg.get("agent_cmd"), cfg.get("claude_path"))
        if cfg.get("fallback_agent_cmd"):
            fallback = resolve_agent(cfg["fallback_agent_cmd"])
    except ConfigError as ex:
        errs.append(str(ex))
    try:
        maxp = int(cfg.get("max_parallel", 6))
        if maxp < 1: raise ValueError
    except (TypeError, ValueError):
        errs.append('"max_parallel" must be a whole number, 1 or more'); maxp = 6
    secs = {}
    for k in ("restart_after_seconds", "idle_needed_seconds"):
        try:
            secs[k] = float(cfg.get(k) if cfg.get(k) is not None else 600)
            if secs[k] < 0: raise ValueError
        except (TypeError, ValueError):
            errs.append('"%s" must be a number of seconds' % k); secs[k] = 600
    if errs:
        raise ConfigError("Bad config:\n  " + "\n  ".join(errs))
    ALLOWED = allowed
    CLAUDE_GV = gv
    GV_ACCOUNT = acct
    REPLY_FROM = str(cfg.get("reply_from_email") or acct).strip()
    GMAIL_U = int(cfg.get("gmail_account_index", 0))
    GV_U = int(cfg.get("gv_account_index", 0))
    TAB_URL = "https://mail.google.com/mail/u/%d/?%s#inbox" % (GMAIL_U, MARK)
    CHROME_APP = cfg.get("chrome_app") or CHROME_APP
    CHROME_PROFILE = cfg.get("chrome_profile") or "Default"
    CHROME_DIR = pathlib.Path(_path(cfg.get("chrome_user_data_dir") or "~/Library/Application Support/Google/Chrome"))
    AGENT, FALLBACK = agent, fallback
    AGENT_CWD = _path(cfg.get("agent_cwd") or cfg.get("claude_cwd") or "~")
    MODEL = cfg.get("model") or "opus"
    EFFORT = cfg.get("effort") or "high"
    PERMISSION_MODE = cfg.get("permission_mode") or "auto"
    POLL = float(cfg.get("poll_seconds", 4))
    SETTLE = float(cfg.get("settle_seconds", 5))
    JOB_TIMEOUT = int(cfg.get("job_timeout_seconds", 3600))
    MAX_PARALLEL = maxp
    AUTO_RESTART = cfg.get("auto_restart_chrome", True) is not False
    RESTART_AFTER = secs["restart_after_seconds"]
    IDLE_NEEDED = secs["idle_needed_seconds"]
    AFTER_RESTART = _path(cfg["after_chrome_restart"]) if cfg.get("after_chrome_restart") else None
    ALERT_IMESSAGE = str(cfg.get("alert_imessage") or "").strip() or None
    RESTORE_WINDOWS = cfg.get("restore_windows_after_restart", True) is not False
    extra = (cfg.get("system_prompt_extra") or "").strip()
    SYSTEM = BASE_SYSTEM + (extra + "\n" if extra else "")

def load_config(path=None):
    path = pathlib.Path(path or CONFIG_PATH)
    if not path.exists():
        raise ConfigError("No config at %s. Copy config.example.json to config.json and fill it in." % path)
    try:
        cfg = json.loads(path.read_text())
    except ValueError as ex:
        raise ConfigError("%s is not valid JSON: %s" % (path, ex))
    configure(cfg)
    return cfg

def log(*a):
    line = datetime.datetime.now().strftime("%m-%d %H:%M:%S ") + " ".join(str(x) for x in a)
    with open(LOG, "a") as f: f.write(line + "\n")
    print(line, flush=True)

def load():
    try: return json.loads(STATE.read_text())
    except Exception: return {"done": [], "session": None}

def save(s):
  with SLOCK:
    s["done"] = s.get("done", [])[-500:]
    STATE.write_text(json.dumps(s, indent=1))

def chrome_pid():
    """PID of your real Chrome, never a headless or test instance. A stray headless Chrome
    can capture `tell application "Google Chrome"`, so we address Chrome by PID instead."""
    out = subprocess.run(["ps", "-axo", "pid=,command="], capture_output=True, text=True).stdout
    for line in out.splitlines():
        pid, _, cmd = line.strip().partition(" ")
        if cmd.startswith(CHROME_APP) and \
           "--headless" not in cmd and "--user-data-dir" not in cmd and "--type=" not in cmd:
            return int(pid)
    raise RuntimeError("real Chrome not running")

def jxa(body):
    """Run JXA against the real Chrome as `c`. Returns stdout."""
    script = "var c=Application(%d);\n%s" % (chrome_pid(), body)
    r = subprocess.run(["osascript", "-l", "JavaScript", "-e", script], capture_output=True, text=True, timeout=60)
    if r.returncode != 0: raise RuntimeError(r.stderr.strip())
    return r.stdout.strip()

FIND = r"""var out=[];var ws=c.windows();for(var i=0;i<ws.length;i++){var ts=ws[i].tabs();for(var j=0;j<ts.length;j++){
try{if(ts[j].url().indexOf(%s)>=0)out.push(ws[i].id()+':'+ts[j].id());}catch(e){}}}out.join(',')"""

def find_tabs(mark):
    out = jxa(FIND % json.dumps(mark))
    return [x for x in out.split(",") if x]

def find_tab(mark=MARK):
    t = find_tabs(mark)
    return t[0] if t else None

def dedupe_tabs(mark):
    """Close extra copies of a marked tab (keep the first)."""
    for t in find_tabs(mark)[1:]:
        w, tid = t.split(":")
        try: jxa("c.windows.byId(%s).tabs.byId(%s).close()" % (w, tid))
        except Exception: pass

# Frozen Chrome. A JavaScript popup (alert, confirm, "Leave site?") left open in any tab blocks
# every Apple Events call to Chrome, so the JXA lookup times out. A timeout does not mean the
# tab is gone: opening a new one each poll piles up dozens of Gmail tabs. So while Chrome is
# frozen the watcher opens nothing. After RESTART_AFTER seconds it restarts Chrome if nobody is
# using the Mac, or sends one alert if someone is.
STUCK = {"since": 0, "alerted": False, "restarted": 0}
AFTER_LOG = DIR / "after-restart.log"
LAST_HEX = [None]      # newest GV thread seen, for the email fallback of the restart notice

def _sleep(s):         # tests stub this
    time.sleep(s)

def hid_idle():
    """Seconds since anyone touched this Mac's keyboard, mouse or trackpad (0 if unknown)."""
    try:
        out = subprocess.run(["ioreg", "-c", "IOHIDSystem"], capture_output=True, text=True, timeout=10).stdout
        m = re.search(r'"HIDIdleTime" = (\d+)', out)
        return int(m.group(1)) / 1e9 if m else 0
    except Exception:
        return 0

MAX_RESTORE_TABS = 40

def chrome_profiles():
    """Profile folder names from Chrome's Local State (just chrome_profile if unreadable)."""
    try: return list(json.loads((CHROME_DIR / "Local State").read_text())["profile"]["info_cache"])
    except Exception: return [CHROME_PROFILE]

def reopens_last_session(prof):
    """True if this profile's "On startup" setting is "Continue where you left off", in which
    case Chrome brings the windows back by itself."""
    try: return json.loads((CHROME_DIR / prof / "Preferences").read_text())["session"]["restore_on_startup"] == 1
    except Exception: return False

def restore_windows(before):
    """Chrome set to open the New Tab page at startup comes back from a restart with none of
    its windows. Rebuild each profile's windows from the newest Sessions/Session_* file saved
    before the quit, leaving out the watcher's own tabs, repeated URLs and New Tab pages.
    Returns how many windows were reopened."""
    import snss
    n = 0
    for prof in chrome_profiles():
        if reopens_last_session(prof): continue
        try:
            files = [f for f in (CHROME_DIR / prof / "Sessions").glob("Session_*") if f.stat().st_mtime <= before + 2]
        except OSError:
            continue
        if not files: continue
        newest = max(files, key=lambda f: f.stat().st_mtime)
        try: windows = snss.parse(str(newest))
        except Exception as ex:
            log("could not read %s: %s" % (newest.name, ex)); continue
        for urls in windows:
            keep = []
            for u in urls:
                if not u or MARK in u or "gvclaudesend=" in u or u in keep or u.startswith("chrome://newtab"): continue
                keep.append(u)
            if not keep: continue
            subprocess.Popen([CHROME_APP, "--profile-directory=" + prof, "--new-window"] + keep[:MAX_RESTORE_TABS],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
            log("restored a %s window with %d tabs" % (prof, min(len(keep), MAX_RESTORE_TABS)))
            n += 1
            _sleep(4)
    return n

def _hhmm(t):
    return time.strftime("%-I:%M %p", time.localtime(t))

def restart_chrome():
    """Quit the frozen Chrome (SIGTERM, so it saves its session), force it only if it ignores
    that, relaunch it, reopen the windows it had, wait until it answers, run the optional hook,
    and text the owner through the normal reply path."""
    pid = chrome_pid()
    since = STUCK["since"]
    log("auto-restarting Chrome (pid %d), frozen since %s" % (pid, _hhmm(since)))
    STUCK["restarted"] = time.time()
    os.kill(pid, signal.SIGTERM)
    for _ in range(45):
        _sleep(1)
        try: os.kill(pid, 0)
        except OSError: break
    else:
        log("Chrome ignored SIGTERM, forcing"); os.kill(pid, signal.SIGKILL); _sleep(3)
    t_quit = time.time()
    # Launch the binary with a profile, not `open -a`: that can stop at the profile picker.
    subprocess.Popen([CHROME_APP, "--profile-directory=" + CHROME_PROFILE],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    _sleep(8)
    if RESTORE_WINDOWS:
        try: restore_windows(t_quit)
        except Exception as ex: log("window restore failed:", ex)
    for _ in range(24):                                   # up to 2 min to answer again
        _sleep(5)
        try: jxa("c.windows().length"); break
        except Exception: pass
    else:
        log("Chrome still not answering after restart"); return False
    STUCK.update(since=0, alerted=False)
    log("Chrome back after restart")
    if AFTER_RESTART:
        if os.path.exists(AFTER_RESTART):
            argv = [AFTER_RESTART] if os.access(AFTER_RESTART, os.X_OK) else ["/bin/bash", AFTER_RESTART]
            with open(AFTER_LOG, "a") as out:
                subprocess.Popen(argv, stdout=out, stderr=subprocess.STDOUT, start_new_session=True)
        else:
            log("after_chrome_restart hook not found:", AFTER_RESTART)
    _sleep(25)                                            # let Chrome and the hook settle
    try:
        send_reply(LAST_HEX[0], "Heads up: Chrome stopped answering at %s, so I restarted it at %s. "
                   "The text line is back up. If a text got no reply, send it again."
                   % (_hhmm(since), _hhmm(time.time())))
    except Exception as ex: log("restart notice failed:", ex)
    return True

def stuck_alert(msg):
    """Chrome is frozen, so the Google Voice tab can't send. Use iMessage if configured."""
    if not ALERT_IMESSAGE:
        log("ALERT (set alert_imessage in config.json to get this as an iMessage):", msg); return
    try:
        subprocess.run(["osascript", "-e", 'tell application "Messages" to send %s to participant %s of '
                        '(1st account whose service type = iMessage)' % (json.dumps(msg), json.dumps(ALERT_IMESSAGE))],
                       capture_output=True, timeout=90)
        log("sent Chrome-stuck alert via iMessage")
    except Exception as ex: log("stuck alert failed:", ex)

def chrome_stuck():
    """Called when a JXA call to Chrome times out. Never opens tabs. Always raises."""
    now = time.time()
    if not STUCK["since"]:
        STUCK["since"] = now; log("Chrome not answering automation")
    frozen = now - STUCK["since"]
    if frozen > RESTART_AFTER:
        recent = now - STUCK["restarted"] < 1800
        if AUTO_RESTART and not recent and hid_idle() > IDLE_NEEDED:
            try: restart_chrome()
            except Exception as ex: log("auto-restart failed:", ex)
            raise RuntimeError("Chrome was restarted")
        if not STUCK["alerted"]:
            STUCK["alerted"] = True
            why = ("Auto-restart is off." if not AUTO_RESTART else
                   "I already restarted it in the last 30 minutes." if recent else
                   "Someone is using the Mac, so I didn't restart it.")
            stuck_alert("Text line is down: Chrome stopped answering automation at %s. %s Quit and "
                        "reopen Chrome and it comes back on its own." % (_hhmm(STUCK["since"]), why))
    _sleep(30)
    raise RuntimeError("Chrome not answering automation")

def ensure_tab(mark=MARK, url=None):
    url = url or TAB_URL
    for _ in range(3):
        try:
            t = find_tab(mark)
            if STUCK["since"]:
                log("Chrome answering again"); STUCK.update(since=0, alerted=False)
            if t:
                dedupe_tabs(mark)
                return t
        except subprocess.TimeoutExpired:
            chrome_stuck()                   # raises: a frozen Chrome gets no new tabs
        except Exception:
            pass
        time.sleep(1)
    log("opening tab", mark)
    subprocess.Popen([CHROME_APP, "--profile-directory=" + CHROME_PROFILE, url],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(12)
    return find_tab(mark)

def js(code, mark=MARK, url=None):
  with TAB:
    t = ensure_tab(mark, url)
    if not t: raise RuntimeError("no tab " + mark)
    for attempt in (1, 2):
        w, tid = t.split(":")
        try:
            return jxa("var r=c.windows.byId(%s).tabs.byId(%s).execute({javascript:%s});(r===undefined||r===null)?'':String(r)"
                       % (w, tid, json.dumps(code, ensure_ascii=False)))
        except RuntimeError:
            if attempt == 2: raise
            t = ensure_tab(mark, url)
            if not t: raise

# Replies go out through the Google Voice web app: it keeps line breaks. Emailed replies
# lose every line break (GV joins them with spaces) and everything after a blank line.
def gv_mark(digits): return "gvclaudesend=" + digits
def gv_url(digits): return "https://voice.google.com/u/%d/messages?itemId=t.%%2B1%s&%s" % (GV_U, digits, gv_mark(digits))
def gvjs(digits, code): return js(code, gv_mark(digits), gv_url(digits))

GV_TA = "[...document.querySelectorAll('textarea')].find(e=>e.getAttribute('placeholder')==='Type a message')"
GV_SEND = "[...document.querySelectorAll('button')].find(e=>e.getAttribute('aria-label')==='Send message')"

GV_FRESH = {}   # digits -> time of last reload

def gv_reload(digits):
    gvjs(digits, "location.reload()"); time.sleep(3)
    wait_gv(digits, "(%s)?'1':''" % GV_TA, 20)
    GV_FRESH[digits] = time.time()

def send_gv(digits, text):
    """The GV web app goes stale after sitting idle: Send clicks stop registering until a
    reload. Reload if idle >10 min; on a failed send, reload and retry once."""
    with TAB:
        if time.time() - GV_FRESH.get(digits, 0) > 600:
            gv_reload(digits)
        try:
            _send_gv(digits, text)
        except Exception as ex:
            log("GV send retry after reload:", ex)
            gv_reload(digits)
            _send_gv(digits, text)
        GV_FRESH[digits] = time.time()

def _send_gv(digits, text):
    with TAB:
        acct = gvjs(digits, "(document.querySelector('[aria-label*=\"Google Account\"]')||{getAttribute:()=>''}).getAttribute('aria-label')")
        if GV_ACCOUNT not in acct: raise RuntimeError("GV tab on wrong account: %r" % acct[:80])
        if not wait_gv(digits, "(%s)?'1':''" % GV_TA, 15): raise RuntimeError("no GV composer")
        gvjs(digits, "(function(){var ta=%s;ta.focus();ta.select();document.execCommand('insertText',false,%s);"
             "['keydown','keyup','change'].forEach(t=>ta.dispatchEvent(new Event(t,{bubbles:true})))})()" % (GV_TA, json.dumps(text)))
        wait_gv(digits, "(function(){var b=%s;return b&&!b.disabled?'1':''})()" % GV_SEND, 5)
        r = gvjs(digits, "(function(){var ta=%s;var b=%s;return (b&&!b.disabled?'ready':'disabled')+'|'+ta.value.length})()" % (GV_TA, GV_SEND))
        if not r.startswith("ready|") or int(r.split("|")[1]) != len(text.encode("utf-16-le")) // 2:  # JS counts UTF-16 units
            gvjs(digits, "(function(){var ta=%s;ta.select();document.execCommand('delete')})()" % GV_TA)
            raise RuntimeError("GV composer not ready: " + r)
        gvjs(digits, "(function(){var b=%s;var r=b.getBoundingClientRect();['mouseover','mousedown','mouseup','click'].forEach(t=>b.dispatchEvent(new MouseEvent(t,{bubbles:true,cancelable:true,view:window,clientX:r.left+5,clientY:r.top+5})));})()" % GV_SEND)
        if not wait_gv(digits, "(%s).value===''?'1':''" % GV_TA, 10):
            raise RuntimeError("GV send did not clear composer")
        log("sent via GV: %r" % text[:80])

def wait_gv(digits, cond, timeout):
    end = time.time() + timeout
    while time.time() < end:
        if gvjs(digits, cond) == "1": return True
        time.sleep(0.4)
    return False

def feed_js():
    return r"""(function(){var x=new XMLHttpRequest();x.open('GET','/mail/u/%d/feed/atom/google-voice',false);x.send();
var out=[],re=/<entry>([\s\S]*?)<\/entry>/g,m;while((m=re.exec(x.responseText))){var e=m[1];
function g(r){var k=e.match(r);return k?k[1]:''}
out.push({title:g(/<title>([\s\S]*?)<\/title>/),email:g(/<email>([\s\S]*?)<\/email>/),
id:g(/<id>tag:gmail\.google\.com,2004:(\d+)<\/id>/),hex:g(/message_id=([0-9a-f]+)/)});}
return JSON.stringify(out);})()""" % GMAIL_U

FIRE = r"""function fire(e,ts){var r=e.getBoundingClientRect();ts.forEach(function(t){e.dispatchEvent(new MouseEvent(t,{bubbles:true,cancelable:true,view:window,clientX:r.left+5,clientY:r.top+5,button:0}));});}"""
CLICK = ['mouseover','mousedown','mouseup','click']
MENU_CLICK = ['mouseover','mousemove','mousedown','mouseup','click']

def wait_js(cond, timeout=15, step=0.4):
    """Poll a JS expression until it returns a non-empty, non-'0' string."""
    end = time.time() + timeout
    while time.time() < end:
        r = js(cond)
        if r and r not in ("0", "false", "null"): return r
        time.sleep(step)
    return ""

REPLY_BTN = "[...document.querySelectorAll('[role=button],[role=link]')].find(e=>(e.innerText||'').trim()==='Reply')"
POPOUT_BTN = "[...document.querySelectorAll('[role=button]')].find(e=>e.getAttribute('aria-label')==='Pop out reply')"
BODY = "(function(){var bs=[...document.querySelectorAll('div[aria-label=\"Message Body\"][contenteditable=\"true\"]')];return bs[bs.length-1]})()"

def send_one(hexid, text):
    """Fallback: reply inside the GV notification thread in Gmail, sent From REPLY_FROM,
    so Google Voice delivers it as a text."""
    with TAB:
        js("location.hash='#all/%s'" % hexid); time.sleep(1.5)
        if not wait_js("(%s)?'1':''" % REPLY_BTN): raise RuntimeError("no reply button")
        js("(function(){"+FIRE+"fire(%s,%s)})()" % (REPLY_BTN, json.dumps(CLICK)))
        if not wait_js("(%s)?'1':''" % POPOUT_BTN, 8): raise RuntimeError("no popout")
        js("(function(){"+FIRE+"fire(%s,%s)})()" % (POPOUT_BTN, json.dumps(CLICK)))
        if not wait_js("(%s)?'1':''" % BODY, 8): raise RuntimeError("no body")
        time.sleep(0.8)
        fr = js("(function(){var f=document.querySelector('[name=\"from\"]');return f?f.value:''})()")
        if fr != REPLY_FROM:
            js("(function(){"+FIRE+r"""var b=[...document.querySelectorAll('[role=button]')].find(e=>/<[^>]+@[^>]+>/.test(e.innerText||'')&&!/@txt/.test(e.innerText||''));if(b)fire(b,%s);})()""" % json.dumps(CLICK))
            wait_js("[...document.querySelectorAll('[role=menuitem]')].some(e=>/%s/.test(e.innerText||''))?'1':''" % re.escape(REPLY_FROM), 5)
            js("(function(){"+FIRE+r"""var o=[...document.querySelectorAll('[role=menuitem]')].find(e=>/%s/.test(e.innerText||''));if(o)fire(o,%s);})()""" % (re.escape(REPLY_FROM), json.dumps(MENU_CLICK)))
            time.sleep(0.6)
        r = js("(function(){var b=%s;if(!b) return 'no body';b.focus();document.execCommand('selectAll',false);document.execCommand('insertText',false,%s);"
               "var f=document.querySelector('[name=\"from\"]');return 'from='+(f?f.value:'?');})()" % (BODY, json.dumps(text)))
        if ("from=" + REPLY_FROM) != r:
            js("location.hash='#inbox'")
            raise RuntimeError("wrong From (%s) - not sending" % r)
        time.sleep(0.5)
        r = js("(function(){"+FIRE+r"""var s=[...document.querySelectorAll('[role=button]')].filter(e=>/^Send/.test(e.getAttribute('aria-label')||e.getAttribute('data-tooltip')||''));
var b=s[s.length-1];if(!b) return 'no send';fire(b,%s);return 'ok';})()""" % json.dumps(CLICK))
        if r != "ok": raise RuntimeError("send: " + r)
        ok = "sent" if wait_js("/Message sent/.test(document.body.innerText)?'1':''", 12) else "unconfirmed"
        log("sent (%s): %r" % (ok, text[:80]))
        js("location.hash='#inbox'")
        return ok

def split_text(text, n=1400):
    parts = []
    while len(text) > n:
        cut = text.rfind("\n", 0, n)
        if cut < n // 2: cut = text.rfind(". ", 0, n) + 1
        if cut < n // 2: cut = n
        parts.append(text[:cut].strip()); text = text[cut:].strip()
    if text: parts.append(text)
    return parts[:4]

def sms_safe(text):
    """Google Voice drops everything after the first blank line of an emailed reply, so
    long reports get cut to one paragraph. Single newlines survive."""
    text = text.replace("\r", "")
    text = re.sub(r"\n[ \t]*\n+", "\n", text)
    return text.strip()

def send_reply(hexid, text, digits=None):
    digits = digits or ALLOWED[0]
    try:
        for p in split_text(text.replace("\r", "").strip()):
            send_gv(digits, p)
        return
    except Exception as ex:
        log("GV web send failed, falling back to email:", ex)
    parts = split_text(sms_safe(text))
    for k, p in enumerate(parts):
        for attempt in (1, 2):
            try: send_one(hexid, p); break
            except Exception as ex:
                log("send failed (try %d):" % attempt, ex)
                if attempt == 2: raise
                time.sleep(3)

LIMIT_RE = re.compile(r"usage limit|limit reached|out of (extra )?usage|rate.?limit|quota|429|credit balance", re.I)

# One agent process per incoming text, several at once. Each entry:
# {"proc", "prompt", "start", "stopped", "hex", "pinged"}
RUNNING = {}
RLOCK = threading.RLock()
NEXT_ID = [0]

def new_job(prompt="", hexid=None):
    return {"proc": None, "prompt": prompt, "start": time.time(), "stopped": False,
            "hex": hexid, "pinged": False}

def child_env():
    """Environment for the agent CLI: a PATH that finds node/homebrew tools even when the
    watcher was started from a GUI app, minus the markers an outer Claude Code session sets
    (so a watcher launched from inside Claude Code can still run `claude -p`)."""
    env = dict(os.environ)
    env["PATH"] = search_path()
    for k in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT"):
        env.pop(k, None)
    return env

def run(cmd, job):
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                         cwd=AGENT_CWD, env=child_env(), start_new_session=True)
    job["proc"] = p
    if job["stopped"]:                   # "stop" arrived while the process was starting
        try: os.killpg(p.pid, signal.SIGTERM)
        except Exception: pass
    try: out, err = p.communicate(timeout=JOB_TIMEOUT)
    except subprocess.TimeoutExpired:
        os.killpg(p.pid, signal.SIGTERM); out, err = p.communicate()
    job["proc"] = None
    return p.returncode, out or "", err or ""

def _has(argv, *flags):
    return any(a == f or a.startswith(f + "=") for a in argv for f in flags)

def run_claude(agent, sessions, key, prompt, job):
    """`claude -p ... --output-format json`. Each job forks the last finished conversation
    (`--resume <id> --fork-session`), so it knows the recent back-and-forth but runs
    independently of the other jobs. The newest finisher's session becomes the next base."""
    argv = agent["argv"]
    cmd = argv + ["-p", prompt, "--output-format", "json"]
    if not _has(argv, "--permission-mode", "--dangerously-skip-permissions"):
        cmd += ["--permission-mode", PERMISSION_MODE]
    if not _has(argv, "--model"): cmd += ["--model", MODEL]
    if not _has(argv, "--effort"): cmd += ["--effort", EFFORT]
    cmd += ["--append-system-prompt", SYSTEM]
    with SLOCK: base = sessions.get(key)
    if base: cmd += ["--resume", base, "--fork-session"]
    rc, out, err = run(cmd, job)
    j = None
    try: j = json.loads(out)
    except Exception: pass
    if not isinstance(j, dict): j = None
    if j and j.get("session_id"):
        with SLOCK: sessions[key] = j["session_id"]
    text = ((j or {}).get("result") or "").strip()
    failed = bool((j is None) or j.get("is_error") or rc != 0)
    if failed and base and "resume" in err.lower():
        with SLOCK:
            if sessions.get(key) == base: sessions[key] = None
    return text, failed, (text + " " + err + " " + (out if j is None else "")).strip()

HELD = {}   # session key -> job count holding that Codex thread

def run_codex(agent, sessions, key, prompt, job):
    """`codex exec --json -o <file>`; the first run starts a thread, later runs use
    `codex exec resume <thread_id>`. Two processes must not write one Codex thread at once,
    and the watcher does not fork Codex threads, so a job that starts while another job holds
    the thread starts a fresh thread (without the earlier conversation) and that thread is
    dropped when it ends. Codex has no system-prompt flag, so the house rules go at the top
    of the first message of each thread."""
    argv = agent["argv"]
    with SLOCK:
        owner = not HELD.get(key)
        sid = sessions.get(key) if owner else None
        if owner: HELD[key] = 1
    fd, last = tempfile.mkstemp(prefix="instinct-", suffix=".txt"); os.close(fd)
    cmd = list(argv)
    if not _has(argv, "--skip-git-repo-check"): cmd += ["--skip-git-repo-check"]
    if not _has(argv, "--sandbox", "-s", "--dangerously-bypass-approvals-and-sandbox"):
        cmd += ["--sandbox", "workspace-write"]
    cmd += ["--json", "-o", last]
    cmd += ["resume", sid, prompt] if sid else [SYSTEM + "\n---\n" + prompt]
    try:
        rc, out, err = run(cmd, job)
        text = pathlib.Path(last).read_text().strip() if os.path.exists(last) else ""
    finally:
        try: os.unlink(last)
        except OSError: pass
        if owner:
            with SLOCK: HELD.pop(key, None)
    errors = []
    for line in out.splitlines():
        try: e = json.loads(line)
        except Exception: continue
        if not isinstance(e, dict): continue
        if e.get("type") == "thread.started" and e.get("thread_id"):
            if owner:
                with SLOCK: sessions[key] = e["thread_id"]
        elif e.get("type") == "error": errors.append(str(e.get("message", "")))
        elif e.get("type") == "turn.failed": errors.append(str((e.get("error") or {}).get("message", "")))
    failed = rc != 0 or (not text and bool(errors))
    blob = (" ".join(errors) + " " + err).strip()
    if failed and sid and re.search(r"resume|not found|no (such )?(session|thread)", blob, re.I):
        with SLOCK:
            if sessions.get(key) == sid: sessions[key] = None
    return text, failed, blob or out[-300:]

def run_custom(agent, sessions, key, prompt, job):
    """Any other CLI: the prompt (with the house rules on top) replaces a {prompt} argument,
    or is added as the last argument. Its stdout is the answer. No session memory, so
    parallel jobs need nothing special."""
    full = SYSTEM + "\n---\n" + prompt
    argv = agent["argv"]
    cmd = [a.replace("{prompt}", full) for a in argv] if any("{prompt}" in a for a in argv) else argv + [full]
    rc, out, err = run(cmd, job)
    text = out.strip()
    return text, rc != 0, (err + " " + text).strip()

RUNNERS = {"claude": run_claude, "codex": run_codex, "custom": run_custom}

def run_agent(agent, role, state, prompt, job):
    with SLOCK: sessions = state.setdefault("sessions", {})
    return RUNNERS[agent["kind"]](agent, sessions, role + ":" + agent["kind"], prompt, job)

def run_job(state, prompt, job=None):
    """Run the prompt through the agent. Returns the reply text, or None if stopped."""
    job = job if job is not None else new_job(prompt)
    text, failed, blob = run_agent(AGENT, "primary", state, prompt, job)
    if job["stopped"]: return None
    name = AGENT["name"]
    if failed and LIMIT_RE.search(blob):
        if FALLBACK:
            log("%s out of usage -> falling back to %s" % (name, FALLBACK["name"]))
            alt, alt_failed, alt_blob = run_agent(FALLBACK, "fallback", state, prompt, job)
            if job["stopped"]: return None
            if alt and not alt_failed: return "(via %s) %s" % (FALLBACK["name"], alt)
            return "%s is out of usage and %s failed too: %s" % (name, FALLBACK["name"], alt_blob[-200:])
        return "%s is out of usage right now. Try again later." % name
    if failed:
        return "Hit an error running that: " + (text or blob)[-300:]
    return text or "Done (no text output)."

def others(jid):
    """Texts other jobs are still working on."""
    with RLOCK:
        return [j["prompt"][:160] for k, j in RUNNING.items() if k != jid]

def build_prompt(jid, body):
    par = others(jid)
    note = ""
    if par:
        note = ("\n\n[Other agents are working on these earlier texts in parallel right now. Do only "
                "what this text asks; don't redo their work:]\n" + "\n".join("- " + x for x in par))
    return "[Text(s) from the owner, %s]\n" % datetime.datetime.now().strftime("%a %b %-d %-I:%M %p") + body + note

def job_thread(state, jid, body, hexid):
    with RLOCK: job = RUNNING[jid]
    try:
        answer = run_job(state, build_prompt(jid, body), job); save(state)
        if answer is None:
            log("job %d stopped" % jid); return
        log("REPLY #%d:" % jid, repr(answer[:300]))
        send_reply(hexid, answer)
    except Exception as ex:
        log("job %d error:" % jid, ex)
        try: send_reply(hexid, "Something broke on my end: %s" % str(ex)[:200])
        except Exception: pass
    finally:
        with RLOCK: RUNNING.pop(jid, None)

def start_job(state, body, hexid):
    with RLOCK:
        NEXT_ID[0] += 1; jid = NEXT_ID[0]
        RUNNING[jid] = new_job(body, hexid)
    threading.Thread(target=job_thread, args=(state, jid, body, hexid), daemon=True).start()
    return jid

def stop_all():
    """Kill every running job. Returns how many there were."""
    with RLOCK: jobs = list(RUNNING.values())
    for j in jobs:
        j["stopped"] = True
        p = j["proc"]
        if p:
            try: os.killpg(p.pid, signal.SIGTERM)
            except Exception: pass
    return len(jobs)

def idle():
    with RLOCK: return not RUNNING

def plural(n, word):
    return "%d %s%s" % (n, word, "" if n == 1 else "s")

def control(body, hexid):
    """Text commands handled by the watcher itself. Returns True if handled."""
    c = body.strip().lower().rstrip(".!")
    if c in ("stop", "cancel", "kill it", "stop it", "nevermind", "never mind", "stop all"):
        n = stop_all()
        send_reply(hexid, ("Stopped %s." % plural(n, "task")) if n else "Nothing running.")
        return True
    if c in ("status", "?", "you there", "you there?"):
        with RLOCK: jobs = sorted(RUNNING.values(), key=lambda j: j["start"])
        if jobs:
            lines = ["%d running:" % len(jobs)]
            for j in jobs:
                lines.append("• %d min - %s" % (int((time.time() - j["start"]) / 60), j["prompt"][:120]))
            msg = "\n".join(lines)
        else: msg = "Here. Nothing running."
        send_reply(hexid, msg)
        return True
    return False

HDR = re.compile(r"^(.*?)<([\w.+-]+@[\w.-]+)>(\w{3}, \w{3} \d{1,2}, \d{4} at \d{1,2}:\d{2}\s?[AP]M)To:.*$", re.M)

def pt_js(hx):
    return r"""(function(){var ik=(window.GLOBALS&&GLOBALS[9])||'';var x=new XMLHttpRequest();
x.open('GET','/mail/u/%d/?ik='+ik+'&view=pt&search=all&th=%s',false);x.send();return x.status==200?x.responseText:'';})()""" % (GMAIL_U, hx)

def thread_msgs(hx):
    """All messages in a Gmail thread (read or unread), via print view: [(addr, date, body, key)]."""
    r = js(pt_js(hx))
    t = re.sub(r"<(br|/tr|/div|/p)[^>]*>", "\n", r)
    t = html.unescape(re.sub(r"<[^>]+>", "", t)).replace("\r", "")
    hs = list(HDR.finditer(t)); out = []; seen = {}
    for k, h in enumerate(hs):
        end = hs[k + 1].start() if k + 1 < len(hs) else len(t)
        body = re.split(r"\n\s*(?:To respond to this text message|\[Quoted text hidden\]|YOUR ACCOUNT|-- *\n)", t[h.end():end])[0].strip()
        addr, date = h.group(2), re.sub(r"\s", " ", h.group(3))
        base = addr + "|" + date + "|" + body
        seen[base] = seen.get(base, 0) + 1
        out.append((addr, date, body, hashlib.sha1((base + "|%d" % seen[base]).encode()).hexdigest()[:16]))
    return out

KEEP_HEXES = 4

def scan(state):
    """New texts to your GV number: [(body, thread_hex, date)]. Reads whole threads, so a text
    that Gmail marked read (we opened the thread to reply) is still caught.
    Gmail splits one Google Voice conversation into several threads over time, and the feed
    can list an older thread after a newer one. So it watches the few newest thread hexes
    (newest first, kept in state), not one hex per conversation."""
    hexes = state.setdefault("gv_hexes", [])
    for hx in (state.pop("threads", None) or {}).values():   # migrate the old one-per-address map
        if hx not in hexes: hexes.append(hx)
    seen = state.setdefault("seen", [])
    fresh = []
    for e in json.loads(js(feed_js()) or "[]"):
        if e["email"].startswith(CLAUDE_GV + ".") and e["email"].endswith("txt.voice.google.com"):
            if e["hex"] and e["hex"] not in fresh: fresh.append(e["hex"])
    hexes[:] = (fresh + [h for h in hexes if h not in fresh])[:KEEP_HEXES]
    if hexes: LAST_HEX[0] = hexes[0]
    out = []
    for hx in reversed(hexes):               # oldest thread first, so texts come out in order
        for a, date, body, key in thread_msgs(hx):
            if key in seen or not a.startswith(CLAUDE_GV + "."): continue
            seen.append(key)
            digits = a.split("@")[0].split(".")[1][-10:]
            if digits not in ALLOWED:
                log("ignored text from a number not in allowed_senders:", repr(body[:60])); continue
            out.append((body, hx, date))
    state["seen"] = seen[-2000:]; save(state)
    return out

def tick(state):
    got = scan(state)
    if not got: return
    time.sleep(SETTLE)                       # let rapid-fire texts land, then batch them
    got += scan(state)
    for body, hx in group_texts(got):
        log("TEXT:", repr(body[:300]))
        dispatch(state, body, hx)

def _minute(date):
    """Parse a Gmail header date like "Thu, Nov 12, 2026 at 6:19 AM" (None if it can't)."""
    try: return datetime.datetime.strptime(re.sub(r"\s*([AP]M)$", r" \1", date.strip()),
                                           "%a, %b %d, %Y at %I:%M %p")
    except Exception: return None

def group_texts(got):
    """[(body, hex, date)] -> [(body, hex)]. Texts sent within a minute of each other are one
    task; a backlog of texts sent minutes apart (e.g. after the watcher was down) becomes one
    task per text. Dates have minute precision, so "within a minute" means <= 60 seconds."""
    groups = []
    for body, hx, date in got:
        t = _minute(date)
        if groups and (t is None or groups[-1][2] is None or abs((t - groups[-1][2]).total_seconds()) <= 60):
            groups[-1][0].append(body); groups[-1][1] = hx; groups[-1][2] = t or groups[-1][2]
        else:
            groups.append([[body], hx, t])
    return [("\n".join(b), hx) for b, hx, _ in groups]

def dispatch(state, body, hx):
    """A command, or a new job running alongside any others (up to MAX_PARALLEL)."""
    if control(body, hx): return
    with RLOCK: n = len(RUNNING)
    if n >= MAX_PARALLEL:
        send_reply(hx, "%d tasks already running, that's my max. Text \"status\" to see them, "
                       "then resend." % n)
        return
    jid = start_job(state, body, hx)
    log("started job #%d (%d running)" % (jid, n + 1))
    try: send_reply(hx, "👍 on it: " + body.strip().split("\n")[0][:40]
                    + (" (alongside %s)" % plural(n, "other task") if n else ""))
    except Exception as ex: log("ack failed:", ex)

def heartbeat_once():
    """Text a 'still working' note once per job that runs over 10 minutes."""
    with RLOCK: jobs = list(RUNNING.values())
    for j in jobs:
        if not j["pinged"] and not j["stopped"] and time.time() - j["start"] > 600:
            j["pinged"] = True
            try: send_reply(j["hex"], "Still on it: %s\nText \"status\" or \"stop\" anytime." % j["prompt"][:80])
            except Exception as ex: log("heartbeat failed:", ex)

def heartbeat():
    while True:
        time.sleep(30)
        heartbeat_once()

def drain_outbox():
    """Other scripts (cron jobs, scheduled tasks) can text you by dropping .txt files in
    outbox/. Each is sent to the first allowed number through the GV web tab under the same
    TAB lock, then renamed to .sent. Failed sends stay for retry."""
    if not OUTBOX.exists(): return
    for f in sorted(OUTBOX.glob("*.txt")):
        text = f.read_text().strip()
        if not text: f.unlink(); continue
        digits = ALLOWED[0]
        for part in split_text(text.replace("\r", "")):
            send_gv(digits, part)
        f.rename(f.with_suffix(".sent"))
        log("outbox sent:", f.name)

def init_config(a):
    """Write config.json from the command line, keeping any values already in it.
    Non-interactive on purpose, so an agent (or you) can run it in one line."""
    path = pathlib.Path(CONFIG_PATH)
    cfg = json.loads((DIR / "config.example.json").read_text())
    cfg["agent_cmd"] = "claude"
    if path.exists(): cfg.update(json.loads(path.read_text()))
    if a.phone: cfg["allowed_senders"] = a.phone
    if a.gv_number: cfg["gv_number"] = a.gv_number
    if a.email:
        if cfg.get("reply_from_email") in (None, "", cfg.get("google_account_email"), "you@example.com"):
            cfg["reply_from_email"] = a.email
        cfg["google_account_email"] = a.email
    if a.agent_cmd:
        cfg["agent_cmd"] = a.agent_cmd
        cfg.pop("claude_path", None)
    try:
        configure(cfg)
    except ConfigError as ex:
        print(ex, file=sys.stderr); sys.exit(2)
    path.write_text(json.dumps(cfg, indent=2) + "\n")
    print("Wrote %s (agent: %s at %s)" % (path, AGENT["name"], AGENT["argv"][0]))
    print("Next: python3 watcher.py --check")

def chrome_pref_apple_events():
    """True/False from Chrome's Preferences file, or None if it cannot be read. Chrome writes
    this file lazily, so a fresh change may not show up until Chrome saves it."""
    f = CHROME_DIR / CHROME_PROFILE / "Preferences"
    try: prefs = json.loads(f.read_text())
    except Exception: return None
    for root in (prefs.get("account_values") or {}, prefs):
        v = (root.get("browser") or {}).get("allow_javascript_apple_events")
        if v is not None: return bool(v)
    return False

def check():
    """Offline preflight. Never touches Chrome tabs, so it triggers no permission prompts."""
    bad = 0
    def say(ok, msg):
        print(("ok    " if ok is True else "WARN  " if ok is None else "FAIL  ") + msg)
    try:
        load_config()
        say(True, "config %s (replies go to ...%s, GV number ...%s)" % (CONFIG_PATH, ALLOWED[0][-4:], CLAUDE_GV[-4:]))
    except ConfigError as ex:
        say(False, str(ex)); return 2
    for role, ag in (("agent", AGENT), ("fallback", FALLBACK)):
        if not ag: continue
        try:
            r = subprocess.run([ag["argv"][0], "--version"], capture_output=True, text=True,
                               timeout=30, env=child_env())
            v = (r.stdout or r.stderr).strip().splitlines()
            say(r.returncode == 0, "%s: %s -> %s" % (role, " ".join(ag["argv"]), v[0] if v else "rc=%d" % r.returncode))
            bad += r.returncode != 0
        except Exception as ex:
            say(False, "%s: %s failed to run: %s" % (role, ag["argv"][0], ex)); bad += 1
    print("      (make sure it is logged in: run it once by hand, e.g. `claude` or `codex login`)")
    if not os.path.exists(CHROME_APP):
        say(False, "Chrome not found at %s (set chrome_app in config.json)" % CHROME_APP); bad += 1
    try:
        say(True, "real Chrome running (pid %d)" % chrome_pid())
    except RuntimeError:
        say(None, "Chrome is not running. Open it and sign in to %s." % GV_ACCOUNT)
    ps = subprocess.run(["ps", "-axo", "pid=,command="], capture_output=True, text=True).stdout
    stray = [l.strip().split(" ")[0] for l in ps.splitlines()
             if CHROME_APP in l and "--type=" not in l and ("--headless" in l or "--user-data-dir" in l)]
    if stray:
        say(None, "extra Chrome instance(s) running (headless or test profile), pids %s. They can grab "
                  "AppleScript from your real Chrome; quit them if the watcher misbehaves." % ", ".join(stray))
    pref = chrome_pref_apple_events()
    if pref:
        say(True, "Chrome: Allow JavaScript from Apple Events is on")
    else:
        say(None, "Chrome: turn on View > Developer > Allow JavaScript from Apple Events"
                  + ("" if pref is False else " (could not read Chrome's settings to confirm)"))
    print("""
Manual steps only you can do (the watcher cannot click these):
  1. voice.google.com > Settings > Messages: turn on "Forward messages to email".
  2. Text your Google Voice number once from your phone so a thread exists in Gmail.
  3. Chrome: View > Developer > Allow JavaScript from Apple Events.
  4. On first run macOS asks "<app> wants to control Google Chrome". Click Allow. <app> is
     whatever started the watcher (Terminal with --start). To fix a Deny later: System
     Settings > Privacy & Security > Automation > <app> > Google Chrome.
Then start it: python3 watcher.py --start""")
    return 1 if bad else 0

def start(app):
    """Open run.sh in its own terminal window, outside the agent's process and sandbox, so
    it keeps running after the agent session ends."""
    run_sh = DIR / "run.sh"
    os.chmod(run_sh, 0o755)
    r = subprocess.run(["open", "-a", app, str(run_sh)], capture_output=True, text=True)
    if r.returncode != 0:
        print("Could not open %s (%s). Open a terminal yourself and run:\n  cd %s && ./run.sh"
              % (app, r.stderr.strip(), shlex.quote(str(DIR))), file=sys.stderr)
        return 1
    print("Started run.sh in a new %s window. Leave it open. Allow the 'control Google Chrome' "
          "prompt if macOS shows one, then text your Google Voice number \"status\"." % app)
    return 0

def prime(state):
    state["seen"] = []; scan(state); log("primed", len(state["seen"]))

def main():
    ap = argparse.ArgumentParser(description="Text your coding agent from your phone through Google Voice.")
    ap.add_argument("--init", action="store_true", help="write config.json from the flags below")
    ap.add_argument("--phone", action="append", help="your cell number (repeat for more allowed senders)")
    ap.add_argument("--gv-number", help="your Google Voice number")
    ap.add_argument("--email", help="Google account that owns the Google Voice number")
    ap.add_argument("--agent-cmd", help='agent CLI, e.g. "claude" (default) or "codex exec"')
    ap.add_argument("--check", action="store_true", help="offline preflight checks")
    ap.add_argument("--start", action="store_true", help="launch run.sh in its own terminal window")
    ap.add_argument("--app", default="Terminal", help="terminal app for --start (Terminal, iTerm, ...)")
    ap.add_argument("--prime", action="store_true", help="mark existing texts as seen and exit")
    a = ap.parse_args()
    if a.init: return init_config(a)
    if a.check: sys.exit(check())
    if a.start: sys.exit(start(a.app))
    try:
        load_config()
    except ConfigError as ex:
        print(ex, file=sys.stderr); sys.exit(2)
    state = load()
    if a.prime: return prime(state)
    if "seen" not in state:     # first run: don't treat old texts as new requests
        prime(state)
    log("watcher started (agent: %s, up to %d jobs in parallel)" % (" ".join(AGENT["argv"]), MAX_PARALLEL))
    threading.Thread(target=heartbeat, daemon=True).start()
    flag = DIR / "restart.flag"           # touch this to restart safely (run.sh relaunches)
    while True:
        try: tick(state)
        except Exception as ex: log("error:", ex)
        try: drain_outbox()
        except Exception as ex: log("outbox error:", ex)
        if flag.exists() and idle():
            flag.unlink(); log("restart requested, idle -> exiting"); os._exit(0)
        time.sleep(POLL)

if __name__ == "__main__":
    main()
