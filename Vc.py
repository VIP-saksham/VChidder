import asyncio
import json
import os
import re
import shutil
import sys
import time

# single explicit event loop shared by all clients (avoids "attached to a different loop")
loop = asyncio.new_event_loop()
asyncio.set_event_loop(loop)

from collections import deque

import aiohttp
from dotenv import load_dotenv

import edge_tts

load_dotenv()

from pyrogram import Client, enums, filters, utils
from pyrogram.raw import functions as raw_functions
from pyrogram.raw import types as raw_types
from pyrogram.errors import (
    InviteHashExpired,
    InviteHashInvalid,
)
from pyrogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from pytgcalls import PyTgCalls
from pytgcalls.types.stream import MediaStream
from pytgcalls.types.stream import StreamEnded

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
API_ID = int(os.getenv("API_ID") or 0)
API_HASH = os.getenv("API_HASH", "").strip()
SESSION_STRING = os.getenv("SESSION_STRING", "").strip()
# assistant (player) accounts can run under a custom app identity (e.g. WeBGram)
ASSISTANT_API_ID = int(os.getenv("ASSISTANT_API_ID") or 0) or API_ID
ASSISTANT_API_HASH = os.getenv("ASSISTANT_API_HASH", "").strip() or API_HASH
OWNER_ID = int(os.getenv("OWNER_ID") or 0)
SUDO_IDS = {
    int(x)
    for x in os.getenv("SUDO_USERS", "").replace(" ", "").split(",")
    if x.isdigit()
}
AUTH_FILE = "/root/VChidder/auth.json"
LOG_GROUP_ID = int(os.getenv("LOG_GROUP_ID") or 0)
SILENT_FILE = os.getenv("SILENT_FILE", "/root/VChidder/silent.mp3")
MIC_REFRESH = int(os.getenv("MIC_REFRESH") or 1500)  # sec — silent stream auto-refresh
SESSIONS_FILE = os.getenv("SESSIONS_FILE", "/root/VChidder/sessions.json")
DOWNLOAD_DIR = os.getenv("DOWNLOAD_DIR", "/root/VChidder/downloads")
TTS_DIR = os.getenv("TTS_DIR", "/root/VChidder/tts")
BASE_DIR = os.getenv("VC_BASE_DIR", "/root/VChidder")
CHANNEL_LINK = os.getenv("CHANNEL_LINK", "https://t.me/TheHellBots")
SUPPORT_LINK = os.getenv("SUPPORT_LINK", "https://t.me/SUNSHINE_GC")
OWNER_LINK = os.getenv("OWNER_LINK", "https://t.me/Truenakshu")
MAX_TG_SIZE = 1900 * 1024 * 1024  # stay under Telegram's 2GB bot limit

if not BOT_TOKEN or not API_ID or not API_HASH:
    raise SystemExit("❌ BOT_TOKEN / API_ID / API_HASH missing — set them in .env")

if not SESSION_STRING:
    raise SystemExit("❌ SESSION_STRING missing in .env (primary assistant session)")

if not OWNER_ID:
    raise SystemExit("❌ OWNER_ID missing in .env (your Telegram user id)")

# ---------------- volume levels (user-facing names) ----------------

VOLUME_LEVELS = {
    "Slow": 30,
    "Mid": 70,
    "High": 100,          # default — original loudness
    "Very High": 140,
    "Super High": 200,    # faad level 🔊
}
DEFAULT_VOLUME = "High"

# assistant join modes
MODE_SINGLE = 0     # sirf primary
MODE_ONBYONE = 1    # ek band -> dusra
MODE_ALL = 2        # sab ek sath
MODE_LABEL = {
    MODE_SINGLE: "1️⃣ Single Assistant",
    MODE_ONBYONE: "2️⃣ One By One",
    MODE_ALL: "3️⃣ All In One",
}

# ---------------- auth store (owner + sudo + approved users) ----------------

def _load_approved():
    try:
        with open(AUTH_FILE) as f:
            return set(json.load(f).get("approved", []))
    except Exception:
        return set()


def _save_approved():
    try:
        with open(AUTH_FILE, "w") as f:
            json.dump({"approved": sorted(APPROVED)}, f)
    except Exception as e:
        print("auth save failed:", e)


APPROVED = _load_approved()
START_TS = time.time()
BOT_UN = ""
pending_requests = set()  # users who asked for access (dedupe)


def is_admin(uid):
    return uid == OWNER_ID or uid in SUDO_IDS


def is_allowed(uid):
    return is_admin(uid) or uid in APPROVED


DENIED_TEXT = (
    "✦━━━━━━━━━━━━━━━━━━✦\n"
    "      🔐 **Access Denied**\n"
    "✦━━━━━━━━━━━━━━━━━━✦\n\n"
    "Ye bot sirf **approved users** ke liye hai.\n\n"
    "📩 Approve karne ke liye apni ID\n"
    "owner ko bhejo — request auto chali gayi hai!\n\n"
    "🆔 **Your ID:** `{my_id}`"
)

# ---------------- clients ----------------

bot = Client(
    "vc_bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN
)

userbot = Client(
    "vc_userbot",
    api_id=ASSISTANT_API_ID,
    api_hash=ASSISTANT_API_HASH,
    session_string=SESSION_STRING
)

calls = PyTgCalls(userbot)

# ---------------- playback state ----------------

user_data = {}       # user id -> flow state (group selection)
active_calls = {}    # user id -> chat id ( requester -> chat )
chat_calls = {}      # chat id -> requester uid
paused_calls = set()

QUEUES = {}          # chat id -> deque of entries
QUEUE_META = {}      # chat id -> {current, mode, worker, extras, vol}
QSEQ = [0]           # download naming counter
WAIT_END = {}        # chat id -> asyncio.Event (stream end / skip signal)
YT_CACHE = {}        # youtube url -> (path, title)
DL_LOCKS = {}        # cache key -> asyncio.Lock (same-url dedupe)

LAST_CHAT = {}       # uid -> last selected chat (for /micon, /tts)
CHAT_NAMES = {}      # chat_id -> group name
INVITE_HASH = {}     # chat_id -> invite hash (private groups, for extra assistants)
ASSISTANTS = []      # [(name, Client, PyTgCalls)] — primary index 0
PRESENCE_JOINED = set()  # {(chat_id, assistant_idx)} — VC me joined
PRESENCE_ONLY = set()    # chats jaha /micon presence chal rahi hai


def _save_sessions():
    try:
        extras = []
        for name, c, tc in ASSISTANTS[1:]:
            s = getattr(c, "_vchidder_session", None)
            if s:
                extras.append({"session": s})
        with open(SESSIONS_FILE, "w") as f:
            json.dump(extras, f, indent=1)
    except Exception as e:
        print("sessions save failed:", e)


# ---------------- keyboards ----------------

def _buttons(paused=False):
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("⏮ Replay", callback_data="replay"),
                InlineKeyboardButton(
                    "⏯ Resume" if paused else "⏯ Pause",
                    callback_data="resume" if paused else "pause"
                ),
                InlineKeyboardButton("⏹ Stop", callback_data="stop"),
                InlineKeyboardButton("⏭ Skip", callback_data="skip"),
                InlineKeyboardButton("🎚 Vol", callback_data="volmenu"),
            ],
            _support_row(),
        ]
    )


def _vol_kb(chat_id):
    row1, row2 = [], []
    for i, (name, _) in enumerate(VOLUME_LEVELS.items()):
        btn = InlineKeyboardButton(
            f"{name} ✅" if _chat_vol(chat_id) == name else name,
            callback_data=f"volset:{name}"
        )
        (row1 if i < 3 else row2).append(btn)
    return InlineKeyboardMarkup([row1, row2])


def _chat_vol(chat_id):
    return QUEUE_META.get(chat_id, {}).get("vol", DEFAULT_VOLUME)


def _support_row():
    return [
        InlineKeyboardButton("✦ Updates", url=CHANNEL_LINK),
        InlineKeyboardButton("✦ Support", url=SUPPORT_LINK),
    ]


def _start_kb():
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("➕ Add Me To Your Group", url=f"https://t.me/{BOT_UN}?startgroup=true")],
            [
                InlineKeyboardButton("🔗 Attach Group", callback_data="mygroups"),
                InlineKeyboardButton("📖 Commands", callback_data="cmds"),
            ],
            _support_row(),
            [InlineKeyboardButton("👑 Owner", url=OWNER_LINK)],
        ]
    )


def _back_kb():
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("⬅ Back", callback_data="backstart")]]
    )


def _mode_kb():
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("1️⃣ Single Assistant", callback_data=f"mode:{MODE_SINGLE}")],
            [InlineKeyboardButton("2️⃣ One By One", callback_data=f"mode:{MODE_ONBYONE}")],
            [InlineKeyboardButton("3️⃣ All In One", callback_data=f"mode:{MODE_ALL}")],
        ]
    )


def _start_text(name):
    return (
        "╔═══════════════════════╗\n"
        "   🎵 **V C H I D D E R** 🎵\n"
        "  ✦ Anonymous VC Player ✦\n"
        "╚═══════════════════════╝\n\n"
        f"👋 Hey **{name}**, welcome!\n\n"
        "🎧 Main kisi bhi group ke voice chat me\n"
        "music / video / YouTube **stream** karta hoon\n"
        "bilkul **anonymous** 👻\n\n"
        "✨ **Quick Start**\n"
        "  ➊ Group ka @username ya invite/YT link bhejo\n"
        "  ➋ Audio/video file bhejo ya link paste karo\n"
        "  ➌ Queue + Volume + Mode — sab buttons se 🎛\n\n"
        "🎯 **Features**\n"
        "  🎵 Music • 🎬 Video • ▶️ YouTube Audio\n"
        "  🔊 5 Volume Levels • 📋 Smart Queue\n"
        "  👥 Multi-Assistant • 🔇 Mic-Off Mode\n\n"
        "⚡ Super Fast • 🔒 Secure • 🎭 Anonymous"
    )


