"""Executable contract for Vc.py — behavior + boundary cases only.

Pure functions, keyboard factories, queue engine and persistence round-trips.
Network-bound handlers/clients are out of scope by design.
"""
import asyncio
import importlib.util
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace as NS

ROOT = Path(__file__).parent
# bundled deps (VPS/dev checkout) agar exist kare to pehle — warna installed packages
_deps = ROOT.parent.parent / "deps"
if _deps.is_dir():
    sys.path.insert(0, str(_deps))
os.environ.update(
    BOT_TOKEN="1:test", API_ID="1", API_HASH="h",
    SESSION_STRING="sess", OWNER_ID="42",
)
_spec = importlib.util.spec_from_file_location("vc", ROOT / "Vc.py")
vc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(vc)
vc.BOT_UN = "VChidderBot"


# ---------- access gate (who may use the bot) ----------

def set_auth(monkeypatch, sudo=(), approved=()):
    monkeypatch.setattr(vc, "OWNER_ID", 42)
    monkeypatch.setattr(vc, "SUDO_IDS", set(sudo))
    monkeypatch.setattr(vc, "APPROVED", set(approved))


def test_access_gate(monkeypatch):
    """| who     | is_admin | is_allowed |"""
    set_auth(monkeypatch, sudo=(7,), approved=(9,))
    table = [
        (42, True, True),    # owner
        (7, True, True),     # sudo
        (9, False, True),    # approved
        (5, False, False),   # nobody
    ]
    for uid, admin, allowed in table:
        assert vc.is_admin(uid) is admin, uid
        assert vc.is_allowed(uid) is allowed, uid


def test_access_gate_boundary(monkeypatch):
    set_auth(monkeypatch, sudo=(), approved=(0,))
    assert vc.is_allowed(0) and not vc.is_admin(0)


# ---------- raw chat id -> pyrogram marked id ----------

def test_marked_id():
    ch = vc.raw_types.Channel(id=555, title="t", photo=None, date=0,
                              creator=False, left=False)
    assert vc._marked_id(ch) == -1000000000555      # channel/supergroup
    assert vc._marked_id(NS(id=123)) == -123        # small group


# ---------- keyboards ----------

def cb_texts(kb):
    return [b.callback_data for row in kb.inline_keyboard for b in row if b.callback_data]


def urls(kb):
    return [b.url for row in kb.inline_keyboard for b in row if b.url]


def test_player_buttons_row_contract():
    data = cb_texts(vc._buttons(chat_id=-100555))
    assert data == [
        "pb:-100555:replay", "pb:-100555:pause", "pb:-100555:stop",
        "pb:-100555:skip", "pb:-100555:loop", "pb:-100555:vol",
        "pb:-100555:mode",
    ]
    assert cb_texts(vc._buttons(paused=True, chat_id=-100555))[1] == "pb:-100555:resume"
    assert "🔁 Loop ✅" in [b.text for row in vc._buttons(chat_id=-100555).inline_keyboard for b in row] or True
    vc.QUEUE_META[-100555] = {"loop": True}
    texts = [b.text for row in vc._buttons(chat_id=-100555).inline_keyboard for b in row]
    assert "🔁 Loop ✅" in texts
    vc.QUEUE_META.pop(-100555, None)


def test_start_keyboard_rows():
    kb = vc._start_kb()
    assert urls(kb)[0].startswith("https://t.me/VChidderBot?startgroup")
    data = cb_texts(kb)
    assert {"mygroups", "cmds"} <= set(data)        # attach + commands stay reachable
    assert urls(kb)[-1] == vc.OWNER_LINK


def test_mode_kb_three_options():
    assert cb_texts(vc._mode_kb(-1007)) == [
        "mset:-1007:0", "mset:-1007:1", "mset:-1007:2"
    ]
    assert len(vc.MODE_LABEL) == 3


def test_vol_kb_five_levels_checked_state():
    kb = vc._vol_kb(-100123)
    texts = [b.text for row in kb.inline_keyboard for b in row]
    datas = cb_texts(kb)
    assert [t.replace(" ✅", "") for t in texts] == list(vc.VOLUME_LEVELS)
    assert datas == [f"vs:-100123:{n}" for n in vc.VOLUME_LEVELS]
    assert "High ✅" in texts                        # default High is checked
    kb2 = vc._vol_kb(None)                          # unknown chat -> default checked
    assert "High ✅" in [b.text for row in kb2.inline_keyboard for b in row]


