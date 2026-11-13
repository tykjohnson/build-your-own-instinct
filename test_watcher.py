"""Offline regression tests: python3 test_watcher.py
(no Chrome, no config.json, no texts sent, no real agent CLI needed: fake ones are used)."""
import sys, time, os, json, tempfile, pathlib
sys.argv = ['x']
import watcher as w

FAKE = pathlib.Path(tempfile.mkdtemp(prefix="instinct-test-"))

def fake_cli(name, body):
    f = FAKE / name
    f.write_text("#!/usr/bin/env python3\nimport sys, json, os\nargs = sys.argv[1:]\n" + body)
    f.chmod(0o755)
    return str(f)

# Fake claude: echoes the text back as a JSON result, reports a session id. A forked
# session gets a new id, like the real CLI. "SLOW" texts run until they are killed.
FAKE_CLAUDE = fake_cli("claude", """
import time
p = args[args.index('-p') + 1]
lines = p.splitlines()
text = lines[1] if lines[0].startswith('[Text') else lines[-1]
sid = args[args.index('--resume') + 1] if '--resume' in args else 'sess-1'
if '--fork-session' in args: sid = 'fork-' + sid
if 'SLOW' in p: time.sleep(60)
if 'LIMIT' in p:
    print(json.dumps({'is_error': True, 'result': 'Claude usage limit reached'})); sys.exit(1)
print(json.dumps({'result': 'claude:' + text, 'session_id': sid}))
""")
# Fake codex: JSONL events on stdout, final message written to the -o file.
FAKE_CODEX = fake_cli("codex", """
assert args[0] == 'exec', args
assert '--skip-git-repo-check' in args and '--json' in args
out = args[args.index('-o') + 1]
if 'resume' in args:
    tid = args[args.index('resume') + 1]
else:
    tid = 'thread-1'
    print(json.dumps({'type': 'thread.started', 'thread_id': tid}))
open(out, 'w').write('codex:' + tid + ':' + args[-1].splitlines()[-1])
print(json.dumps({'type': 'turn.completed'}))
""")

# Fake values only (555 numbers are reserved for fiction).
w.configure({
    "allowed_senders": ["+1 (555) 555-0123"],
    "gv_number": "+1 555 555 0100",
    "google_account_email": "you@example.com",
    "agent_cmd": FAKE_CLAUDE,
})

sent = []
w.send_one = lambda hx, t: sent.append(t)
gv = []
def _gv_fail(d, t): raise RuntimeError("gv down")
w.send_gv = _gv_fail
w.save = lambda s: None
w.log = lambda *a: None

def test_config_normalizes_numbers():
    assert w.ALLOWED == ["5555550123"], w.ALLOWED
    assert w.CLAUDE_GV == "15555550100", w.CLAUDE_GV
    assert w.REPLY_FROM == "you@example.com"

def test_config_rejects_missing_values():
    try:
        w.configure({"allowed_senders": [], "gv_number": "12", "google_account_email": ""})
    except w.ConfigError as ex:
        msg = str(ex)
        assert "allowed_senders" in msg and "gv_number" in msg and "google_account_email" in msg, msg
    else:
        raise AssertionError("bad config accepted")
    assert w.ALLOWED == ["5555550123"]     # a rejected config leaves the old one in place

def test_blank_lines_never_reach_gv():
    sent.clear()
    w.send_reply('h', "para one\n\npara two\n  \n- item\n\n\n2. item")
    assert all("\n\n" not in p for p in sent), sent
    assert "2. item" in sent[-1]

def test_gv_path_keeps_line_breaks():
    gv.clear(); w.send_gv = lambda d, t: gv.append(t)
    w.send_reply('h', "Haven't spoken:\n1. A\n2. B\n\nNext section")
    assert gv == ["Haven't spoken:\n1. A\n2. B\n\nNext section"], gv
    w.send_gv = _gv_fail

def test_emoji_length_matches_js():
    assert len("👍 on it".encode("utf-16-le")) // 2 == 8   # what the GV textarea reports