CMDS_TEXT = (
    "📖 **Commands Guide**\n\n"
    "🎵 **Player**\n"
    "  ▶️ /playing — ab kya chal raha hai\n"
    "  ⏸ /pause  •  ▶️ /resume  •  ⏹ /stop\n"
    "  ⏭ /sk — current skip\n"
    "  📋 /q — queue dekho (swap buttons ke sath)\n"
    "  🔄 /sw 1 2 — position swap karo\n"
    "  🔊 /vol — volume panel (Slow→Super High)\n"
    "  🗣 /tts <text> — VC me bolo (hi:/en: prefix)\n\n"
    "📊 **Info**\n"
    "  📊 /status — bot stats & uptime\n\n"
    "👑 **Owner Only**\n"
    "  🎙 /micon • /micoff — presence toggle\n"
    "  🎛 /ac — active calls panel\n"
    "  👥 /addsession • /sessions • /delsession\n"
    "  ✅ /approve • /unapprove • /approved"
)


# ---------------- logging to log group ----------------

SEEN_USERS = set()


def _mention(u):
    name = (u.first_name or "User").replace("[", "(").replace("]", ")")
    return f"[{name}](tg://user?id={u.id})"


def _uname(u):
    return f"@{u.username}" if u.username else "—"


async def send_log(text):
    if not LOG_GROUP_ID:
        return
    try:
        await bot.send_message(
            LOG_GROUP_ID,
            text,
            disable_web_page_preview=True
        )
    except Exception as e:
        print("log send failed:", e)


# ---------------- chat id helpers ----------------

def _marked_id(raw_chat):
    """Convert a raw Chat/Channel object to a pyrogram marked chat id."""
    if isinstance(raw_chat, raw_types.Channel):
        return utils.get_channel_id(raw_chat.id)
    return -raw_chat.id


async def _resolve_group(m: Message, text: str):
    """@username / t.me link -> (chat_id, title). Small groups ko dialogs me dhundo."""
    grp = text.split("t.me/")[-1].replace("@", "").strip().rstrip("/")
    if not grp or " " in grp or "/" in grp:
        return None, None
    try:
        chat = await userbot.get_chat(grp)
        return chat.id, chat.title or grp
    except Exception:
        pass
    # small private group — dialogs scan
    target = grp.lower()
    async for d in userbot.get_dialogs(limit=200):
        ch = d.chat
        if ch and ch.type in (enums.ChatType.GROUP, enums.ChatType.SUPERGROUP):
            uname = (ch.username or "").lower()
            if uname == target or (ch.title or "").lower() == target:
                return ch.id, ch.title
    return None, None


async def _snapshot_group_ids():
    ids = set()
    async for d in userbot.get_dialogs(limit=100):
        chat = d.chat
        if chat and chat.type in (
            enums.ChatType.GROUP,
            enums.ChatType.SUPERGROUP,
            enums.ChatType.CHANNEL
        ):
            ids.add(chat.id)
    return ids


async def _find_new_chat(uid):
    """Find a group/supergroup dialog that appeared after the join attempt."""
    base = user_data.get(uid, {}).get("baseline") or set()
    async for d in userbot.get_dialogs(limit=100):
        chat = d.chat
        if chat and chat.type in (
            enums.ChatType.GROUP,
            enums.ChatType.SUPERGROUP,
            enums.ChatType.CHANNEL
        ) and chat.id not in base:
            return chat
    return None


async def _resolve_chat_id(uid):
    info = user_data[uid]
    if info.get("chat_id"):
        return info["chat_id"]
    if info.get("invite"):
        # re-check invite hash: admin approval turns it into ChatInviteAlready
        h = info.get("hash")
        if h:
            try:
                inv = await userbot.invoke(
                    raw_functions.messages.CheckChatInvite(hash=h)
                )
                if isinstance(inv, (raw_types.ChatInviteAlready, raw_types.ChatInvitePeek)):
                    if getattr(inv, "chat", None):
                        marked = _marked_id(inv.chat)
                        info["chat_id"] = marked
                        info["group"] = inv.chat.title or info.get("group") or str(marked)
                        return marked
            except Exception:
                pass
        chat = await _find_new_chat(uid)
        if not chat:
            raise Exception("Join request abhi approve nahi hua ⏳")
        info["chat_id"] = chat.id
        return chat.id
    chat = await userbot.get_chat(info["group"])
    info["chat_id"] = chat.id
    return chat.id


def _deny(m: Message):
    return m.reply_text(DENIED_TEXT.format(my_id=m.from_user.id))


# ---------------- multi-assistant engine ----------------

async def _start_assistant(session, idx):
    """Naya assistant client + PyTgCalls engine start karta hai. Returns (name, c, tc)."""
    c = Client(
        f"vchidder_assist_{idx}",
        api_id=ASSISTANT_API_ID,
        api_hash=ASSISTANT_API_HASH,
        session_string=session,
        in_memory=True
    )
    c._vchidder_session = session
    await c.start()
    me = await c.get_me()
    tc = PyTgCalls(c)
    await tc.start()
    name = f"{me.first_name or 'Assistant'} (@{me.username or me.id})"
    return name, c, tc


async def _ensure_assistant_member(idx, chat_id):
    """Extra assistant group ka member nahi -> invite hash/username se join karao."""
    name, c, tc = ASSISTANTS[idx]
    try:
        await c.get_chat(chat_id)
        return  # already member
    except Exception:
        pass
    h = INVITE_HASH.get(chat_id)
    if h:
        try:
            await c.invoke(raw_functions.messages.ImportChatInvite(hash=h))
            await asyncio.sleep(1.5)
            return
        except Exception:
            pass  # already participant?
    try:
        ch = await userbot.get_chat(chat_id)
        if getattr(ch, "username", None):
            await c.join_chat(ch.username)
            await asyncio.sleep(1.5)
    except Exception:
        pass


async def _assistant_join(chat_id, idx, media=None):
    """Assistant idx ko VC me join karao (silent presence ya media stream)."""
    if idx >= len(ASSISTANTS):
        return False
    if (chat_id, idx) in PRESENCE_JOINED:
        return True
    try:
        if idx > 0:
            await _ensure_assistant_member(idx, chat_id)
        tc = ASSISTANTS[idx][2]
        src = media or SILENT_FILE
        await tc.play(chat_id, MediaStream(src))
        PRESENCE_JOINED.add((chat_id, idx))
        return True
    except Exception as e:
        print(f"assistant join error chat={chat_id} idx={idx}:", str(e)[:120])
        return False


async def _assistant_leave(chat_id, idx):
    try:
        await ASSISTANTS[idx][2].leave_call(chat_id)
    except Exception:
        pass
    PRESENCE_JOINED.discard((chat_id, idx))


async def _restore_mic(chat_id):
    """Playback khatam -> jis chat me presence ON thi, wapas silent stream."""
    await asyncio.sleep(2)
    if chat_id in PRESENCE_ONLY and chat_id not in QUEUES:
        for idx in range(len(ASSISTANTS)):
            if (chat_id, idx) not in PRESENCE_JOINED:
                await _assistant_join(chat_id, idx)


async def _mic_keepalive_worker():
    """Har MIC_REFRESH sec me presence refresh — VC kabhi end na ho."""
    while True:
        await asyncio.sleep(MIC_REFRESH)
        for (chat_id, idx) in list(PRESENCE_JOINED):
            if chat_id in WAIT_END:
                continue  # playback chal raha hai — disturb nahi karna
            try:
                tc = ASSISTANTS[idx][2]
                await tc.leave_call(chat_id)
                await asyncio.sleep(2)
                await tc.play(chat_id, MediaStream(SILENT_FILE))
                print(f"🎙 presence refreshed: {chat_id} asst#{idx}")
            except Exception as e:
                print(f"presence keepalive error {chat_id}/{idx}:", e)


# ---------------- queue engine ----------------

def _drop_entry(entry):
    if not entry:
        return
    path = entry.get("path")
    if path and os.path.exists(path) and path not in (SILENT_FILE,):
        try:
            os.remove(path)
        except OSError:
            pass


async def _apply_volume(chat_id):
    """Play ke thodi der baad live volume lagao (1-200)."""
    vol = VOLUME_LEVELS.get(_chat_vol(chat_id), 100)
    if vol == 100:
        return
    await asyncio.sleep(2)
    try:
        await calls.change_volume_call(chat_id, vol)
    except Exception as e:
        print("volume apply error:", str(e)[:100])


