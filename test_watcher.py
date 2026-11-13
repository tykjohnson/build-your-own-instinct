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

# Fake claude: echoes the prompt back as a JSON result, reports a session id.
FAKE_CLAUDE = fake_cli("claude", """
p = args[args.index('-p') + 1]
sid = args[args.index('--resume') + 1] if '--resume' in args else 'sess-1'
if 'LIMIT' in p:
    print(json.dumps({'is_error': True, 'result': 'Claude usage limit reached'})); sys.exit(1)
print(json.dumps({'result': 'claude:' + p.splitlines()[-1], 'session_id': sid}))
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
    # busy job -> must not exit; idle + empty queue -> may exit
    w.CUR["start"] = time.time(); assert not (not w.CUR["start"] and w.JOBS.empty())
    w.CUR["start"] = 0; w.JOBS.put(("x", "h")); assert not (not w.CUR["start"] and w.JOBS.empty())
    w.JOBS.get(); assert (not w.CUR["start"] and w.JOBS.empty())

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

def test_claude_runner_resumes_session():
    st = {}
    assert w.run_job(st, "hi") == "claude:hi"
    assert st["sessions"]["primary:claude"] == "sess-1"
    assert w.run_job(st, "again") == "claude:again"

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

for name, fn in list(globals().items()):
    if name.startswith("test_"): fn(); print("ok", name)

import shutil; shutil.rmtree(FAKE, ignore_errors=True)
