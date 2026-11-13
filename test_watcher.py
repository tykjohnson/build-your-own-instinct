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
    got = w.scan(st); assert [b for b, _ in got] == ["follow up text"], got
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
    assert acks[0] == "\U0001F44D on it", replies
    assert acks[1] in ("\U0001F44D on it (running alongside 1 other task)",
                       "\U0001F44D on it"), replies          # the first may already be done
    assert answers == ["claude:first ask", "claude:second ask"], replies

@with_stub_send
def test_parallel_status_cap_and_stop():
    old_max = w.MAX_PARALLEL
    w.MAX_PARALLEL = 2
    try:
        st = {}
        w.dispatch(st, "SLOW one", "h1")
        w.dispatch(st, "SLOW two", "h2")
        assert replies == ["👍 on it", "👍 on it (running alongside 1 other task)"], replies
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

for name, fn in list(globals().items()):
    if name.startswith("test_"): fn(); print("ok", name)

import shutil; shutil.rmtree(FAKE, ignore_errors=True)
