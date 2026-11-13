#!/usr/bin/env python3
"""Text Claude Code from your phone through Google Voice.

Flow: you text your Google Voice number. Google Voice forwards the text to your Gmail
inbox. This script reads that inbox through a dedicated tab in your logged-in Chrome
(JXA -> JavaScript in the tab), runs `claude -p` in a resumed session, and replies through
the Google Voice web app. If that fails it replies to the Google Voice email instead,
which Google Voice delivers back as a text in the same thread.

All personal values (phone numbers, account email, paths, model) live in config.json,
next to this file. Copy config.example.json to config.json and fill it in.
"""
import json, os, re, subprocess, sys, time, html, datetime, pathlib, threading, queue, signal, hashlib

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
CLAUDE = str(HOME / ".local" / "bin" / "claude")
CLAUDE_CWD = str(HOME)
MODEL = "opus"
EFFORT = "high"
PERMISSION_MODE = "auto"
AGY = None             # optional fallback CLI, used only when Claude is out of usage
POLL = 4
SETTLE = 5             # wait for rapid-fire texts, then batch
JOB_TIMEOUT = 3600
SYSTEM = ""

BASE_SYSTEM = """You are Claude Code running on the owner's Mac, reached by text message
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
  use the Agent tool to run sub-agents in parallel, one per item or group, instead
  of working through them one at a time. Then merge their results into the report.
  Items that take one quick command each can just be parallel tool calls.
- Long answers are fine when there are many items: they go out as several texts.
- Line breaks reach the phone. Format for a phone screen: one item per line,
  short lines, a blank line between sections. Never a wall of text.
- Lists over text: bulleted with "•", one item per line, "Name - short detail". No group
  headers, no parentheticals, no comma-run blobs of names.
"""

class ConfigError(Exception):
    pass

def _digits(v):
    return re.sub(r"\D", "", str(v or ""))

def _path(v):
    return str(pathlib.Path(os.path.expanduser(str(v))))

def configure(cfg):
    """Validate a config dict and set the module globals from it."""
    global ALLOWED, CLAUDE_GV, GV_ACCOUNT, REPLY_FROM, GMAIL_U, GV_U, TAB_URL, CHROME_APP, \
        CHROME_PROFILE, CLAUDE, CLAUDE_CWD, MODEL, EFFORT, PERMISSION_MODE, AGY, POLL, SETTLE, \
        JOB_TIMEOUT, SYSTEM
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
    CLAUDE = _path(cfg.get("claude_path") or "~/.local/bin/claude")
    CLAUDE_CWD = _path(cfg.get("claude_cwd") or "~")
    MODEL = cfg.get("model") or "opus"
    EFFORT = cfg.get("effort") or "high"
    PERMISSION_MODE = cfg.get("permission_mode") or "auto"
    AGY = _path(cfg["fallback_agy_path"]) if cfg.get("fallback_agy_path") else None
    POLL = float(cfg.get("poll_seconds", 4))
    SETTLE = float(cfg.get("settle_seconds", 5))
    JOB_TIMEOUT = int(cfg.get("job_timeout_seconds", 3600))
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
    if not pathlib.Path(CLAUDE).exists():
        raise ConfigError('"claude_path" %s does not exist. Run `which claude` and put that path in config.json.' % CLAUDE)
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

def ensure_tab(mark=MARK, url=None):
    url = url or TAB_URL
    for _ in range(3):
        try:
            t = find_tab(mark)
            if t:
                dedupe_tabs(mark)
                return t
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

CUR = {"proc": None, "prompt": "", "start": 0, "stopped": False}

def run(cmd):
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                         cwd=CLAUDE_CWD, start_new_session=True)
    CUR["proc"] = p
    try: out, err = p.communicate(timeout=JOB_TIMEOUT)
    except subprocess.TimeoutExpired:
        os.killpg(p.pid, signal.SIGTERM); out, err = p.communicate()
    CUR["proc"] = None
    return p.returncode, out or "", err or ""

def run_agy(state, prompt):
    """Optional fallback when Claude is out of usage (set "fallback_agy_path" in config).
    Runs in the fallback CLI's default asking mode, so anything needing approval is
    declined rather than auto-run."""
    cmd = [AGY, "-p", prompt, "--output-format", "json", "--effort", EFFORT]
    if state.get("agy_conv"): cmd += ["--conversation", state["agy_conv"]]
    rc, out, err = run(cmd)
    j = None
    for line in reversed(out.strip().splitlines()):
        try: j = json.loads(line); break
        except Exception: continue
    if j and j.get("conversation_id"): state["agy_conv"] = j["conversation_id"]
    if j and j.get("status") != "ERROR" and j.get("response"):
        return "(via fallback) " + j["response"].strip()
    e = (j or {}).get("error") or (err or out)[-200:]
    return None if not e else "Fallback failed too: " + e.strip()[:200]