async def _queue_worker(chat_id):
    meta = QUEUE_META.setdefault(chat_id, {})
    try:
        while True:
            q = QUEUES.get(chat_id)
            if not q:
                break
            entry = q.popleft()
            try:
                await calls.play(chat_id, MediaStream(entry["path"]))
            except Exception as e:
                print("play error, skipping:", str(e)[:150])
                try:
                    await bot.send_message(
                        entry["uid"],
                        f"⏭ **{entry['title'][:32]}** skip hua:\n`{str(e)[:100]}`"
                    )
                except Exception:
                    pass
                _drop_entry(entry)
                continue

            meta["current"] = entry
            meta["pos"] = len(q) + 1
            paused_calls.discard(chat_id)
            active_calls[entry["uid"]] = chat_id
            chat_calls[chat_id] = entry["uid"]
            asyncio.ensure_future(_apply_volume(chat_id))

            # assistant mode ke hisab se extra presence
            mode = meta.get("mode", MODE_SINGLE)
            if mode == MODE_ONBYONE and len(ASSISTANTS) > 1:
                joined = [i for (c, i) in PRESENCE_JOINED if c == chat_id]
                nxt = 1 + (max(joined) if joined else 0)
                if nxt < len(ASSISTANTS):
                    await _assistant_join(chat_id, nxt)
            elif mode == MODE_ALL:
                for idx in range(1, len(ASSISTANTS)):
                    await _assistant_join(chat_id, idx)

            icon = "🎬" if entry["kind"] == "video" else "🎵"
            pos = meta.get("pos", 1)
            if entry["kind"] == "yt":
                icon = "▶️"
            try:
                await bot.send_message(
                    entry["uid"],
                    "╭────── 🎧 **Now Playing** ──────╮\n"
                    f"  {icon} **{entry['title'][:34]}**\n"
                    f"  📻 Group: **{entry.get('group', '?')}**\n"
                    f"  🔊 Volume: {_chat_vol(chat_id)}\n"
                    f"  📋 Queue: #{pos}" + (
                        f" • ⏭ {len(q)} baaki" if q else ""
                    ) + "\n"
                    "  🎭 Mode: Anonymous\n"
                    "╰────────────────────────────╯",
                    reply_markup=_buttons(),
                )
            except Exception:
                pass
            await send_log(
                f"{icon} **Now Playing**\n\n"
                f"👤 User: [{entry.get('uname', 'User')}](tg://user?id={entry.get('uid', 0)})\n"
                f"📁 File: `{entry['title'][:40]}`\n"
                f"📻 Group: {entry.get('group', '?')}"
            )

            # stream end / skip ka intezaar
            ev = WAIT_END.get(chat_id)
            if ev:
                try:
                    await asyncio.wait_for(ev.wait(), timeout=3600 * 6)
                except asyncio.TimeoutError:
                    pass
                ev.clear()
                if meta.get("stop"):
                    break
            _drop_entry(entry)
            meta["current"] = None
            await asyncio.sleep(1)
    except asyncio.CancelledError:
        pass
    finally:
        WAIT_END.pop(chat_id, None)
        try:
            await calls.leave_call(chat_id)
        except Exception:
            pass
        for idx in range(len(ASSISTANTS)):
            if (chat_id, idx) in PRESENCE_JOINED and (chat_id, idx) not in _presence_only_pairs():
                await _assistant_leave(chat_id, idx)
        if meta.pop("worker", None):
            pass
        cur = meta.get("current")
        if cur:
            _drop_entry(cur)
            meta["current"] = None
        asyncio.ensure_future(_restore_mic(chat_id))


def _presence_only_pairs():
    return {(c, i) for (c, i) in PRESENCE_JOINED if c in PRESENCE_ONLY}


def _enqueue(chat_id, entry):
    q = QUEUES.setdefault(chat_id, deque())
    q.append(entry)
    meta = QUEUE_META.setdefault(chat_id, {})
    pos = len(q)
    meta.setdefault("mode", MODE_SINGLE)
    w = meta.get("worker")
    if w is None or w.done():
        meta["worker"] = asyncio.ensure_future(_queue_worker(chat_id))
    return pos


# ---------------- downloads (super fast) ----------------

YT_RE = re.compile(
    r"^((?:https?:)?//)?((?:www|m)\.)?"
    r"(youtube(-nocookie)?\.com|youtu\.be)"
    r"(/(?:[\w\-]+\?v=|embed/|live/|v/)?)"
    r"([\w\-]+)(\S+)?$",
    re.I,
)
HTTP_RE = re.compile(r"^https?://\S+$", re.I)


def _is_youtube(text):
    return bool(YT_RE.match(text.strip()))


def _dl_path(name):
    QSEQ[0] += 1
    safe = re.sub(r"[^\w\-. ()\[\]]+", "_", name)[:60] or "media"
    return os.path.join(DOWNLOAD_DIR, f"{QSEQ[0]:04d}_{safe}".replace("..", "_"))


def _ytdlp_bin():
    """yt-dlp binary dhundo: PATH, phir venv bin, warna '' (python -m fallback)."""
    p = shutil.which("yt-dlp")
    if p:
        return p
    venv_root = os.path.dirname(os.path.dirname(sys.executable))
    cand = os.path.join(venv_root, "bin", "yt-dlp")
    return cand if os.path.exists(cand) else ""


COOKIES_FILE = os.getenv("YT_COOKIES", "/root/VChidder/yt_cookies.txt")


def _yt_extra_args():
    """Cookies file exist karti hai to yt-dlp ko auth ke liye de do (bot-check bypass)."""
    if COOKIES_FILE and os.path.exists(COOKIES_FILE) and os.path.getsize(COOKIES_FILE) > 50:
        return ["--cookies", COOKIES_FILE]
    return []


async def _download_yt(url, key):
    """YouTube -> sirf audio, fastest flags. Returns (path, title)."""
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    out_tmpl = os.path.join(DOWNLOAD_DIR, "yt_%(id)s.%(ext)s")
    binp = _ytdlp_bin()
    head = [binp] if binp else [sys.executable, "-m", "yt_dlp"]
    cmd = head + [
        "--no-playlist", "--quiet", "--no-warnings",
        "-f", "ba[ext=m4a]/ba/b",
        "-N", "8",  # parallel fragment connections
        *_yt_extra_args(),
        "-o", out_tmpl,
        "--print", "%(title)s",
        "--print", "after_move:%(filepath)s",
        url,
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=900)
    except asyncio.TimeoutError:
        proc.kill()
        raise Exception("YT download timeout (15 min)")
    if proc.returncode != 0:
        errtxt = (stderr or b"").decode()[:180]
        if "Sign in to confirm" in errtxt and not _yt_extra_args():
            raise Exception(
                "YouTube ne is server ka IP flag kar diya hai 🤖\n\n"
                "Fix (1 min): browser se YouTube cookies export karo\n"
                "(Get cookies.txt extension, YT me logged-in ho)\n"
                f"aur file daalo: `{COOKIES_FILE}`\n"
                "phir link dobara bhejo — super fast chalega ⚡"
            )
        raise Exception("YT download failed:\n" + errtxt)
    lines = [l for l in stdout.decode().strip().splitlines() if l.strip()]
    if len(lines) < 2:
        raise Exception("YT: title/path nahi mila")
    title, path = lines[0], lines[-1]
    if not os.path.exists(path):
        raise Exception("YT: file missing after download")
    YT_CACHE[key] = (path, title)
    return path, title


async def _download_direct(url, key):
    """Direct link -> aiohttp streaming (ya aria2c agar available ho)."""
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    fname = url.split("?")[0].rstrip("/").split("/")[-1] or "file.mp3"
    dest = _dl_path(fname)

    if shutil.which("aria2c"):
        proc = await asyncio.create_subprocess_exec(
            "aria2c", "-x", "16", "-s", "16", "-k", "1M",
            "--console-log-level=error", "--summary-interval=0",
            "-d", os.path.dirname(dest), "-o", os.path.basename(dest),
            url,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=900)
        except asyncio.TimeoutError:
            proc.kill()
            raise Exception("download timeout (15 min)")
        if proc.returncode != 0 or not os.path.exists(dest):
            raise Exception("download failed:\n" + (stderr or b"").decode()[:150])
        return dest, fname

    timeout = aiohttp.ClientTimeout(total=None, sock_connect=30, sock_read=60)
    async with aiohttp.ClientSession(timeout=timeout) as s:
        async with s.get(url, headers={"User-Agent": "Mozilla/5.0"}) as r:
            r.raise_for_status()
            with open(dest, "wb") as f:
                async for chunk in r.content.iter_chunked(1 << 20):
                    f.write(chunk)
    return dest, fname


async def _fetch_link(uid, raw, gname, chat_id):
    """YT/direct link -> download -> queue. Progress message ke sath."""
    key = raw.strip()
    lock = DL_LOCKS.setdefault(key, asyncio.Lock())
    if lock.locked():
        return await m_reply(uid, "⏳ Ye link already download ho raha hai...")
    async with lock:
        is_yt = _is_youtube(raw)
        cached = YT_CACHE.get(key)
        if cached and os.path.exists(cached[0]):
            path, title = cached
            _enqueue(chat_id, {
                "path": path, "title": title, "kind": "yt", "uid": uid,
                "group": gname, "cached": True,
            })
            await m_reply(uid, f"⚡ **Cache se instantly:**\n📋 **{title[:40]}**")
            return
        label = "▶️ YouTube" if is_yt else "🔗 Direct"
        wait = await m_reply(uid, f"{label} fetch ho raha hai — **super fast** ⚡")
        try:
            t0 = time.time()
            if is_yt:
                path, title = await _download_yt(raw, key)
            else:
                path, title = await _download_direct(raw, key)
            dt = time.time() - t0
            if os.path.getsize(path) > MAX_TG_SIZE:
                os.remove(path)
                return await wait.edit_text("❌ File 2GB se badi hai")
            pos = _enqueue(chat_id, {
                "path": path, "title": title, "kind": "yt" if is_yt else "audio",
                "uid": uid, "group": gname,
            })
            await wait.edit_text(
                f"⚡ **Downloaded in {dt:.0f}s**\n"
                f"📥 **Aᴅᴅᴇᴅ Tᴏ Qᴜᴇᴜᴇ Aᴛ #{pos}**\n\n"
                f"🎵 **{title[:40]}**",
            )
        except Exception as e:
            await wait.edit_text(f"❌ Link fetch failed:\n`{str(e)[:200]}`")


async def m_reply(uid, text):
    """User ko PM me message bhejo (Message object ki jagah)."""
    try:
        return await bot.send_message(uid, text)
    except Exception:
        class _Fake:
            async def edit_text(self, t, **kw):
                pass
        return _Fake()


# ---------------- mic on/off (owner/sudo) ----------------

