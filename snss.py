"""Read a Chrome session file (Profile/Sessions/Session_*, format "SNSS") into its open windows.

  python3 snss.py <Session_file>      prints [[url, url, ...], ...], one list per window

The file is a 4-byte "SNSS" magic, an int32 version, then commands: uint16 size, uint8 command
id, and size-1 bytes of payload (little-endian). Chrome appends a command for every change and
rewrites the file from its current state now and then, so replaying the commands in order gives
the windows that were open when the file was last written. Only the commands needed to rebuild
normal windows' tab lists are read; ids follow Chromium's session_service_commands.cc.
"""
import struct, sys, json

SET_TAB_WINDOW = 0          # (window_id, tab_id)
SET_TAB_INDEX = 2           # (tab_id, index in window)
UPDATE_TAB_NAVIGATION = 6   # pickle: (tab_id, nav_index, url, ...)
SET_SELECTED_NAV = 7        # (tab_id, nav_index)
SET_WINDOW_TYPE = 9         # (window_id, type); 0 = normal browser window
SET_PINNED = 12             # (tab_id, bool)
TAB_CLOSED = 16             # (tab_id, close_time)
WINDOW_CLOSED = 17          # (window_id, close_time)

def pickle_str(buf, off):
    n = struct.unpack_from("<i", buf, off)[0]; off += 4
    s = buf[off:off + n].decode("utf-8", "replace"); off += (n + 3) & ~3
    return s, off

def parse(path):
    """Open normal windows -> their tab URLs in tab-strip order (each tab's current page).
    Closed tabs, closed windows, popups and app windows are left out."""
    with open(path, "rb") as f: b = f.read()
    if b[:4] != b"SNSS": raise ValueError("not a Chrome session file: %s" % path)
    off = 8
    tab_win, tab_idx, navs, sel_nav, win_type = {}, {}, {}, {}, {}
    closed_tabs, closed_wins = set(), set()
    while off + 2 <= len(b):
        size = struct.unpack_from("<H", b, off)[0]; off += 2
        if size == 0 or off + size > len(b): break
        cid = b[off]; p = b[off + 1: off + size]; off += size
        try:
            if cid == SET_TAB_WINDOW and len(p) >= 8:
                w, t = struct.unpack_from("<ii", p); tab_win[t] = w
            elif cid == SET_TAB_INDEX and len(p) >= 8:
                t, i = struct.unpack_from("<ii", p); tab_idx[t] = i
            elif cid == UPDATE_TAB_NAVIGATION:
                t, i = struct.unpack_from("<ii", p, 4)      # after the pickle's size header
                url, _ = pickle_str(p, 12)
                navs.setdefault(t, {})[i] = url
            elif cid == SET_SELECTED_NAV and len(p) >= 8:
                t, i = struct.unpack_from("<ii", p); sel_nav[t] = i
            elif cid == SET_WINDOW_TYPE and len(p) >= 8:
                w, ty = struct.unpack_from("<ii", p); win_type[w] = ty
            elif cid == TAB_CLOSED and len(p) >= 4:
                closed_tabs.add(struct.unpack_from("<i", p)[0])
            elif cid == WINDOW_CLOSED and len(p) >= 4:
                closed_wins.add(struct.unpack_from("<i", p)[0])
        except (struct.error, ValueError):
            pass
    wins = {}
    for t, w in tab_win.items():
        if t in closed_tabs or w in closed_wins or not navs.get(t): continue
        n = navs[t]
        url = n.get(sel_nav.get(t), n[max(n)])
        wins.setdefault(w, []).append((tab_idx.get(t, 1 << 30), t, url))
    return [[u for _, _, u in sorted(tabs)] for w, tabs in wins.items() if win_type.get(w, 0) == 0]

if __name__ == "__main__":
    print(json.dumps(parse(sys.argv[1]), indent=1))
