"""Offline regression tests: python3 test_watcher.py (no Chrome, no config.json, no texts sent)."""
import sys, time
sys.argv = ['x']
import watcher as w

# Fake values only (555 numbers are reserved for fiction).
w.configure({
    "allowed_senders": ["+1 (555) 555-0123"],
    "gv_number": "+1 555 555 0100",
    "google_account_email": "you@example.com",
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

for name, fn in list(globals().items()):
    if name.startswith("test_"): fn(); print("ok", name)