@bot.on_message(filters.command(["micon", "micoff"]) & filters.private)
async def mic_toggle(_, m: Message):
    uid = m.from_user.id
    if not is_admin(uid):
        return await m.reply_text("👑 Ye command sirf owner/sudo ke liye hai")
    info = user_data.get(uid) or {}
    chat_id = info.get("chat_id") or active_calls.get(uid) or LAST_CHAT.get(uid)
    if not chat_id:
        return await m.reply_text(
            "❌ Pehle group select karo (username/invite link bhejo)\n"
            "ya koi stream chalu karo"
        )
    gname = info.get("group") or CHAT_NAMES.get(chat_id, str(chat_id))
    CHAT_NAMES[chat_id] = gname
    if m.command[0] == "micon":
        ok, fail = [], []
        for idx in range(len(ASSISTANTS)):
            if (chat_id, idx) in PRESENCE_JOINED:
                await _assistant_leave(chat_id, idx)
                await asyncio.sleep(1.0)
            if await _assistant_join(chat_id, idx):
                ok.append(ASSISTANTS[idx][0])
            else:
                fail.append(ASSISTANTS[idx][0])
        PRESENCE_ONLY.add(chat_id)
        txt = (
            "🎙 **Presence ON!** (Mic OFF badge)\n\n"
            f"📻 Group: {gname}\n"
            f"👻 Joined ({len(ok)}/{len(ASSISTANTS)}):\n"
            + "\n".join(f"  • {n}" for n in ok)
            + "\n\n🔇 Sab VC me honge par MIC OFF dikhenge\n"
            f"♻️ Auto-refresh har {MIC_REFRESH // 60} min me"
        )
        if fail:
            txt += "\n\n⚠️ Failed:\n" + "\n".join(f"  • {n}" for n in fail)
        await m.reply_text(txt)
        await send_log(
            "🎙 **Mic ON**\n\n"
            f"👤 By: {_mention(m.from_user)}\n"
            f"📻 Group: {gname}\n"
            f"👻 Assistants: {len(ok)}/{len(ASSISTANTS)}"
        )
    else:
        for idx in range(len(ASSISTANTS)):
            if (chat_id, idx) in PRESENCE_JOINED:
                await _assistant_leave(chat_id, idx)
        PRESENCE_ONLY.discard(chat_id)
        await m.reply_text(
            "🎙 **Mic OFF**\n\n"
            f"📻 Group: {gname}\n"
            "👻 Assistants VC se leave ho gaye (badge bhi clear)"
        )
        await send_log(
            "🎙 **Mic OFF**\n\n"
            f"👤 By: {_mention(m.from_user)}\n"
            f"📻 Group: {gname}"
        )


# ---------------- /ac active-calls panel ----------------

def _ac_kb():
    rows = []
    for cid, idx in sorted(PRESENCE_JOINED):
        name = CHAT_NAMES.get(cid) or str(cid)
        rows.append(
            [InlineKeyboardButton(
                f"🎙 {name[:22]} — #{idx}",
                callback_data=f"acoff:{cid}:{idx}"
            )]
        )
    rows.append([InlineKeyboardButton("🔴 Refresh", callback_data="acview")])
    return InlineKeyboardMarkup(rows)


@bot.on_message(filters.command("ac") & filters.private)
async def ac_cmd(_, m: Message):
    if not is_admin(m.from_user.id):
        return await m.reply_text("👑 Owner only")
    if not PRESENCE_JOINED:
        return await m.reply_text(
            "📴 **Koi active presence nahi**\n\n"
            "/micon se kisi group me mic ON karo"
        )
    lines = [f"🎙 **Active Mic/Presence ({len(PRESENCE_JOINED)}):**\n"]
    for cid, idx in sorted(PRESENCE_JOINED):
        name = CHAT_NAMES.get(cid) or str(cid)
        playing = "🎵 music" if cid in WAIT_END else "mic only"
        lines.append(f"  • {name} → asst #{idx} ({playing})")
    await m.reply_text(
        "\n".join(lines) + "\n\n⬇️ Button dabao = OFF",
        reply_markup=_ac_kb()
    )