def test_long_reply_splits_without_loss():
    sent.clear()
    body = "\n\n".join("line %d " % i + "x" * 90 for i in range(40))
    w.send_reply('h', body)
    joined = "\n".join(sent)
    assert len(sent) > 1 and all(len(p) <= 1400 for p in sent)
    assert "line 0 " in joined and "line 39 " in joined

def test_restart_flag_waits_for_idle():
    # any running job -> must not exit; no jobs -> may exit
    assert w.idle()
    w.RUNNING[999] = w.new_job("x", "h"); assert not w.idle()
    w.RUNNING.pop(999); assert w.idle()

GV_ADDR = "15555550100.15555550123.Z@txt.voice.google.com"

def test_read_texts_still_found():
    # thread scan dedupes by content, not by Gmail unread state
    page = ("(555) 555-0123 &lt;" + GV_ADDR + "&gt;Thu, Nov 12, 2026 at 6:19 AMTo: me\n"
            "follow up text\nTo respond to this text message, reply\n"
            "Me &lt;you@example.com&gt;Thu, Nov 12, 2026 at 6:21 AMTo: x\nreply\n")
    w.js = lambda code: page if "view=pt" in code else "[]"
    st = {"threads": {GV_ADDR: "abc"}, "seen": []}
    got = w.scan(st); assert got == [("follow up text", "abc", "Thu, Nov 12, 2026 at 6:19 AM")], got
    assert st["gv_hexes"] == ["abc"] and "threads" not in st, st   # old state key migrated
    assert w.scan(st) == []          # not processed twice

def test_unknown_sender_ignored():
    addr = "15555550100.15555550199.Z@txt.voice.google.com"
    page = ("(555) 555-0199 &lt;" + addr + "&gt;Thu, Nov 12, 2026 at 7:00 AMTo: me\n"
            "hello\nTo respond to this text message, reply\n")
    w.js = lambda code: page if "view=pt" in code else "[]"
    st = {"threads": {addr: "def"}, "seen": []}
    assert w.scan(st) == []

def test_resolve_agent_kinds():
    a = w.resolve_agent(FAKE_CLAUDE + " -p --model sonnet")
    assert a["kind"] == "claude" and "-p" not in a["argv"] and a["argv"][-2:] == ["--model", "sonnet"], a
    c = w.resolve_agent(FAKE_CODEX + " -m some-model")
    assert c["kind"] == "codex" and c["argv"][1] == "exec", c
    assert w.resolve_agent([FAKE_CODEX, "exec"])["argv"].count("exec") == 1
    assert w.resolve_agent("/bin/echo {prompt}")["kind"] == "custom"
    try: w.resolve_agent("/nonexistent/agent-cli")
    except w.ConfigError as ex: assert "agent_cmd" in str(ex)
    else: raise AssertionError("missing CLI accepted")

def test_agent_cmd_env_overrides_config():
    os.environ["AGENT_CMD"] = FAKE_CODEX + " exec"
    try:
        w.configure({"allowed_senders": ["5555550123"], "gv_number": "5555550100",
                     "google_account_email": "you@example.com", "agent_cmd": FAKE_CLAUDE})
        assert w.AGENT["kind"] == "codex", w.AGENT
    finally:
        del os.environ["AGENT_CMD"]
        w.configure({"allowed_senders": ["5555550123"], "gv_number": "5555550100",
                     "google_account_email": "you@example.com", "agent_cmd": FAKE_CLAUDE})

def test_claude_runner_forks_last_session():
    st = {}
    assert w.run_job(st, "hi") == "claude:hi"
    assert st["sessions"]["primary:claude"] == "sess-1"
    assert w.run_job(st, "again") == "claude:again"
    # follow-ups fork the last finished session; the newest finisher becomes the base
    assert st["sessions"]["primary:claude"] == "fork-sess-1", st