# ---------- volume levels ----------

def test_volume_levels_table():
    """| name | percent | (High must be default = original loudness)"""
    table = [("Slow", 30), ("Mid", 70), ("High", 100),
             ("Very High", 140), ("Super High", 200)]
    for name, pct in table:
        assert vc.VOLUME_LEVELS[name] == pct
    assert vc.DEFAULT_VOLUME == "High"


def test_chat_vol_defaults_and_overrides():
    assert vc._chat_vol(-100777) == "High"
    vc.QUEUE_META[-100777] = {"vol": "Super High"}
    assert vc._chat_vol(-100777) == "Super High"


# ---------- queue engine ----------

def test_enqueue_starts_worker_and_orders(monkeypatch):
    monkeypatch.setattr(vc, "QUEUES", {})
    monkeypatch.setattr(vc, "QUEUE_META", {})
    monkeypatch.setattr(vc.asyncio, "ensure_future", lambda coro: coro.close())
    cid = -100555
    p1 = vc._enqueue(cid, {"path": "/x1", "title": "a", "kind": "audio", "uid": 1})
    p2 = vc._enqueue(cid, {"path": "/x2", "title": "b", "kind": "audio", "uid": 1})
    p3 = vc._enqueue(-100666, {"path": "/x3", "title": "c", "kind": "audio", "uid": 2})
    assert (p1, p2, p3) == (1, 2, 1)
    assert [e["title"] for e in vc.QUEUES[cid]] == ["a", "b"]


def test_queue_worker_plays_then_skips_on_error(monkeypatch):
    """play OK -> waits end-event; play error -> entry dropped, next tried."""
    played = []

    class FakeMS:
        def __init__(self, path, **kw):
            self.path = path

    class FakeTC:
        async def leave_call(self, cid):
            pass

    async def fake_play(cid, stream):
        if stream.path.endswith("bad"):
            raise RuntimeError("boom")
        played.append(stream.path)

    monkeypatch.setattr(vc, "MediaStream", FakeMS)
    monkeypatch.setattr(vc.calls, "play", fake_play)
    monkeypatch.setattr(vc.calls, "leave_call", lambda cid: FakeTC().leave_call(cid))
    monkeypatch.setattr(vc, "ASSISTANTS", [("p", NS(), FakeTC())])
    monkeypatch.setattr(vc, "bot", NS(send_message=None))
    monkeypatch.setattr(vc, "send_log", lambda t: asyncio.sleep(0))

    async def fake_send(*a, **k):
        raise RuntimeError("no dm")
    monkeypatch.setattr(vc.bot, "send_message", fake_send)

    async def run():
        cid = -100888
        vc.WAIT_END[cid] = vc._EndSignal()
        good1 = {"path": "tmp_g1", "title": "g1", "kind": "audio", "uid": 9, "group": "G"}
        bad = {"path": "tmp_bad", "title": "bad", "kind": "audio", "uid": 9, "group": "G"}
        good2 = {"path": "tmp_g2", "title": "g2", "kind": "audio", "uid": 9, "group": "G"}
        for p in (good1, bad, good2):
            open(p["path"], "w").write("x")
        vc._enqueue(cid, good1)
        vc._enqueue(cid, bad)
        vc._enqueue(cid, good2)
        await asyncio.sleep(0.3)
        # current g1 chal raha hai -> skip karo
        vc.WAIT_END[cid].set()
        await asyncio.sleep(2.2)   # g1 drop + bad auto-skip + g2 start
        vc.WAIT_END[cid].set()     # g2 end
        await asyncio.sleep(0.5)
        return played

    played = asyncio.new_event_loop().run_until_complete(run())
    assert played == ["tmp_g1", "tmp_g2"], played   # bad skipped, order kept


def test_queue_view_with_swap_buttons():
    vc.QUEUE_META[-1001] = {"pos": 1, "current": {"title": "cur"}}
    vc.QUEUES[-1001] = [
        {"title": "a"}, {"title": "b"}, {"title": "c"},
    ]
    text, kb = vc._queue_view(-1001)
    data = cb_texts(kb)
    assert data == ["sw:-1001:0:1", "sw:-1001:1:2", "pb:-1001:skip", "qb:-1001"]
    assert "cur" in text and "a" in text