@bot.on_callback_query(filters.regex(r"^acoff:(-?\d+):(\d+)$"))
async def ac_off_cb(_, cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return await cb.answer("Owner only", show_alert=True)
    cid, idx = int(cb.matches[0].group(1)), int(cb.matches[0].group(2))
    if (cid, idx) not in PRESENCE_JOINED:
        return await cb.answer("Already off", show_alert=True)
    await _assistant_leave(cid, idx)
    name = CHAT_NAMES.get(cid) or str(cid)
    await cb.answer(f"🎙 OFF: {name}", show_alert=True)
    await send_log(
        "🎙 **Mic OFF (panel)**\n\n"
        f"👤 By: {_mention(cb.from_user)}\n"
        f"📻 Group: {name}"
    )
    if PRESENCE_JOINED:
        try:
            await cb.message.edit_reply_markup(reply_markup=_ac_kb())
        except Exception:
            pass
    else:
        await cb.message.edit_text("📴 Sab presence OFF ho gayi")


@bot.on_callback_query(filters.regex(r"^acview$"))
async def ac_view_cb(_, cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return await cb.answer("Owner only", show_alert=True)
    if not PRESENCE_JOINED:
        return await cb.message.edit_text("📴 Sab presence OFF ho gayi")
    try:
        await cb.message.edit_reply_markup(reply_markup=_ac_kb())
        await cb.answer("Refreshed")
    except Exception:
        await cb.answer("No change")


# ---------------- owner: approve system ----------------

@bot.on_message(filters.command("approve") & filters.private)
async def approve_cmd(_, m: Message):
    if not is_admin(m.from_user.id):
        return
    target = None
    label = ""
    if m.reply_to_message and m.reply_to_message.from_user:
        target = m.reply_to_message.from_user.id
        label = m.reply_to_message.from_user.first_name or ""
    elif len(m.command) > 1:
        arg = m.command[1].strip()
        if arg.isdigit():
            target = int(arg)
        else:
            try:
                u = await bot.get_users(arg if arg.startswith("@") else "@" + arg)
                target = u.id
                label = u.first_name or ""
            except Exception as e:
                return await m.reply_text(f"❌ User not found:\n`{e}`")
    if target is None:
        return await m.reply_text(
            "Usage:\n/approve `<user_id>`\n/approve `@username`\nreply /approve"
        )
    APPROVED.add(target)
    _save_approved()
    pending_requests.discard(target)
    await m.reply_text(f"✅ Approved [{target}] {label}\nAb wo bot use kar sakta hai.")
    try:
        await bot.send_message(
            target, "✅ **You have been approved!**\nAb /start karke bot use karo 🎵"
        )
    except Exception:
        pass


@bot.on_message(filters.command("unapprove") & filters.private)
async def unapprove_cmd(_, m: Message):
    if not is_admin(m.from_user.id):
        return
    if len(m.command) < 2 or not m.command[1].isdigit():
        return await m.reply_text("Usage: /unapprove `<user_id>`")
    target = int(m.command[1])
    APPROVED.discard(target)
    _save_approved()
    await m.reply_text(f"🚫 Unapproved [{target}]")


@bot.on_message(filters.command("approved") & filters.private)
async def approved_list(_, m: Message):
    if not is_admin(m.from_user.id):
        return
    lines = [f"👑 Owner: `{OWNER_ID}`"]
    if SUDO_IDS:
        lines.append("🛡 Sudo: " + ", ".join(f"`{x}`" for x in sorted(SUDO_IDS)))
    lines.append("✅ Approved: " + (
        ", ".join(f"`{x}`" for x in sorted(APPROVED)) if APPROVED else "_none_"
    ))
    await m.reply_text("\n".join(lines))


@bot.on_callback_query(filters.regex(r"^ap:(\d+)$"))
async def approve_cb(_, cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return await cb.answer("Owner only", show_alert=True)
    target = int(cb.matches[0].group(1))
    APPROVED.add(target)
    _save_approved()
    pending_requests.discard(target)
    await cb.message.edit_text(f"✅ Approved [{target}]")
    await cb.answer("Approved!")
    try:
        await bot.send_message(
            target, "✅ **You have been approved!**\nAb /start karke bot use karo 🎵"
        )
    except Exception:
        pass


@bot.on_callback_query(filters.regex(r"^rj:(\d+)$"))
async def reject_cb(_, cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return await cb.answer("Owner only", show_alert=True)
    target = int(cb.matches[0].group(1))
    pending_requests.discard(target)
    await cb.message.edit_text(f"🚫 Rejected [{target}]")
    await cb.answer("Rejected")


# ---------------- start / access ----------------

@bot.on_message(filters.command(["start", "help"]) & filters.private)
async def start(_, m: Message):
    uid = m.from_user.id
    if not is_allowed(uid):
        txt = DENIED_TEXT.format(my_id=uid)
        if uid not in pending_requests and not is_admin(uid):
            pending_requests.add(uid)
            kb = InlineKeyboardMarkup(
                [[
                    InlineKeyboardButton("✅ Approve", callback_data=f"ap:{uid}"),
                    InlineKeyboardButton("❌ Reject", callback_data=f"rj:{uid}")
                ]]
            )
            try:
                await bot.send_message(
                    OWNER_ID,
                    f"📩 **New access request**\n"
                    f"User: [{m.from_user.first_name}](tg://user?id={uid})\n"
                    f"ID: `{uid}`\nUsername: @{m.from_user.username or '—'}",
                    reply_markup=kb
                )
            except Exception:
                pass
        return await m.reply_text(txt, reply_markup=_deny_kb())

    user_data[uid] = {"step": "group"}
    name = m.from_user.first_name or "Friend"
    await m.reply_text(_start_text(name), reply_markup=_start_kb())

    if uid not in SEEN_USERS and not is_admin(uid):
        SEEN_USERS.add(uid)
        await send_log(
            "🆕 **New User Started Bot**\n\n"
            f"👤 User: {_mention(m.from_user)}\n"
            f"📱 Username: {_uname(m.from_user)}\n"
            f"🆔 ID: `{uid}`"
        )


def _deny_kb():
    return InlineKeyboardMarkup(
        [
            _support_row(),
            [InlineKeyboardButton("👑 Owner", url=OWNER_LINK)],
        ]
    )


@bot.on_message(filters.command(["pause", "resume"]) & filters.private)
async def pause_resume(_, m: Message):
    if not is_allowed(m.from_user.id):
        return await _deny(m)
    uid = m.from_user.id
    chat_id = active_calls.get(uid)
    if not chat_id:
        return await m.reply_text("❌ Nothing is playing")
    try:
        if m.command[0] == "pause":
            await calls.pause(chat_id)
            paused_calls.add(chat_id)
            await m.reply_text("⏸ Paused")
        else:
            await calls.resume(chat_id)
            paused_calls.discard(chat_id)
            await m.reply_text("▶️ Resumed")
    except Exception as e:
        await m.reply_text(f"❌ {e}")


@bot.on_message(filters.command("playing") & filters.private)
async def playing(_, m: Message):
    if not is_allowed(m.from_user.id):
        return await _deny(m)
    uid = m.from_user.id
    chat_id = active_calls.get(uid)
    if not chat_id:
        return await m.reply_text("❌ Nothing is playing")
    meta = QUEUE_META.get(chat_id, {})
    cur = meta.get("current")
    q = QUEUES.get(chat_id) or deque()
    state = "⏸ Paused" if chat_id in paused_calls else "▶️ Playing"
    if cur:
        await m.reply_text(
            f"{state}: **{cur['title'][:34]}**\n"
            f"📋 Queue: {len(q)} baaki • 🔊 Vol: {_chat_vol(chat_id)}\n"
            f"👥 Mode: {MODE_LABEL.get(meta.get('mode', 0))}"
        )
    else:
        await m.reply_text("❌ Nothing is playing")


@bot.on_message(filters.command("status") & filters.private)
async def status_cmd(_, m: Message):
    if not is_allowed(m.from_user.id):
        return await _deny(m)
    up = int(time.time() - START_TS)
    h, rem = divmod(up, 3600)
    mn, s = divmod(rem, 60)
    queues = []
    for cid, q in QUEUES.items():
        if q or (QUEUE_META.get(cid, {}).get("current")):
            queues.append(f"📻 `{CHAT_NAMES.get(cid, cid)}` — {len(q)} queued")
    text = (
        f"📊 **Bot Status**\n\n"
        f"⏱ Uptime: `{h}h {mn}m {s}s`\n"
        f"🎚 Active streams: `{len(WAIT_END)}`\n"
        f"👻 Assistants: `{len(ASSISTANTS)}`\n"
        f"👥 Approved users: `{len(APPROVED)}`\n"
    )
    if queues:
        text += "\n**Active queues:**\n" + "\n".join(queues)
    await m.reply_text(text)


# ---------------- queue commands: /q, /sw, /sk, /vol ----------------

def _queue_view(chat_id):
    """Queue text + swap/skip buttons. Returns (text, kb) ya None."""
    q = QUEUES.get(chat_id)
    meta = QUEUE_META.get(chat_id, {})
    cur = meta.get("current")
    if not cur and not q:
        return None
    lines = ["📋 **Queue**\n"]
    if cur:
        lines.append(f"▶️ **#{meta.get('pos', 1)}** {cur['title'][:36]}")
    if q:
        for i, e in enumerate(q):
            lines.append(f"  ⏳ #{i + 1 + meta.get('pos', 1)} {e['title'][:36]}")
    else:
        lines.append("  _(queue khali — current khatam hone pe band ho jayega)_")
    kb = []
    n = len(q) if q else 0
    for i in range(n - 1):
        kb.append([InlineKeyboardButton(
            f"🔄 #{i + 1} ⇄ #{i + 2}",
            callback_data=f"swap:{i}:{i + 1}"
        )])
    row = []
    if cur:
        row.append(InlineKeyboardButton("⏭ Skip Current", callback_data="skip"))
    if n:
        row.append(InlineKeyboardButton("♻ Refresh", callback_data="qview"))
    if row:
        kb.append(row)
    kb.append(_support_row())
    return "\n".join(lines), InlineKeyboardMarkup(kb)


@bot.on_message(filters.command("q") & filters.private)
async def queue_cmd(_, m: Message):
    if not is_allowed(m.from_user.id):
        return await _deny(m)
    uid = m.from_user.id
    chat_id = active_calls.get(uid) or LAST_CHAT.get(uid)
    view = _queue_view(chat_id) if chat_id else None
    if not view:
        return await m.reply_text("📋 Queue khali hai")
    await m.reply_text(view[0], reply_markup=view[1])


@bot.on_message(filters.command("sw") & filters.private)
async def swap_cmd(_, m: Message):
    if not is_allowed(m.from_user.id):
        return await _deny(m)
    uid = m.from_user.id
    chat_id = active_calls.get(uid) or LAST_CHAT.get(uid)
    q = QUEUES.get(chat_id)
    if not q or len(m.command) < 3:
        return await m.reply_text("Usage: /sw `<pos1> <pos2>`  (e.g. /sw 1 2)\nQueue: /q")
    try:
        i, j = int(m.command[1]) - 1, int(m.command[2]) - 1
        assert 0 <= i < len(q) and 0 <= j < len(q)
    except Exception:
        return await m.reply_text(f"❌ Position 1–{len(q)} me do numbers do")
    q[i], q[j] = q[j], q[i]
    view = _queue_view(chat_id)
    await m.reply_text(f"✅ Swapped #{i + 1} ⇄ #{j + 1}\n\n{view[0]}", reply_markup=view[1])


@bot.on_message(filters.command("sk") & filters.private)
async def skip_cmd(_, m: Message):
    if not is_allowed(m.from_user.id):
        return await _deny(m)
    uid = m.from_user.id
    chat_id = active_calls.get(uid) or LAST_CHAT.get(uid)
    ev = WAIT_END.get(chat_id)
    if not ev:
        return await m.reply_text("❌ Nothing is playing")
    ev.set()
    await m.reply_text("⏭ Skipped")


@bot.on_message(filters.command("vol") & filters.private)
async def vol_cmd(_, m: Message):
    if not is_allowed(m.from_user.id):
        return await _deny(m)
    uid = m.from_user.id
    chat_id = active_calls.get(uid) or LAST_CHAT.get(uid)
    if not chat_id:
        return await m.reply_text("❌ Pehle group select karo")
    if len(m.command) > 1:
        arg = " ".join(m.command[1:]).strip().title()
        if arg not in VOLUME_LEVELS:
            return await m.reply_text(
                "❌ Levels: " + " • ".join(VOLUME_LEVELS)
            )
        QUEUE_META.setdefault(chat_id, {})["vol"] = arg
        await _apply_volume(chat_id)
        return await m.reply_text(f"🔊 Volume: **{arg}** ({VOLUME_LEVELS[arg]}%)")
    await m.reply_text(
        f"🔊 **Volume** — abhi: **{_chat_vol(chat_id)}**\nLevel chuno:",
        reply_markup=_vol_kb(chat_id),
    )


@bot.on_callback_query(filters.regex("^volmenu$"))
async def vol_menu_cb(_, cb: CallbackQuery):
    if not is_allowed(cb.from_user.id):
        return await cb.answer("Not approved", show_alert=True)
    chat_id = cb.message.chat.id
    try:
        await cb.message.reply_text(
            f"🔊 **Volume** — abhi: **{_chat_vol(chat_id)}**\nLevel chuno:",
            reply_markup=_vol_kb(chat_id),
        )
    except Exception:
        pass
    await cb.answer()


@bot.on_callback_query(filters.regex(r"^volset:([\w ]+)$"))
async def vol_set_cb(_, cb: CallbackQuery):
    if not is_allowed(cb.from_user.id):
        return await cb.answer("Not approved", show_alert=True)
    chat_id = cb.message.chat.id
    name = cb.matches[0].group(1)
    if name not in VOLUME_LEVELS:
        return await cb.answer("Unknown level", show_alert=True)
    QUEUE_META.setdefault(chat_id, {})["vol"] = name
    await _apply_volume(chat_id)
    await cb.answer(f"🔊 {name} ({VOLUME_LEVELS[name]}%)", show_alert=True)
    try:
        await cb.message.edit_reply_markup(reply_markup=_vol_kb(chat_id))
    except Exception:
        pass


# ---------------- stream end hook ----------------

@calls.on_update()
async def stream_ended_handler(_, update: StreamEnded):
    chat_id = update.chat_id
    if chat_id in WAIT_END and update.stream_type == StreamEnded.Type.AUDIO:
        WAIT_END[chat_id].set()


# ---------------- stop ----------------

def _clear_queue(chat_id):
    meta = QUEUE_META.get(chat_id)
    if meta:
        meta["stop"] = True
    ev = WAIT_END.get(chat_id)
    if ev:
        ev.set()
    q = QUEUES.pop(chat_id, None)
    if q:
        for e in q:
            _drop_entry(e)


@bot.on_message(filters.command("stop"))
async def stop_cmd(_, m: Message):
    if not m.from_user or not is_allowed(m.from_user.id):
        return
    if m.chat.type == enums.ChatType.PRIVATE:
        uid = m.from_user.id
        chat_id = active_calls.get(uid) or LAST_CHAT.get(uid)
        if not chat_id:
            return await m.reply_text("❌ Nothing is playing")
        group_name = CHAT_NAMES.get(chat_id, "chat")
    else:
        chat_id = m.chat.id
        group_name = m.chat.title or str(chat_id)
    _clear_queue(chat_id)
    active_calls.pop(m.from_user.id, None)
    chat_calls.pop(chat_id, None)
    await m.reply_text(f"⏹ Stopped in **{group_name}**")
    await send_log(
        "⏹ **Stream Stopped**\n\n"
        f"👤 User: {_mention(m.from_user)}\n"
        f"📻 Group: {group_name}"
    )


@bot.on_callback_query(filters.regex("^stop$"))
async def stop_cb(_, cb: CallbackQuery):
    uid = cb.from_user.id
    if not is_allowed(uid):
        return await cb.answer("Not approved", show_alert=True)
    chat_id = cb.message.chat.id
    if chat_id not in WAIT_END and not QUEUES.get(chat_id):
        return await cb.answer("Nothing playing", show_alert=True)
    _clear_queue(chat_id)
    active_calls.pop(uid, None)
    await cb.message.edit_text("⏹ Voice Chat Stopped")
    await cb.answer("Stopped")


@bot.on_callback_query(filters.regex("^skip$"))
async def skip_cb(_, cb: CallbackQuery):
    uid = cb.from_user.id
    if not is_allowed(uid):
        return await cb.answer("Not approved", show_alert=True)
    chat_id = cb.message.chat.id
    ev = WAIT_END.get(chat_id)
    if not ev:
        return await cb.answer("Nothing playing", show_alert=True)
    ev.set()
    await cb.answer("⏭ Skipped")


@bot.on_callback_query(filters.regex("^qview$"))
async def qview_cb(_, cb: CallbackQuery):
    if not is_allowed(cb.from_user.id):
        return await cb.answer("Not approved", show_alert=True)
    view = _queue_view(cb.message.chat.id)
    if not view:
        return await cb.answer("Queue khali", show_alert=True)
    try:
        await cb.message.edit_text(view[0], reply_markup=view[1])
    except Exception:
        pass
    await cb.answer()


@bot.on_callback_query(filters.regex(r"^swap:(\d+):(\d+)$"))
async def swap_cb(_, cb: CallbackQuery):
    if not is_allowed(cb.from_user.id):
        return await cb.answer("Not approved", show_alert=True)
    chat_id = cb.message.chat.id
    q = QUEUES.get(chat_id)
    i, j = int(cb.matches[0].group(1)), int(cb.matches[0].group(2))
    if not q or i >= len(q) or j >= len(q):
        return await cb.answer("Positions abhi valid nahi", show_alert=True)
    q[i], q[j] = q[j], q[i]
    await cb.answer(f"✅ #{i + 1} ⇄ #{j + 1}")
    view = _queue_view(chat_id)
    if view:
        try:
            await cb.message.edit_text(view[0], reply_markup=view[1])
        except Exception:
            pass


@bot.on_callback_query(filters.regex("^replay$"))
async def replay(_, cb: CallbackQuery):
    uid = cb.from_user.id
    if not is_allowed(uid):
        return await cb.answer("Not approved", show_alert=True)
    chat_id = cb.message.chat.id
    cur = QUEUE_META.get(chat_id, {}).get("current")
    if not cur or not os.path.exists(cur["path"]):
        return await cb.answer("File missing — dobara bhejo", show_alert=True)
    try:
        await calls.play(chat_id, MediaStream(cur["path"]))
        paused_calls.discard(chat_id)
        await cb.answer("⟲ Replaying")
    except Exception as e:
        await cb.answer(f"Error: {e}", show_alert=True)


@bot.on_callback_query(filters.regex("^(pause|resume)$"))
async def pause_resume_cb(_, cb: CallbackQuery):
    uid = cb.from_user.id
    if not is_allowed(uid):
        return await cb.answer("Not approved", show_alert=True)
    chat_id = cb.message.chat.id
    if chat_id not in WAIT_END:
        return await cb.answer("Nothing playing", show_alert=True)
    try:
        if cb.data == "pause":
            await calls.pause(chat_id)
            paused_calls.add(chat_id)
            await cb.answer("⏸ Paused")
        else:
            await calls.resume(chat_id)
            paused_calls.discard(chat_id)
            await cb.answer("▶️ Resumed")
        try:
            await cb.message.edit_reply_markup(
                reply_markup=_buttons(chat_id in paused_calls)
            )
        except Exception:
            pass
    except Exception as e:
        await cb.answer(f"Error: {e}", show_alert=True)


# ---------------- /tts — live text-to-speech in VC ----------------

_TTS_VOICES = {"hi": "hi-IN-MadhurNeural", "en": "en-IN-NeerjaNeural"}


def _tts_args(raw):
    """`en:text` / `hi:text` prefix se voice, warna script (Devanagari) se auto."""
    pref = raw[:3]
    if pref in ("en:", "hi:"):
        voice, raw = _TTS_VOICES[pref[:2]], raw[3:].strip()
    else:
        voice = (
            _TTS_VOICES["hi"]
            if any("\u0900" <= ch <= "\u097F" for ch in raw)
            else _TTS_VOICES["en"]
        )
    return voice, raw


async def _tts_file(text, voice):
    os.makedirs(TTS_DIR, exist_ok=True)
    out = os.path.join(TTS_DIR, f"{int(time.time() * 1000)}.mp3")
    await asyncio.wait_for(edge_tts.Communicate(text, voice).save(out), timeout=60)
    return out


@bot.on_message(filters.command("tts") & filters.private)
async def tts_cmd(_, m: Message):
    if not is_allowed(m.from_user.id):
        return await _deny(m)
    raw = m.text.split(maxsplit=1)[1].strip() if len(m.command) > 1 else ""
    if not raw:
        return await m.reply_text(
            "🔊 Usage: /tts `<text>`\n"
            "Voice select: /tts `hi:namaste` ya /tts `en:hello`"
        )
    voice, text = _tts_args(raw)
    uid = m.from_user.id
    info = user_data.get(uid) or {}
    chat_id = info.get("chat_id") or active_calls.get(uid) or LAST_CHAT.get(uid)
    if not chat_id:
        return await m.reply_text(
            "❌ Pehle group select karo (username/link ya 🔗 Attach Group)"
        )
    msg = await m.reply_text("🎙 Generating voice...")
    try:
        path = await _tts_file(text[:400], voice)
        gname = info.get("group") or CHAT_NAMES.get(chat_id) or str(chat_id)
        WAIT_END.setdefault(chat_id, asyncio.Event())
        await calls.play(chat_id, MediaStream(path))
        CHAT_NAMES[chat_id] = gname
        asyncio.ensure_future(_apply_volume(chat_id))
        await msg.reply_voice(path)
        await msg.edit_text(
            "╭────── 🔊 **TTS Playing** ──────╮\n"
            f"  🗣 `{text[:32]}`\n"
            f"  🎙 Voice: `{voice}`\n"
            f"  📻 Group: **{gname}**\n"
            "╰────────────────────────────╯",
            reply_markup=_buttons(),
        )
        # TTS file ko 15 sec baad clean kar do (stream start ho chuki hogi)
        async def _cleanup_tts():
            await asyncio.sleep(15)
            try:
                os.remove(path)
            except OSError:
                pass
        asyncio.ensure_future(_cleanup_tts())
        await send_log(
            "🔊 **TTS Request**\n\n"
            f"👤 User: {_mention(m.from_user)}\n"
            f"📱 Username: {_uname(m.from_user)}\n"
            f"🗣 Text: `{text[:60]}`\n"
            f"📻 Group: {gname}"
        )
    except Exception as e:
        await msg.edit_text(f"❌ TTS failed:\n`{str(e)[:120]}`")


# ---------------- attach group (buttons) ----------------

@bot.on_callback_query(filters.regex("^mygroups$"))
async def mygroups_cb(_, cb: CallbackQuery):
    uid = cb.from_user.id
    if not is_allowed(uid):
        return await cb.answer("Not approved", show_alert=True)
    await cb.answer("Loading groups...")
    buttons = []
    seen = set()
    try:
        async for d in userbot.get_dialogs(limit=60):
            ch = d.chat
            if (
                ch and ch.type in (enums.ChatType.GROUP, enums.ChatType.SUPERGROUP)
                and ch.id not in seen and len(buttons) < 15
            ):
                seen.add(ch.id)
                buttons.append(
                    [InlineKeyboardButton(
                        f"🔗 {(ch.title or 'Group')[:24]}",
                        callback_data=f"selg:{ch.id}"
                    )]
                )
    except Exception:
        pass
    if not buttons:
        return await cb.message.edit_text(
            "❌ Assistant kisi group me nahi hai.\n"
            "Group ka link bhejo, ya assistant account ko group me add karo.",
            reply_markup=_back_kb()
        )
    buttons.append([InlineKeyboardButton("⬅ Back", callback_data="backstart")])
    await cb.message.edit_text(
        "🔗 **Attach Group**\n\n"
        "Apna group tap karo — link bhejne ki zaroorat nahi 🚀",
        reply_markup=InlineKeyboardMarkup(buttons)
    )


@bot.on_callback_query(filters.regex(r"^selg:(-?\d+)$"))
async def selg_cb(_, cb: CallbackQuery):
    uid = cb.from_user.id
    if not is_allowed(uid):
        return await cb.answer("Not approved", show_alert=True)
    chat_id = int(cb.matches[0].group(1))
    try:
        chat = await userbot.get_chat(chat_id)
        gname = chat.title or str(chat_id)
    except Exception:
        gname = CHAT_NAMES.get(chat_id, str(chat_id))
    user_data[uid] = {
        "step": "audio",
        "group": gname,
        "chat_id": chat_id,
        "invite": True
    }
    LAST_CHAT[uid] = chat_id
    CHAT_NAMES[chat_id] = gname
    await cb.message.edit_text(
        f"✅ **Group attached:** {gname}\n\n"
        "🎵 Ab audio/video file ya link bhejo — turant play hoga"
    )
    await cb.answer("Attached! 🎵")


@bot.on_callback_query(filters.regex("^cmds$"))
async def cmds_cb(_, cb: CallbackQuery):
    await cb.message.edit_text(CMDS_TEXT, reply_markup=_back_kb())
    await cb.answer()


@bot.on_callback_query(filters.regex("^backstart$"))
async def backstart_cb(_, cb: CallbackQuery):
    name = cb.from_user.first_name or "Friend"
    if is_allowed(cb.from_user.id):
        await cb.message.edit_text(_start_text(name), reply_markup=_start_kb())
    await cb.answer()


def _check_button():
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("📋 Check Join Status", callback_data="checkjoin")]]
    )


# ---------------- group selection flow (text) ----------------

@bot.on_message(filters.private & filters.text)
async def text_handler(_, m: Message):
    uid = m.from_user.id
    if not is_allowed(uid):
        return await _deny(m)
    if uid not in user_data:
        return

    raw = m.text.strip()

    # ---------- assistant mode selection (1/2/3 typed) ----------
    if user_data[uid].get("step") == "mode" and raw in ("1", "2", "3"):
        mode = int(raw) - 1
        _finish_group_select(uid, mode, m)
        return

    step = user_data[uid].get("step")

    # ---------- private group invite link ----------
    if "t.me/+" in raw or "t.me/joinchat/" in raw:
        hash_part = (
            raw.split("t.me/")[-1]
            .replace("joinchat/", "")
            .lstrip("+")
            .strip()
        )
        wait = await m.reply_text("📨 Checking invite link...")
        baseline = await _snapshot_group_ids()
        title_guess = "group"
        try:
            invite = await userbot.invoke(
                raw_functions.messages.CheckChatInvite(hash=hash_part)
            )

            # CASE 1: player is already a member
            if isinstance(invite, (raw_types.ChatInviteAlready, raw_types.ChatInvitePeek)):
                if not getattr(invite, "chat", None):
                    raise Exception("Group info nahi mila")
                marked = _marked_id(invite.chat)
                user_data[uid] = {
                    "step": "mode",
                    "group": invite.chat.title or str(marked),
                    "chat_id": marked,
                    "invite": True
                }
                INVITE_HASH[marked] = hash_part
                LAST_CHAT[uid] = marked
                CHAT_NAMES[marked] = invite.chat.title or str(marked)
                return await wait.edit_text(
                    f"✅ **Group verify ho gaya:** {invite.chat.title}\n\n"
                    "👥 **Kaunsa assistant mode?**",
                    reply_markup=_mode_kb()
                )

            # CASE 2/3: joinable preview -> import the invite
            if isinstance(invite, raw_types.ChatInvite):
                title_guess = invite.title or title_guess
                try:
                    updates = await userbot.invoke(
                        raw_functions.messages.ImportChatInvite(hash=hash_part)
                    )
                except Exception as je:
                    if "REQUEST" in str(je).upper():
                        user_data[uid] = {
                            "step": "pending",
                            "group": title_guess,
                            "invite": True,
                            "hash": hash_part,
                            "baseline": baseline
                        }
                        return await wait.edit_text(
                            "📨 **Join request sent!** ⏳\n\n"
                            "Group admins approve karte hi bot play karega.\n"
                            "Approve hone ke baad 📋 button dabao ya file bhej do.",
                            reply_markup=_check_button()
                        )
                    raise

                await asyncio.sleep(1.5)
                marked = None
                for c in (getattr(updates, "chats", None) or []):
                    marked = _marked_id(c)
                    break
                if marked is None:
                    chat = await _find_new_chat(uid)
                    marked = chat.id if chat else None
                if marked:
                    try:
                        full = await userbot.get_chat(marked)
                        gname = full.title or title_guess
                    except Exception:
                        gname = title_guess
                    user_data[uid] = {
                        "step": "mode",
                        "group": gname,
                        "chat_id": marked,
                        "invite": True
                    }
                    INVITE_HASH[marked] = hash_part
                    LAST_CHAT[uid] = marked
                    CHAT_NAMES[marked] = gname
                    return await wait.edit_text(
                        f"✅ **Joined:** {gname}\n\n"
                        "👥 **Kaunsa assistant mode?**",
                        reply_markup=_mode_kb()
                    )
                user_data[uid] = {
                    "step": "pending",
                    "group": title_guess,
                    "invite": True,
                    "hash": hash_part,
                    "baseline": baseline
                }
                if marked is not None:
                    INVITE_HASH[marked] = hash_part
                return await wait.edit_text(
                    "✅ Join ho gaya, group load ho raha hai...\n"
                    "📋 button dabao ya file bhej do.",
                    reply_markup=_check_button()
                )

            raise Exception(
                f"Unexpected invite response: {type(invite).__name__}"
            )

        except (InviteHashExpired, InviteHashInvalid):
            return await wait.edit_text("❌ Invite link invalid ya expired hai")
        except Exception as e:
            return await wait.edit_text(f"❌ Join failed:\n`{e}`")

    # ---------- YouTube / direct link ----------
    if HTTP_RE.match(raw):
        info = user_data[uid]
        chat_id = info.get("chat_id")
        if not chat_id:
            return await m.reply_text(
                "❌ Pehle group select karo (@username / invite link / 🔗 Attach Group)"
            )
        gname = info.get("group") or CHAT_NAMES.get(chat_id, str(chat_id))
        asyncio.ensure_future(_fetch_link(uid, raw, gname, chat_id))
        return

    # ---------- public group @username ----------
    if step == "group":
        chat_id, gname = await _resolve_group(m, raw)
        if chat_id is None:
            return await m.reply_text(
                "❌ Group nahi mila. Public @username bhejo,\n"
                "ya private invite link, ya assistant ko group me add karo."
            )
        user_data[uid] = {
            "step": "mode",
            "group": gname,
            "chat_id": chat_id,
        }
        LAST_CHAT[uid] = chat_id
        CHAT_NAMES[chat_id] = gname
        return await m.reply_text(
            f"✅ **Group verify ho gaya:** {gname}\n\n"
            "👥 **Kaunsa assistant mode?**\n"
            "  1️⃣ Single — sirf main assistant VC me jayega\n"
            "  2️⃣ One By One — ek band to dusra jayega\n"
            "  3️⃣ All In One — teeno ek sath VC me",
            reply_markup=_mode_kb()
        )
    if step == "mode":
        return await m.reply_text("👥 Upar mode buttons me se chuno (1/2/3)")
    await m.reply_text("🎵 Ab audio/video file ya link bhejo")


def _finish_group_select(uid, mode, m=None):
    info = user_data.get(uid) or {}
    chat_id = info.get("chat_id")
    gname = info.get("group", "?")
    if not chat_id:
        return
    meta = QUEUE_META.setdefault(chat_id, {})
    meta["mode"] = mode
    info["step"] = "audio"
    label = MODE_LABEL.get(mode)
    txt = (
        f"✅ **Mode set:** {label}\n"
        f"📻 **Group:** {gname}\n\n"
        "🎵 Ab audio/video file ya YT/direct link bhejo!\n"
        "🔊 Volume bhi chun sakte ho: /vol"
    )
    if m:
        asyncio.ensure_future(m.reply_text(txt))
    else:
        asyncio.ensure_future(bot.send_message(uid, txt))
    asyncio.ensure_future(send_log(
        "👥 **Assistant Mode Set**\n\n"
        f"👤 User: ID `{uid}`\n"
        f"📻 Group: {gname}\n"
        f"👥 Mode: {label}"
    ))


@bot.on_callback_query(filters.regex(r"^mode:(\d+)$"))
async def mode_cb(_, cb: CallbackQuery):
    uid = cb.from_user.id
    if not is_allowed(uid):
        return await cb.answer("Not approved", show_alert=True)
    mode = int(cb.matches[0].group(1))
    info = user_data.get(uid) or {}
    chat_id = info.get("chat_id")
    if not chat_id:
        return await cb.answer("Pehle group bhejo", show_alert=True)
    meta = QUEUE_META.setdefault(chat_id, {})
    meta["mode"] = mode
    info["step"] = "audio"
    gname = info.get("group", str(chat_id))
    await cb.message.edit_text(
        f"✅ **Mode set:** {MODE_LABEL[mode]}\n"
        f"📻 **Group:** {gname}\n\n"
        "🎵 Ab audio/video file ya YT/direct link bhejo!\n"
        "🔊 Volume: /vol"
    )
    await cb.answer(f"{MODE_LABEL[mode]} ✓")
    await send_log(
        "👥 **Assistant Mode Set**\n\n"
        f"👤 User: {_mention(cb.from_user)}\n"
        f"📻 Group: {gname}\n"
        f"👥 Mode: {MODE_LABEL[mode]}"
    )


@bot.on_callback_query(filters.regex("^checkjoin$"))
async def checkjoin_cb(_, cb: CallbackQuery):
    uid = cb.from_user.id
    info = user_data.get(uid)
    if not info or info.get("step") not in ("pending", "audio"):
        return await cb.answer("Pehle invite link bhejo", show_alert=True)
    try:
        chat_id = await _resolve_chat_id(uid)
    except Exception:
        chat_id = None
    if chat_id:
        user_data[uid]["step"] = "mode"
        if info.get("hash"):
            INVITE_HASH[chat_id] = info["hash"]
        LAST_CHAT[uid] = chat_id
        CHAT_NAMES[chat_id] = user_data[uid].get("group") or str(chat_id)
        await cb.message.edit_text(
            f"✅ **Approved & Joined:** {user_data[uid].get('group', chat_id)}\n\n"
            "👥 **Kaunsa assistant mode?**",
            reply_markup=_mode_kb()
        )
        await cb.answer("Joined! 🎉")
    else:
        await cb.answer("⏳ Abhi pending hai — thodi der baad try karo", show_alert=True)


# ---------------- media (audio + video files) ----------------

@bot.on_message(
    filters.private &
    (filters.audio | filters.voice | filters.video | filters.video_note |
     filters.document | filters.animation)
)
async def media_handler(_, m: Message):
    uid = m.from_user.id
    if not is_allowed(uid):
        return await _deny(m)
    info = user_data.get(uid)
    if not info or info.get("step") not in ("audio", "pending", "mode"):
        return await m.reply_text("ℹ️ Pehle /start karo, phir group ka username/link bhejo")
    chat_id = info.get("chat_id")
    if not chat_id:
        return await m.reply_text("ℹ️ Pehle group select karo (username/link bhejo)")

    doc = m.document
    if doc:
        mime = doc.mime_type or ""
        if not (mime.startswith("audio/") or mime.startswith("video/")):
            return await m.reply_text("❌ Sirf audio ya video file bhejo")

    is_video = bool(m.video or m.video_note or (doc and doc.mime_type.startswith("video/")))
    fname = (
        getattr(m.audio, "file_name", None)
        or getattr(m.video, "file_name", None)
        or getattr(m.document, "file_name", None)
        or ("Voice Note" if m.voice else "Media")
    )
    gname = info.get("group") or CHAT_NAMES.get(chat_id, str(chat_id))

    if doc and (doc.file_size or 0) > MAX_TG_SIZE:
        return await m.reply_text("❌ File 2GB se badi hai — Telegram limit")

    msg = await m.reply_text("⬇️ Downloading — **super fast** ⚡")
    try:
        os.makedirs(DOWNLOAD_DIR, exist_ok=True)
        dest = _dl_path(fname)
        await m.download(dest)
        pos = _enqueue(chat_id, {
            "path": dest, "title": fname, "kind": "video" if is_video else "audio",
            "uid": uid, "group": gname,
        })
        await msg.edit_text(
            f"📥 **Aᴅᴅᴇᴅ Tᴏ Qᴜᴇᴜᴇ Aᴛ #{pos}**\n\n"
            f"{'🎬' if is_video else '🎵'} **{fname[:40]}**\n"
            f"📻 Group: **{gname}**"
        )
        await send_log(
            ("🎬 **Video Request**" if is_video else "🎵 **Music Request**") + "\n\n"
            f"👤 User: {_mention(m.from_user)}\n"
            f"📱 Username: {_uname(m.from_user)}\n"
            f"🆔 ID: `{uid}`\n"
            f"📁 File: `{fname[:40]}`\n"
            f"📻 Group: {gname}"
        )
    except Exception as e:
        await msg.edit_text(f"❌ Error:\n`{e}`")


# ---------------- assistant session management (owner) ----------------

@bot.on_message(filters.command("addsession") & filters.private)
async def addsession_cmd(_, m: Message):
    if not is_admin(m.from_user.id):
        return await m.reply_text("👑 Owner only")
    session = ""
    if len(m.command) > 1:
        session = m.command[1].strip()
    elif m.reply_to_message and m.reply_to_message.text:
        session = m.reply_to_message.text.strip().split()[0]
    if not session:
        return await m.reply_text(
            "Usage:\n`/addsession <session_string>`\n"
            "ya session string wale message ko reply kar do"
        )
    wait = await m.reply_text("⏳ Logging in new assistant...")
    try:
        idx = len(ASSISTANTS)
        name, c, tc = await _start_assistant(session, idx)
    except Exception as e:
        return await wait.edit_text(f"❌ Login failed (invalid session?):\n`{str(e)[:150]}`")
    ASSISTANTS.append((name, c, tc))
    _save_sessions()
    await wait.edit_text(
        f"✅ **Assistant #{len(ASSISTANTS) - 1} logged in!**\n\n"
        f"👤 {name}\n\n"
        "👥 Ab mode 2️⃣/3️⃣ me ye assistant bhi VC jayega"
    )
    await send_log(
        "➕ **Assistant Added**\n\n"
        f"👤 By: {_mention(m.from_user)}\n"
        f"👻 New assistant: {name}\n"
        f"📊 Total: {len(ASSISTANTS)}"
    )


@bot.on_message(filters.command("sessions") & filters.private)
async def sessions_cmd(_, m: Message):
    if not is_admin(m.from_user.id):
        return await m.reply_text("👑 Owner only")
    if not ASSISTANTS:
        return await m.reply_text("❌ Koi assistant loaded nahi")
    lines = [f"👻 **Assistants ({len(ASSISTANTS)}):**\n"]
    for i, (name, c, tc) in enumerate(ASSISTANTS):
        pres = sum(1 for (cid, idx) in PRESENCE_JOINED if idx == i)
        lines.append(f"  #{i} — {name}\n      🎙 presence in {pres} chats")
    lines.append("\n➕ Add: /addsession `<string>`")
    lines.append("➖ Remove: /delsession `<n>`")
    await m.reply_text("\n".join(lines))


@bot.on_message(filters.command("delsession") & filters.private)
async def delsession_cmd(_, m: Message):
    if not is_admin(m.from_user.id):
        return await m.reply_text("👑 Owner only")
    if len(m.command) < 2 or not m.command[1].isdigit():
        return await m.reply_text("Usage: /delsession `<assistant_number>`\nList: /sessions")
    idx = int(m.command[1])
    if idx <= 0 or idx >= len(ASSISTANTS):
        return await m.reply_text("❌ Galat number (0 = primary, delete nahi hota)")
    name, c, tc = ASSISTANTS[idx]
    for (cid, i) in [p for p in list(PRESENCE_JOINED) if p[1] == idx]:
        PRESENCE_JOINED.discard((cid, i))
    try:
        await c.stop()
    except Exception:
        pass
    ASSISTANTS.pop(idx)
    _save_sessions()
    await m.reply_text(f"✅ Assistant #{idx} ({name}) removed")
    await send_log(f"➖ **Assistant Removed:** #{idx} {name}")


# ---------------- main ----------------

async def _start_primary():
    """Primary session start; revoked ho to sessions.json se promote karo."""
    global SESSION_STRING, userbot, calls
    try:
        await userbot.start()
        return True
    except Exception as e:
        print(f"⚠️ Primary session failed: {str(e)[:120]}")
        try:
            await userbot.terminate()
        except Exception:
            pass
    try:
        with open(SESSIONS_FILE) as f:
            saved = json.load(f)
    except Exception:
        saved = []
    for i, item in enumerate(saved):
        s = (item.get("session") or "").strip() if isinstance(item, dict) else ""
        if not s:
            continue
        print(f"🔁 Trying saved session #{i + 1} as primary...")
        try:
            userbot = Client(
                "vc_userbot",
                api_id=ASSISTANT_API_ID,
                api_hash=ASSISTANT_API_HASH,
                session_string=s,
            )
            await userbot.start()
            SESSION_STRING = s
            me = await userbot.get_me()
            print(f"✅ Promoted to primary: {me.first_name} ({me.id})")
            calls = PyTgCalls(userbot)
            await calls.start()
            # promoted session ko saved list se hata do (ab wo .env wala hai)
            saved.pop(i)
            with open(SESSIONS_FILE, "w") as f:
                json.dump(saved, f, indent=1)
            return True
        except Exception as e2:
            print(f"⚠️ Saved session #{i + 1} bhi fail: {str(e2)[:100]}")
    return False


async def main():
    global BOT_UN
    print("🚀 Starting VChidder bot...")
    await bot.start()
    me = await bot.get_me()
    BOT_UN = me.username or ""
    print(f"✅ Bot started: @{BOT_UN}")

    ok = await _start_primary()
    if not ok:
        print("❌ Koi valid assistant session nahi mila!")
        print("   Fix: sessions.json me naya session daalo ya .env ka SESSION_STRING update karo.")
        raise SystemExit(1)

    ub = await userbot.get_me()
    print(f"✅ Userbot started: {ub.first_name} ({ub.id})")
    ASSISTANTS.append(
        (f"{ub.first_name} (@{ub.username or ub.id})", userbot, calls)
    )
    print(f"👑 Owner: {OWNER_ID} | 🛡 Sudo: {sorted(SUDO_IDS)} | ✅ Approved: {len(APPROVED)}")

    if SESSION_STRING != os.getenv("SESSION_STRING", "").strip():
        # promoted session — .env ko bhi update kar do (persistent)
        try:
            env_path = os.path.join(BASE_DIR, ".env")
            lines = []
            with open(env_path) as f:
                lines = f.readlines()
            with open(env_path, "w") as f:
                for line in lines:
                    if line.startswith("SESSION_STRING="):
                        f.write(f"SESSION_STRING={SESSION_STRING}\n")
                    else:
                        f.write(line)
        except Exception as e:
            print("env update failed:", e)

    # saved extra assistants (sessions.json) ko startup par load karo
    try:
        with open(SESSIONS_FILE) as f:
            saved = json.load(f)
    except Exception:
        saved = []
    for i, item in enumerate(saved, start=1):
        s = (item.get("session") or "").strip() if isinstance(item, dict) else ""
        if not s:
            continue
        try:
            name, c, tc = await _start_assistant(s, i)
            ASSISTANTS.append((name, c, tc))
            print(f"✅ Assistant #{i} loaded: {name}")
        except Exception as e:
            print(f"⚠️ Assistant #{i} load failed: {str(e)[:80]}")

    asyncio.ensure_future(_mic_keepalive_worker())
    print(f"🎙 Mic keepalive worker running (refresh: {MIC_REFRESH}s)")
    print(f"⚡ yt-dlp: {_ytdlp_bin() or 'python -m fallback'} | "
          f"aria2c: {'OK' if shutil.which('aria2c') else 'aiohttp fallback'} | "
          f"YT cookies: {'OK' if _yt_extra_args() else 'nahi (direct links OK)'}")
    print("🎵 VC Bot Running")

    sudo_txt = ", ".join(f"`{x}`" for x in sorted(SUDO_IDS)) or "—"
    await send_log(
        "🔋 **Bot Started!**\n\n"
        f"🤖 Bot: @{BOT_UN}\n"
        f"👻 Assistant: {ub.first_name} (`{ub.id}`)\n"
        f"👻 Total Assistants: `{len(ASSISTANTS)}`\n"
        f"👑 Owner: `{OWNER_ID}`\n"
        f"🛡 Sudo: {sudo_txt}\n"
        f"✅ Approved: `{len(APPROVED)}`\n\n"
        "⚡ Fast • 🎵 Queue • 🔊 Volume • 🎭 Anonymous"
    )

    await asyncio.Event().wait()


if __name__ == "__main__":
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    loop.run_until_complete(main())