def test_codex_runner_resumes_thread():
    old = w.AGENT
    w.AGENT = w.resolve_agent(FAKE_CODEX)
    try:
        st = {}
        assert w.run_job(st, "first") == "codex:thread-1:first", st
        assert st["sessions"]["primary:codex"] == "thread-1"
        assert w.run_job(st, "second") == "codex:thread-1:second"
    finally:
        w.AGENT = old

def test_codex_parallel_job_gets_fresh_thread():
    # while one job holds the Codex thread, another starts fresh and leaves the base alone
    old = w.AGENT
    w.AGENT = w.resolve_agent(FAKE_CODEX)
    try:
        st = {"sessions": {"primary:codex": "thread-old"}}
        w.HELD["primary:codex"] = 1
        assert w.run_job(st, "par") == "codex:thread-1:par"
        assert st["sessions"]["primary:codex"] == "thread-old" and w.HELD["primary:codex"] == 1
        w.HELD.pop("primary:codex")
        assert w.run_job(st, "next") == "codex:thread-old:next"
        assert not w.HELD
    finally:
        w.AGENT = old

def test_custom_runner_and_fallback_on_usage_limit():
    old = w.FALLBACK
    w.FALLBACK = w.resolve_agent("/bin/echo {prompt}")
    try:
        r = w.run_job({}, "LIMIT please")
        assert r.startswith("(via echo) ") and r.endswith("LIMIT please"), r
        w.FALLBACK = None
        assert "out of usage" in w.run_job({}, "LIMIT again")
    finally:
        w.FALLBACK = old

def test_child_env_drops_outer_claude_session():
    os.environ["CLAUDECODE"] = "1"
    try: assert "CLAUDECODE" not in w.child_env()
    finally: del os.environ["CLAUDECODE"]
    assert "/opt/homebrew/bin" in w.child_env()["PATH"]

# Parallel jobs: real fake-CLI processes, stubbed texting.
replies = []
def wait_for(cond, timeout=15):
    end = time.time() + timeout
    while time.time() < end:
        if cond(): return True
        time.sleep(0.05)
    return False

def with_stub_send(fn):
    def wrapped():
        old = w.send_reply
        w.send_reply = lambda hx, t, digits=None: replies.append(t)
        replies.clear()
        try: fn()
        finally:
            w.stop_all(); wait_for(w.idle)
            w.send_reply = old
    wrapped.__name__ = fn.__name__
    return wrapped

@with_stub_send
def test_parallel_jobs_each_reply():
    st = {}
    w.dispatch(st, "first ask", "h1")
    w.dispatch(st, "second ask", "h2")
    assert wait_for(w.idle), w.RUNNING
    acks = [r for r in replies if r.startswith("\U0001F44D")]
    answers = sorted(r for r in replies if r not in acks)
    assert acks[0] == "\U0001F44D on it: first ask", replies
    assert acks[1] in ("\U0001F44D on it: second ask (alongside 1 other task)",
                       "\U0001F44D on it: second ask"), replies   # the first may already be done
    assert answers == ["claude:first ask", "claude:second ask"], replies

@with_stub_send
def test_parallel_status_cap_and_stop():
    old_max = w.MAX_PARALLEL
    w.MAX_PARALLEL = 2
    try:
        st = {}
        w.dispatch(st, "SLOW one", "h1")
        w.dispatch(st, "SLOW two", "h2")
        assert replies == ["👍 on it: SLOW one", "👍 on it: SLOW two (alongside 1 other task)"], replies
        assert wait_for(lambda: all(j["proc"] for j in list(w.RUNNING.values()))), w.RUNNING
        procs = [j["proc"] for j in w.RUNNING.values()]
        # the later job is told what the other one is doing
        jids = sorted(w.RUNNING)
        note = w.build_prompt(jids[1], "SLOW two").split("Other agents", 1)[1]
        assert "- SLOW one" in note and "- SLOW two" not in note, note
        w.dispatch(st, "status", "h3")
        lines = replies[-1].splitlines()
        assert lines[0] == "2 running:" and lines[1] == "• 0 min - SLOW one" \
            and lines[2] == "• 0 min - SLOW two", replies[-1]
        w.dispatch(st, "third", "h4")                  # over the cap: refused, nothing started
        assert "that's my max" in replies[-1] and len(w.RUNNING) == 2, replies[-1]
        assert not w.idle()
        w.dispatch(st, "stop", "h5")
        assert replies[-1] == "Stopped 2 tasks.", replies[-1]
        assert wait_for(w.idle), w.RUNNING
        assert all(p.poll() is not None for p in procs)    # processes really killed
        time.sleep(0.2)
        assert replies[-1] == "Stopped 2 tasks.", replies  # stopped jobs send no reply
        w.dispatch(st, "status", "h6")
        assert replies[-1] == "Here. Nothing running."
        w.dispatch(st, "stop", "h7")
        assert replies[-1] == "Nothing running."
    finally:
        w.MAX_PARALLEL = old_max

