"""Executable contract for Vc.py — behavior + boundary cases only.

Pure functions, keyboard factories and persistence round-trips.
Handlers/clients (network-bound) are out of scope by design.
"""
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
    # approved stays allowed even when sudo/owner sets change; 0/None ids never pass
    set_auth(monkeypatch, sudo=(), approved=(0,))
    assert vc.is_allowed(0) and not vc.is_admin(0)


# ---------- raw chat id -> pyrogram marked id ----------

def test_marked_id():
    ch = vc.raw_types.Channel(id=555, title="t", photo=None, date=0,
                              creator=False, left=False)
    assert vc._marked_id(ch) == -1000000000555      # channel/supergroup
    assert vc._marked_id(NS(id=123)) == -123        # small group


# ---------- keyboard + text factories ----------

def cb_texts(kb):
    return [b.callback_data for row in kb.inline_keyboard for b in row if b.callback_data]


def urls(kb):
    return [b.url for row in kb.inline_keyboard for b in row if b.url]


def test_player_buttons_default():
    data = cb_texts(vc._buttons())
    assert data == ["replay", "pause", "stop"]      # order is the UI contract


def test_player_buttons_paused_swaps_toggle_only():
    data = cb_texts(vc._buttons(paused=True))
    assert data == ["replay", "resume", "stop"]


def test_start_keyboard_rows():
    kb = vc._start_kb()
    assert urls(kb)[0].startswith("https://t.me/VChidderBot?startgroup")
    data = cb_texts(kb)
    assert {"mygroups", "cmds"} <= set(data)        # attach + commands stay reachable
    assert urls(kb)[-1] == vc.OWNER_LINK


def test_back_kb_single_button():
    assert cb_texts(vc._back_kb()) == ["backstart"]


def test_start_text_and_cmds_mention_owner_commands():
    text = vc._start_text("Nakshu")
    assert "Nakshu" in text and "VC" in text
    cmds = vc.CMDS_TEXT
    for cmd in ("/playing", "/pause", "/stop", "/status", "/micon", "/ac", "/addsession"):
        assert cmd in cmds, cmd


# ---------- state cleanup ----------

def test_clear_state_full(tmp_path):
    f = tmp_path / "song.mp3"
    f.write_bytes(b"x")
    vc.user_data[9] = {"audio": str(f), "group": "g"}
    vc.active_calls[9] = -100
    vc.chat_calls[-100] = 9
    vc.paused_calls.add(-100)
    assert vc._clear_state(9) == -100
    assert not f.exists()
    assert 9 not in vc.user_data and -100 not in vc.chat_calls
    assert -100 not in vc.paused_calls


def test_clear_state_unknown_user_is_noop():
    assert vc._clear_state(31337) is None


# ---------- persistence round-trips (survive restart) ----------

def test_approved_roundtrip(tmp_path, monkeypatch):
    vc.AUTH_FILE = str(tmp_path / "auth.json")
    vc.APPROVED = {5, 1}
    vc._save_approved()
    vc.APPROVED = set()
    assert vc._load_approved() == {1, 5}


def test_approved_load_missing_file(tmp_path):
    vc.AUTH_FILE = str(tmp_path / "nope.json")
    assert vc._load_approved() == set()


def test_mute_flag_roundtrip(tmp_path):
    vc.MUTED_FILE = str(tmp_path / "muted.json")
    vc.MUTE_FLAG = {-100, -200}
    vc._save_mute_flag()
    vc.MUTE_FLAG = set()
    assert vc._load_mute_flag() == {-200, -100}


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