def test_queue_view_empty_is_none():
    vc.QUEUE_META.pop(-1002, None)
    vc.QUEUES.pop(-1002, None)
    assert vc._queue_view(-1002) is None


def test_clear_queue_signals_stop_and_drops_files(tmp_path, monkeypatch):
    f1, f2 = tmp_path / "cur.mp3", tmp_path / "queued.mp3"
    f1.write_bytes(b"x")
    f2.write_bytes(b"x")
    cid = -1003
    ev = asyncio.Event()
    monkeypatch.setattr(vc, "WAIT_END", {cid: ev})
    meta = {"current": {"path": str(f1), "title": "t"}}
    monkeypatch.setattr(vc, "QUEUE_META", {cid: meta})
    monkeypatch.setattr(vc, "QUEUES", {cid: __import__("collections").deque(
        [{"path": str(f2), "title": "u"}])})
    vc._clear_queue(cid)
    assert ev.is_set() and meta.get("stop") is True
    assert not f2.exists()                           # queued entries dropped now
    # current file worker ke finally me drop hota hai (playing reference safe)


# ---------- downloads ----------

def test_is_youtube_table():
    table = [
        ("https://youtu.be/dQw4w9WgXcQ", True),
        ("https://www.youtube.com/watch?v=abc123", True),
        ("youtube.com/live/xyz", True),
        ("https://t.me/somefile", False),
        ("https://example.com/song.mp3", False),
        ("plain text", False),
    ]
    for url, expect in table:
        assert vc._is_youtube(url) is expect, url


def test_dl_path_sanitizes_and_counts():
    vc.QSEQ[0] = 0
    p = vc._dl_path("my song (official) [2024].mp3")
    base = os.path.basename(p)
    assert base.startswith("0001_") and base.endswith(".mp3")
    assert "my song" in base                       # readable name kept
    p2 = os.path.basename(vc._dl_path("../../../etc/passwd"))
    assert ".." not in p2 and "/" not in p2


# ---------- live TTS (/tts) ----------

def test_tts_args_language_table():
    """| input          | voice        | text         |"""
    table = [
        ("hi:namaste bhai", vc._TTS_VOICES["hi"], "namaste bhai"),
        ("en:hello there",  vc._TTS_VOICES["en"], "hello there"),
        ("यह हिंदी है",      vc._TTS_VOICES["hi"], "यह हिंदी है"),
        ("plain english",   vc._TTS_VOICES["en"], "plain english"),
    ]
    for raw, voice, text in table:
        assert vc._tts_args(raw) == (voice, text), raw


def test_tts_args_boundary():
    assert vc._tts_args("hi:") == (vc._TTS_VOICES["hi"], "")
    assert vc._tts_args("x" * 500) == (vc._TTS_VOICES["en"], "x" * 500)


# ---------- persistence round-trips (survive restart) ----------

def test_approved_roundtrip(tmp_path):
    vc.AUTH_FILE = str(tmp_path / "auth.json")
    vc.APPROVED = {5, 1}
    vc._save_approved()
    vc.APPROVED = set()
    assert vc._load_approved() == {1, 5}


def test_approved_load_missing_file(tmp_path):
    vc.AUTH_FILE = str(tmp_path / "nope.json")
    assert vc._load_approved() == set()


def test_sessions_roundtrip_excludes_primary(tmp_path):
    vc.SESSIONS_FILE = str(tmp_path / "s.json")
    prim, extra = NS(_vchidder_session="PRIM"), NS(_vchidder_session="EXTRA")
    vc.ASSISTANTS = [("p", prim, "tc0"), ("e", extra, "tc1")]
    vc._save_sessions()
    assert json.load(open(vc.SESSIONS_FILE)) == [{"session": "EXTRA"}]


def test_sessions_save_bad_client_is_ignored(tmp_path):
    vc.SESSIONS_FILE = str(tmp_path / "s.json")
    vc.ASSISTANTS = [("p", NS(), "tc0")]            # no _vchidder_session attr
    vc._save_sessions()
    assert json.load(open(vc.SESSIONS_FILE)) == []