@with_stub_send
def test_heartbeat_pings_each_long_job_once():
    w.RUNNING[901] = w.new_job("long task", "h1")
    w.RUNNING[902] = w.new_job("short task", "h2")
    w.RUNNING[901]["start"] -= 700
    try:
        w.heartbeat_once(); w.heartbeat_once()
        assert len(replies) == 1 and replies[0].startswith("Still on it: long task"), replies
    finally:
        w.RUNNING.pop(901); w.RUNNING.pop(902)

def test_max_parallel_config():
    base = {"allowed_senders": ["5555550123"], "gv_number": "5555550100",
            "google_account_email": "you@example.com", "agent_cmd": FAKE_CLAUDE}
    try:
        w.configure(dict(base, max_parallel=3)); assert w.MAX_PARALLEL == 3
        try: w.configure(dict(base, max_parallel=0))
        except w.ConfigError as ex: assert "max_parallel" in str(ex)
        else: raise AssertionError("max_parallel 0 accepted")
    finally:
        w.configure(base)
    assert w.MAX_PARALLEL == 6

def gv_page(*texts):
    """Print view of a GV thread: texts as (time, body), e.g. ("6:19 AM", "hi")."""
    return "".join("(555) 555-0123 &lt;" + GV_ADDR + "&gt;Thu, Nov 12, 2026 at %s" % t
                   + "To: me\n%s\nTo respond to this text message, reply\n" % b for t, b in texts)

def test_thread_split_watches_newest_hexes():
    # Gmail split the conversation: the feed lists the new thread first, then the old one.
    # The new thread's text must be read (one-hex-per-address let the old one win).
    pages = {"old": gv_page(("4:10 AM", "old text")), "new": gv_page(("4:53 AM", "new text"))}
    feed = [{"email": GV_ADDR, "hex": "new"}, {"email": GV_ADDR, "hex": "old"}]
    def fake_js(code):
        if "feed/atom" in code: return json.dumps(feed)
        return pages[code.split("th=")[1].split("'")[0]]
    w.js = fake_js
    st = {"seen": []}
    got = w.scan(st)
    assert st["gv_hexes"] == ["new", "old"], st
    assert [(b, h) for b, h, _ in got] == [("old text", "old"), ("new text", "new")], got
    assert w.LAST_HEX[0] == "new"
    # later the feed only shows a third thread: the older ones stay watched, newest first
    pages["n3"] = gv_page(("5:30 AM", "third"))
    pages["new"] = gv_page(("4:53 AM", "new text"), ("5:31 AM", "late reply in new thread"))
    feed[:] = [{"email": GV_ADDR, "hex": "n3"}]
    got = w.scan(st)
    assert st["gv_hexes"] == ["n3", "new", "old"], st
    assert sorted(b for b, _, _ in got) == ["late reply in new thread", "third"], got
    # only the newest few are kept
    for h in ("n4", "n5"):
        pages[h] = ""; feed[:] = [{"email": GV_ADDR, "hex": h}]; w.scan(st)
    assert st["gv_hexes"] == ["n5", "n4", "n3", "new"], st
    # other GV numbers' threads are not watched
    feed[:] = [{"email": "15555550111.15555550123.Z@txt.voice.google.com", "hex": "x"}]
    w.scan(st); assert "x" not in st["gv_hexes"]