def run_claude(state, prompt):
    cmd = [CLAUDE, "-p", prompt, "--output-format", "json", "--permission-mode", PERMISSION_MODE,
           "--model", MODEL, "--effort", EFFORT, "--append-system-prompt", SYSTEM]
    if state.get("session"): cmd += ["--resume", state["session"]]
    rc, out, err = run(cmd)
    if CUR["stopped"]: return None
    j = None
    try: j = json.loads(out)
    except Exception: pass
    if j and j.get("session_id"): state["session"] = j["session_id"]
    text = ((j or {}).get("result") or "").strip()
    failed = (j is None) or j.get("is_error") or rc != 0
    blob = text + " " + err + " " + (out if j is None else "")
    if failed and LIMIT_RE.search(blob):
        if AGY:
            log("Claude out of usage -> falling back to", AGY)
            alt = run_agy(state, prompt)
            return alt or "Claude is out of usage and the fallback gave no answer."
        return "Claude is out of usage right now. Try again later."
    if failed:
        if state.get("session") and "resume" in err.lower():
            state["session"] = None
        return "Hit an error running that: " + (text or err or out)[-300:]
    return text or "Done (no text output)."

JOBS = queue.Queue()

def worker(state):
    while True:
        body, hexid = JOBS.get()
        batch = [body]
        while not JOBS.empty():              # fold queued follow-ups into one prompt
            b, hexid = JOBS.get(); batch.append(b)
        prompt = "[Text(s) from the owner, %s]\n" % datetime.datetime.now().strftime("%a %b %-d %-I:%M %p") + "\n\n".join(batch)
        CUR.update(prompt=prompt, start=time.time(), stopped=False); LAST_HEX[0] = hexid
        try:
            answer = run_claude(state, prompt); save(state)
            if answer is None:
                log("job stopped"); continue
            log("REPLY:", repr(answer[:300]))
            send_reply(hexid, answer)
        except Exception as ex:
            log("job error:", ex)
            try: send_reply(hexid, "Something broke on my end: %s" % str(ex)[:200])
            except Exception: pass
        finally:
            CUR.update(prompt="", start=0)

def control(body, hexid):
    """Text commands handled by the watcher itself. Returns True if handled."""
    c = body.strip().lower().rstrip(".!")
    if c in ("stop", "cancel", "kill it", "stop it", "nevermind", "never mind"):
        p = CUR["proc"]
        while not JOBS.empty(): JOBS.get()
        if p:
            CUR["stopped"] = True
            try: os.killpg(p.pid, signal.SIGTERM)
            except Exception: pass
            send_reply(hexid, "Stopped.")
        else: send_reply(hexid, "Nothing running.")
        return True
    if c in ("status", "?", "you there", "you there?"):
        if CUR["start"]:
            mins = int((time.time() - CUR["start"]) / 60)
            msg = "Still working (%d min) on: %s" % (mins, CUR["prompt"][:200])
            if JOBS.qsize(): msg += "\n%d more queued after it." % JOBS.qsize()
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

def scan(state):
    """New texts to your GV number: [(body, thread_hex)]. Reads whole threads, so a text
    that Gmail marked read (we opened the thread to reply) is still caught."""
    threads = state.setdefault("threads", {})           # GV conversation addr -> latest thread hex
    seen = state.setdefault("seen", [])
    for e in json.loads(js(feed_js()) or "[]"):
        if e["email"].startswith(CLAUDE_GV + ".") and e["email"].endswith("txt.voice.google.com"):
            threads[e["email"]] = e["hex"]
    out = []
    for addr, hx in list(threads.items()):
        for a, date, body, key in thread_msgs(hx):
            if key in seen or not a.startswith(CLAUDE_GV + "."): continue
            seen.append(key)
            digits = a.split("@")[0].split(".")[1][-10:]
            if digits not in ALLOWED:
                log("ignored text from a number not in allowed_senders:", repr(body[:60])); continue
            out.append((body, hx))
    state["seen"] = seen[-2000:]; save(state)
    return out

def tick(state):
    got = scan(state)
    if not got: return
    time.sleep(SETTLE)                       # let rapid-fire texts land, then batch them
    got += scan(state)
    body = "\n".join(b for b, _ in got); hx = got[-1][1]
    log("TEXT:", repr(body[:300]))
    if control(body, hx): return
    busy = bool(CUR["start"]) or not JOBS.empty()
    JOBS.put((body, hx))
    try: send_reply(hx, "👍 got it, I'll do that right after the current one" if busy else "👍 on it")
    except Exception as ex: log("ack failed:", ex)

def heartbeat():
    """Text a 'still working' note once if a job runs long."""
    pinged = 0
    while True:
        time.sleep(30)
        s = CUR["start"]
        if s and s != pinged and time.time() - s > 600:
            pinged = s
            try: send_reply(LAST_HEX[0], "Still on it, taking a bit. Text \"status\" or \"stop\" anytime.")
            except Exception as ex: log("heartbeat failed:", ex)

LAST_HEX = [None]

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

def main():
    try:
        load_config()
    except ConfigError as ex:
        print(ex, file=sys.stderr); sys.exit(2)
    state = load()
    if "--prime" in sys.argv:   # mark every message in known threads as seen
        state["seen"] = []; scan(state); log("primed", len(state["seen"])); return
    if "seen" not in state:
        log("no seen-list yet: run with --prime first"); return
    log("watcher started")
    threading.Thread(target=worker, args=(state,), daemon=True).start()
    threading.Thread(target=heartbeat, daemon=True).start()
    flag = DIR / "restart.flag"           # touch this to restart safely (run.sh relaunches)
    while True:
        try: tick(state)
        except Exception as ex: log("error:", ex)
        try: drain_outbox()
        except Exception as ex: log("outbox error:", ex)
        if flag.exists() and not CUR["start"] and JOBS.empty():
            flag.unlink(); log("restart requested, idle -> exiting"); os._exit(0)
        time.sleep(POLL)

if __name__ == "__main__":
    main()