def test_group_texts():
    d = "Thu, Nov 12, 2026 at %s"
    got = [("a", "h1", d % "6:19 AM"), ("b", "h1", d % "6:20 AM"),     # 60s apart: one task
           ("c", "h1", d % "6:25 AM"),                                   # minutes later: its own
           ("d", "h2", d % "6:40AM"), ("e", "h2", "unparseable")]       # no space before AM; bad date joins
    assert w.group_texts(got) == [("a\nb", "h1"), ("c", "h1"), ("d\ne", "h2")], w.group_texts(got)
    assert w.group_texts([]) == []
    assert w.group_texts([("x", "h", d % "11:59 PM"), ("y", "h", "Fri, Nov 13, 2026 at 12:00 AM")]) \
        == [("x\ny", "h")]

def test_ack_shows_task():
    old = w.send_reply, w.start_job
    acks = []
    w.send_reply = lambda hx, t, digits=None: acks.append(t)
    w.start_job = lambda st, body, hx: 1
    try:
        w.dispatch({}, "check the build on the main branch and tell me what failed\nthanks", "h")
        assert acks == ["👍 on it: check the build on the main branch and t"], acks
    finally:
        w.send_reply, w.start_job = old

class Stubs:
    """Swap module attributes for one test and put them back."""
    def __init__(self, **kw): self.kw = kw
    def __enter__(self):
        self.old = {}
        for k, v in self.kw.items():
            obj, attr = (w, k) if "." not in k else ({"os": os, "subprocess": w.subprocess}[k.split(".")[0]], k.split(".")[1])
            self.old[(id(obj), attr)] = (obj, attr, getattr(obj, attr)); setattr(obj, attr, v)
        return self
    def __exit__(self, *a):
        for obj, attr, v in self.old.values(): setattr(obj, attr, v)

def reset_stuck(**kw):
    w.STUCK.clear(); w.STUCK.update(since=0, alerted=False, restarted=0); w.STUCK.update(kw)

def test_frozen_chrome_opens_no_tabs():
    opened = []
    def timeout(mark): raise w.subprocess.TimeoutExpired("osascript", 60)
    reset_stuck()
    with Stubs(find_tab=timeout, _sleep=lambda s: None, hid_idle=lambda: 0, **{"subprocess.Popen": lambda *a, **k: opened.append(a)}):
        for _ in range(5):
            try: w.ensure_tab()
            except RuntimeError as ex: assert "not answering" in str(ex), ex
            else: raise AssertionError("frozen Chrome returned a tab")
        assert opened == [], opened
        assert w.STUCK["since"] and not w.STUCK["alerted"]
    # Chrome answers again: STUCK clears and the tab is used
    with Stubs(find_tab=lambda mark: "1:2", dedupe_tabs=lambda mark: None):
        assert w.ensure_tab() == "1:2"
    assert w.STUCK["since"] == 0 and not w.STUCK["alerted"]

def test_auto_restart_decision():
    restarts, alerts = [], []
    now = time.time()
    def stuck():
        try: w.chrome_stuck()
        except RuntimeError as ex: return str(ex)
        raise AssertionError("chrome_stuck must raise")
    with Stubs(restart_chrome=lambda: restarts.append(1), stuck_alert=alerts.append, _sleep=lambda s: None):
        # frozen 5 min: too soon for anything
        reset_stuck(since=now - 300)
        with Stubs(hid_idle=lambda: 9999): stuck()
        assert restarts == [] and alerts == []
        # frozen 11 min and nobody at the Mac: restart
        reset_stuck(since=now - 660)
        with Stubs(hid_idle=lambda: 9999): assert stuck() == "Chrome was restarted"
        assert restarts == [1] and alerts == []
        # frozen 11 min but the Mac is in use: one alert, no restart, however many polls
        reset_stuck(since=now - 660); restarts.clear()
        with Stubs(hid_idle=lambda: 5):
            stuck(); stuck(); stuck()
        assert restarts == [] and len(alerts) == 1 and "using the Mac" in alerts[0], alerts
        # idle, but restarted 10 min ago: cooldown, alert instead of another restart
        reset_stuck(since=now - 660, restarted=now - 600); alerts.clear()
        with Stubs(hid_idle=lambda: 9999): stuck(); stuck()
        assert restarts == [] and len(alerts) == 1 and "last 30 minutes" in alerts[0], alerts
        # cooldown over: restart again
        reset_stuck(since=now - 660, restarted=now - 1900)
        with Stubs(hid_idle=lambda: 9999): stuck()
        assert restarts == [1]
        # switched off in config: alert only
        reset_stuck(since=now - 660); restarts.clear(); alerts.clear()
        with Stubs(AUTO_RESTART=False, hid_idle=lambda: 9999): stuck()
        assert restarts == [] and len(alerts) == 1 and "off" in alerts[0], alerts
    reset_stuck()

def test_stuck_alert_uses_imessage_only_if_set():
    calls = []
    with Stubs(**{"subprocess.run": lambda argv, **k: calls.append(argv)}):
        with Stubs(ALERT_IMESSAGE=None): w.stuck_alert("down")
        assert calls == []
        with Stubs(ALERT_IMESSAGE="owner@example.com"): w.stuck_alert("down")
        assert len(calls) == 1 and calls[0][0] == "osascript" and '"owner@example.com"' in calls[0][2] \
            and '"down"' in calls[0][2], calls

def test_hid_idle_parses_ioreg():
    class R: stdout = '  |   "HIDIdleTime" = %d\n' % (754 * 10**9)
    with Stubs(**{"subprocess.run": lambda *a, **k: R()}):
        assert w.hid_idle() == 754.0
    def boom(*a, **k): raise OSError("no ioreg")
    with Stubs(**{"subprocess.run": boom}):
        assert w.hid_idle() == 0               # unknown counts as "in use": never restart blind

def test_restart_chrome_sequence():
    hook = FAKE / "hook.sh"; hook.write_text("echo hook\n")
    for ignores_term in (False, True):
        kills, popens, notes = [], [], []
        def kill(pid, sig):
            kills.append((pid, sig))
            if sig == 0 and not ignores_term and len(kills) > 2: raise OSError("gone")
        reset_stuck(since=time.time() - 700)
        restored = []
        with Stubs(chrome_pid=lambda: 4242, jxa=lambda body: "1", _sleep=lambda s: None,
                   send_reply=lambda hx, t, digits=None: notes.append(t), AFTER_RESTART=str(hook),
                   AFTER_LOG=FAKE / "after.log", restore_windows=restored.append,
                   **{"os.kill": kill, "subprocess.Popen": lambda argv, **k: popens.append(argv)}):
            assert w.restart_chrome() is True
        sigs = [s for _, s in kills]
        assert all(p == 4242 for p, _ in kills) and sigs[0] == w.signal.SIGTERM, kills
        assert (w.signal.SIGKILL in sigs) == ignores_term, sigs
        if ignores_term: assert sigs.count(0) == 45
        assert popens == [[w.CHROME_APP, "--profile-directory=Default"], ["/bin/bash", str(hook)]], popens
        assert len(restored) == 1 and abs(restored[0] - time.time()) < 5, restored   # quit time
        assert w.STUCK["since"] == 0 and w.STUCK["restarted"] > 0
        assert len(notes) == 1 and notes[0].startswith("Heads up: Chrome stopped answering"), notes
    # Chrome never answers after relaunch: no hook, no notice, still frozen
    popens, notes = [], []
    reset_stuck(since=time.time() - 700)
    def gone(pid, sig):
        if sig == 0: raise OSError("gone")
    def dead(body): raise RuntimeError("no answer")
    with Stubs(chrome_pid=lambda: 4242, jxa=dead, _sleep=lambda s: None, AFTER_RESTART=str(hook),
               send_reply=lambda hx, t, digits=None: notes.append(t), RESTORE_WINDOWS=False,
               restore_windows=lambda t: notes.append("restored"),
               **{"os.kill": gone, "subprocess.Popen": lambda argv, **k: popens.append(argv)}):
        assert w.restart_chrome() is False
    assert popens == [[w.CHROME_APP, "--profile-directory=Default"]] and notes == []   # restore off
    assert w.STUCK["since"] != 0
    reset_stuck()

# A tiny Chrome session file, built by hand. Command ids and layouts follow Chromium's
# session_service_commands.cc (see snss.py).
import struct, snss

def snss_file(path, cmds):
    out = b"SNSS" + struct.pack("<i", 3)
    for cid, payload in cmds:
        out += struct.pack("<H", len(payload) + 1) + bytes([cid]) + payload
    pathlib.Path(path).write_bytes(out)
    return path

def c_tab_window(win, tab): return (0, struct.pack("<ii", win, tab))
def c_tab_index(tab, i): return (2, struct.pack("<ii", tab, i))
def c_nav(tab, i, url):
    u = url.encode(); body = struct.pack("<iii", tab, i, len(u)) + u + b"\0" * (-len(u) % 4) + b"title-etc"
    return (6, struct.pack("<I", len(body)) + body)
def c_sel_nav(tab, i): return (7, struct.pack("<ii", tab, i))
def c_win_type(win, ty): return (9, struct.pack("<ii", win, ty))
def c_pinned(tab): return (12, struct.pack("<i?xxx", tab, True))
def c_tab_closed(tab): return (16, struct.pack("<i", tab) + b"\0" * 12)       # id, padding, close time
def c_win_closed(win): return (17, struct.pack("<i", win) + b"\0" * 12)

def window_file(path, *windows):
    """windows: lists of URLs, one normal window each."""
    cmds, tab = [], 100
    for wid, urls in enumerate(windows, 1):
        for i, u in enumerate(urls):
            tab += 1; cmds += [c_tab_window(wid, tab), c_tab_index(tab, i), c_nav(tab, 0, u)]
    return snss_file(path, cmds)

def test_snss_parse():
    f = snss_file(FAKE / "Session_1", [
        c_tab_window(1, 10), c_tab_window(1, 11), c_tab_window(1, 12),
        c_tab_index(10, 1), c_tab_index(11, 0), c_tab_index(12, 2),
        c_nav(10, 0, "https://a.example/back"), c_nav(10, 1, "https://a.example/fwd"), c_sel_nav(10, 0),
        c_nav(11, 0, "https://b.example/"), c_nav(12, 0, "https://closed.example/"),
        c_pinned(10),                                   # pinned: still open
        c_tab_closed(12),                               # closed tab: left out
        (99, b"unknown command"),
        c_tab_window(2, 20), c_nav(20, 0, "https://popup.example/"), c_win_type(2, 1),   # popup
        c_tab_window(3, 30), c_nav(30, 0, "https://gone.example/"), c_win_closed(3),     # closed window
        c_tab_window(4, 40),                            # tab with no page yet: left out
    ])
    assert snss.parse(str(f)) == [["https://b.example/", "https://a.example/back"]], snss.parse(str(f))
    bad = FAKE / "not-a-session"; bad.write_bytes(b"nope")
    try: snss.parse(str(bad))
    except ValueError: pass
    else: raise AssertionError("non-SNSS file accepted")

def test_restore_windows_picks_file_and_builds_argv():
    root = FAKE / "chrome"
    for prof in ("Default", "Profile 1", "Profile 2", "Profile 3"):
        (root / prof / "Sessions").mkdir(parents=True, exist_ok=True)
    (root / "Local State").write_text(json.dumps({"profile": {"info_cache": {
        "Default": {}, "Profile 1": {}, "Profile 2": {}, "Profile 3": {}}}}))
    quit_at = time.time() - 30
    def aged(path, t): os.utime(path, (t, t)); return path
    d = root / "Default" / "Sessions"
    aged(window_file(d / "Session_old", ["https://old.example/"]), quit_at - 600)
    many = ["https://m.example/%d" % i for i in range(45)]
    aged(window_file(d / "Session_new",
                     ["https://x.example/", "https://mail.google.com/mail/u/0/?" + w.MARK + "#inbox",
                      "https://x.example/", "chrome://newtab/", "https://y.example/"],
                     ["chrome://newtab/"],                          # nothing left: no window
                     many), quit_at - 5)
    aged(window_file(d / "Session_after", ["chrome://newtab/"]), quit_at + 20)   # the relaunched Chrome's
    p1 = root / "Profile 1"                                         # "Continue where you left off": skipped
    aged(window_file(p1 / "Sessions" / "Session_1", ["https://p1.example/"]), quit_at - 5)
    (p1 / "Preferences").write_text(json.dumps({"session": {"restore_on_startup": 1}}))
    p2 = root / "Profile 2"
    aged(window_file(p2 / "Sessions" / "Session_2",
                     ["https://voice.google.com/u/0/messages?gvclaudesend=5555550123", "https://p2.example/"]),
         quit_at - 5)
    (p2 / "Preferences").write_text(json.dumps({"session": {"restore_on_startup": 5}}))
    popens = []
    with Stubs(CHROME_DIR=root, _sleep=lambda s: None,
               **{"subprocess.Popen": lambda argv, **k: popens.append(argv)}):
        assert w.restore_windows(quit_at) == 3
        app = w.CHROME_APP
        assert popens == [
            [app, "--profile-directory=Default", "--new-window", "https://x.example/", "https://y.example/"],
            [app, "--profile-directory=Default", "--new-window"] + many[:40],
            [app, "--profile-directory=Profile 2", "--new-window", "https://p2.example/"],
        ], popens
        # no Local State: falls back to chrome_profile
        (root / "Local State").unlink(); popens.clear()
        assert w.chrome_profiles() == ["Default"]
        assert w.restore_windows(quit_at) == 2 and len(popens) == 2

def test_self_heal_config():
    base = {"allowed_senders": ["5555550123"], "gv_number": "5555550100",
            "google_account_email": "you@example.com", "agent_cmd": FAKE_CLAUDE}
    try:
        w.configure(base)
        assert w.AUTO_RESTART is True and w.RESTART_AFTER == 600 and w.IDLE_NEEDED == 600
        assert w.AFTER_RESTART is None and w.ALERT_IMESSAGE is None and w.RESTORE_WINDOWS is True
        assert w.CHROME_DIR == pathlib.Path.home() / "Library/Application Support/Google/Chrome"
        w.configure(dict(base, auto_restart_chrome=False, restart_after_seconds=120, idle_needed_seconds=30,
                         after_chrome_restart="~/hook.sh", alert_imessage="+1 555 555 0123",
                         restore_windows_after_restart=False, chrome_user_data_dir="~/chrome-test"))
        assert w.RESTORE_WINDOWS is False and w.CHROME_DIR == pathlib.Path.home() / "chrome-test"
        assert w.AUTO_RESTART is False and w.RESTART_AFTER == 120 and w.IDLE_NEEDED == 30
        assert w.AFTER_RESTART == str(pathlib.Path.home() / "hook.sh") and w.ALERT_IMESSAGE == "+1 555 555 0123"
        try: w.configure(dict(base, restart_after_seconds="soon"))
        except w.ConfigError as ex: assert "restart_after_seconds" in str(ex)
        else: raise AssertionError("bad restart_after_seconds accepted")
        assert "JavaScript popup" in w.SYSTEM and "window.confirm" in w.SYSTEM
    finally:
        w.configure(base)

for name, fn in list(globals().items()):
    if name.startswith("test_"): fn(); print("ok", name)

import shutil; shutil.rmtree(FAKE, ignore_errors=True)
