import asyncio
import json
import os
import time

# single explicit event loop shared by all clients (avoids "attached to a different loop")
loop = asyncio.new_event_loop()
asyncio.set_event_loop(loop)

from dotenv import load_dotenv

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
SESSIONS_FILE = "/root/VChidder/sessions.json"
CHANNEL_LINK = os.getenv("CHANNEL_LINK", "https://t.me/TheHellBots")
SUPPORT_LINK = os.getenv("SUPPORT_LINK", "https://t.me/SUNSHINE_GC")
OWNER_LINK = os.getenv("OWNER_LINK", "https://t.me/Truenakshu")

if not BOT_TOKEN or not API_ID or not API_HASH:
    raise SystemExit("❌ BOT_TOKEN / API_ID / API_HASH missing — set them in /root/VChidder/.env")

if not SESSION_STRING:
    raise SystemExit(
        "❌ SESSION_STRING missing in /root/VChidder/.env\n"
        "   Generate a Pyrogram string session (same API_ID/API_HASH),\n"
        "   put SESSION_STRING=... in .env and restart the bot."
    )

if not OWNER_ID:
    raise SystemExit("❌ OWNER_ID missing in .env (your Telegram user id)")

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

user_data = {}      # user id -> flow state
active_calls = {}   # user id -> chat id
chat_calls = {}     # chat id -> user id
paused_calls = set()


def _buttons(paused=False):
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("⏮ Replay", callback_data="replay"),
                InlineKeyboardButton(
                    "⏯ Resume" if paused else "⏯ Pause",
                    callback_data="resume" if paused else "pause"
                ),
                InlineKeyboardButton("⏹ Stop", callback_data="stop")
            ],
            _support_row(),
        ]
    )


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


def _start_text(name):
    return (
        "╔═══════════════════════╗\n"
        "   🎵 **V C H I D D E R** 🎵\n"
        "  ✦ Anonymous VC Player ✦\n"
        "╚═══════════════════════╝\n\n"
        f"👋 Hey **{name}**, welcome!\n\n"
        "🎧 Main kisi bhi group ke voice chat me\n"
        "music ya video **stream** karta hoon —\n"
        "bilkul **anonymous** 👻\n\n"
        "✨ **Quick Start**\n"
        "  ➊ Group ka @username ya invite link bhejo\n"
        "  ➋ Audio ya video file bhejo\n"
        "  ➌ VC me play — full control ke saath 🎛\n\n"
        "🎯 **Features**\n"
        "  🎵 Music • 🎬 Video • 👻 Anonymous\n"
        "  🔇 Mic-Off Mode • 👥 Multi-Assistant\n\n"
        "⚡ Fast • 🔒 Secure • 🎭 Anonymous"
    )


CMDS_TEXT = (
    "📖 **Commands Guide**\n\n"
    "🎵 **Player**\n"
    "  ▶️ /playing — ab kya chal raha hai\n"
    "  ⏸ /pause  •  ▶️ /resume  •  ⏹ /stop\n\n"
    "📊 **Info**\n"
    "  📊 /status — bot stats & uptime\n\n"
    "👑 **Owner Only**\n"
    "  🎙 /micon • /micoff — presence toggle\n"
    "  🎛 /ac — active calls panel\n"
    "  👥 /addsession • /sessions • /delsession\n"
    "  ✅ /approve • /unapprove • /approved\n"
)


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
        "🎵 Ab audio ya video file bhejo — turant play hoga"
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


def _deny_kb():
    return InlineKeyboardMarkup(
        [
            _support_row(),
            [InlineKeyboardButton("👑 Owner", url=OWNER_LINK)],
        ]
    )


def _check_button():
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("📋 Check Join Status", callback_data="checkjoin")]]
    )


def _cleanup_files(uid):
    path = user_data.get(uid, {}).get("audio")
    if path:
        try:
            os.remove(path)
        except OSError:
            pass


def _clear_state(uid):
    chat_id = active_calls.pop(uid, None)
    if chat_id:
        chat_calls.pop(chat_id, None)
    paused_calls.discard(chat_id)
    _cleanup_files(uid)
    user_data.pop(uid, None)
    return chat_id


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


def _marked_id(raw_chat):
    """Convert a raw Chat/Channel object to a pyrogram marked chat id."""
    if isinstance(raw_chat, raw_types.Channel):
        return utils.get_channel_id(raw_chat.id)
    return -raw_chat.id


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


# ---------------- multi-assistant presence ----------------

LAST_CHAT = {}       # uid -> last selected chat (for /micon)
CHAT_NAMES = {}      # chat_id -> group name (for /ac panel)
INVITE_HASH = {}     # chat_id -> invite hash (private groups, for extra assistants)
ASSISTANTS = []      # [(name, Client, PyTgCalls)] — primary pehla
PRESENCE_JOINED = set()  # {(chat_id, assistant_idx)}
MUTED_FILE = "/root/VChidder/muted_chats.json"


def _load_mute_flag():
    try:
        with open(MUTED_FILE) as f:
            return set(json.load(f))
    except Exception:
        return set()


def _save_mute_flag():
    try:
        with open(MUTED_FILE, "w") as f:
            json.dump(sorted(MUTE_FLAG), f)
    except Exception as e:
        print("mute flag save failed:", e)


MUTE_FLAG = _load_mute_flag()  # chats jahan assistant MIC OFF badge ke saath join hoga


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


async def _restore_mic(chat_id):
    """Stop ke baad presence wapas chalu karta hai (jitne assistants joined the)."""
    await asyncio.sleep(2)
    if chat_id in active_calls.values():
        return
    # presence = muted badge (file patch join ke waqt padhta hai)
    MUTE_FLAG.add(chat_id)
    _save_mute_flag()
    for (cid, idx) in list(PRESENCE_JOINED):
        if cid != chat_id:
            continue
        try:
            tc = ASSISTANTS[idx][2]
            await tc.play(chat_id, MediaStream(SILENT_FILE))
        except Exception as e:
            print(f"mic restore error {chat_id}/{idx}:", e)


async def _mic_keepalive_worker():
    """Har MIC_REFRESH sec me presence refresh — VC kabhi end nahi hota."""
    while True:
        await asyncio.sleep(MIC_REFRESH)
        for (chat_id, idx) in list(PRESENCE_JOINED):
            if idx == 0 and chat_id in active_calls.values():
                continue  # primary pe real music chal raha hai
            try:
                tc = ASSISTANTS[idx][2]
                await tc.leave_call(chat_id)
                await asyncio.sleep(2)
                await tc.play(chat_id, MediaStream(SILENT_FILE))
                print(f"🎙 presence refreshed: {chat_id} asst#{idx}")
            except Exception as e:
                print(f"presence keepalive error {chat_id}/{idx}:", e)


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
        # send join request to owner with approve buttons
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
    grp = user_data.get(uid, {}).get("group", "?")
    kind = user_data.get(uid, {}).get("kind", "audio")
    state = "⏸ Paused" if chat_id in paused_calls else "▶️ Playing"
    icon = "🎬" if kind == "video" else "🎵"
    await m.reply_text(f"{icon} {state} in `{grp}`")


@bot.on_message(filters.command("status") & filters.private)
async def status_cmd(_, m: Message):
    if not is_allowed(m.from_user.id):
        return await _deny(m)
    up = int(time.time() - START_TS)
    h, rem = divmod(up, 3600)
    mn, s = divmod(rem, 60)
    groups = []
    for uid, cid in active_calls.items():
        g = user_data.get(uid, {}).get("group", str(cid))
        st = "⏸" if cid in paused_calls else "▶️"
        groups.append(f"{st} `{g}`")
    text = (
        f"📊 **Bot Status**\n\n"
        f"⏱ Uptime: `{h}h {mn}m {s}s`\n"
        f"🎚 Active streams: `{len(active_calls)}`\n"
        f"👥 Approved users: `{len(APPROVED)}`\n"
    )
    if groups:
        text += "\n**Now playing:**\n" + "\n".join(groups)
    await m.reply_text(text)


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
    gname = info.get("group", str(chat_id))
    CHAT_NAMES[chat_id] = gname
    if m.command[0] == "micon":
        # mic OFF badge join se PEHLE set (patched lib join ke waqt file padhta hai)
        MUTE_FLAG.add(chat_id)
        _save_mute_flag()
        ok, fail = [], []
        for idx in range(len(ASSISTANTS)):
            try:
                if (chat_id, idx) in PRESENCE_JOINED:
                    try:
                        await ASSISTANTS[idx][2].leave_call(chat_id)
                        await asyncio.sleep(1.5)
                    except Exception:
                        pass
                if idx > 0:
                    await _ensure_assistant_member(idx, chat_id)
                tc = ASSISTANTS[idx][2]
                await tc.play(chat_id, MediaStream(SILENT_FILE))
                PRESENCE_JOINED.add((chat_id, idx))
                ok.append(ASSISTANTS[idx][0])
            except Exception as e:
                fail.append(f"{ASSISTANTS[idx][0]}: {str(e)[:60]}")
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
        MUTE_FLAG.discard(chat_id)
        _save_mute_flag()
        for (cid, idx) in [p for p in list(PRESENCE_JOINED) if p[0] == chat_id]:
            try:
                await ASSISTANTS[idx][2].leave_call(chat_id)
            except Exception:
                pass
            PRESENCE_JOINED.discard((cid, idx))
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
    """Active calls panel ke live buttons."""
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
        playing = "🎵 music" if (idx == 0 and cid in active_calls.values()) else "mic only"
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
    try:
        await ASSISTANTS[idx][2].leave_call(cid)
    except Exception:
        pass
    PRESENCE_JOINED.discard((cid, idx))
    MUTE_FLAG.discard(cid)
    _save_mute_flag()
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
        "Ab /micon se is group me sab assistants ka mic ON hoga"
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


@bot.on_message(filters.command("stop"))
async def stop_cmd(_, m: Message):
    if not m.from_user or not is_allowed(m.from_user.id):
        return
    if m.chat.type == enums.ChatType.PRIVATE:
        uid = m.from_user.id
        if uid not in active_calls:
            return await m.reply_text("❌ Nothing is playing")
        chat_id = active_calls[uid]
        group_name = user_data.get(uid, {}).get("group", "chat")
    else:
        chat_id = m.chat.id
        uid = chat_calls.get(chat_id)
        if uid is None:
            return await m.reply_text("❌ Nothing is playing here")
        group_name = m.chat.title or str(chat_id)
    try:
        await calls.leave_call(chat_id)
    except Exception:
        pass
    _clear_state(uid)
    await m.reply_text(f"⏹ Stopped in **{group_name}**")
    await send_log(
        "⏹ **Stream Stopped**\n\n"
        f"👤 User: {_mention(m.from_user)}\n"
        f"📻 Group: {group_name}"
    )
    asyncio.ensure_future(_restore_mic(chat_id))


# ---------------- group selection flow ----------------

@bot.on_message(filters.private & filters.text)
async def text_handler(_, m: Message):
    uid = m.from_user.id
    if not is_allowed(uid):
        return await _deny(m)
    if uid not in user_data:
        return
    if user_data[uid].get("step") not in ("group", "pending"):
        return await m.reply_text("🎵 Ab audio/video file bhejo")

    raw = m.text.strip()

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
                    "step": "audio",
                    "group": invite.chat.title or str(marked),
                    "chat_id": marked,
                    "invite": True
                }
                INVITE_HASH[marked] = hash_part
                LAST_CHAT[uid] = marked
                CHAT_NAMES[marked] = invite.chat.title or str(marked)
                return await wait.edit_text(
                    f"✅ **Already in:** {invite.chat.title}\n\n"
                    "🎵 Ab audio ya video file bhejo"
                )

            # CASE 2/3: joinable preview -> import the invite
            if isinstance(invite, raw_types.ChatInvite):
                title_guess = invite.title or title_guess
                try:
                    updates = await userbot.invoke(
                        raw_functions.messages.ImportChatInvite(hash=hash_part)
                    )
                except Exception as je:
                    # request-approval group -> server asks for join request
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

                # joined instantly — pull chat id from updates
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
                        "step": "audio",
                        "group": gname,
                        "chat_id": marked,
                        "invite": True
                    }
                    INVITE_HASH[marked] = hash_part
                    LAST_CHAT[uid] = marked
                    CHAT_NAMES[marked] = gname
                    return await wait.edit_text(
                        f"✅ **Joined:** {gname}\n\n"
                        "🎵 Ab audio ya video file bhejo"
                    )
                # joined but dialog not visible yet -> wait as pending
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

    # ---------- public group @username ----------
    if user_data[uid].get("step") == "group":
        grp = raw.split("t.me/")[-1].replace("@", "").strip().rstrip("/")
        if not grp or " " in grp or "/" in grp:
            return await m.reply_text(
                "❌ Valid group @username ya invite link bhejo"
            )
        wait = await m.reply_text("🔎 Checking group...")
        try:
            chat = await userbot.get_chat(grp)
        except Exception as e:
            return await wait.edit_text(
                f"❌ Player account @{grp} access nahi kar pa raha.\n"
                "Group public ho ya player account ko add karo.\n\n"
                f"`{e}`"
            )
        user_data[uid] = {"step": "audio", "group": grp, "chat_id": chat.id}
        LAST_CHAT[uid] = chat.id
        CHAT_NAMES[chat.id] = grp
        await wait.edit_text(f"✅ **Group set:** {grp}\n\n🎵 Ab audio ya video file bhejo")
    else:
        await m.reply_text("🎵 Ab audio ya video file bhejo")


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
        user_data[uid]["step"] = "audio"
        if info.get("hash"):
            INVITE_HASH[chat_id] = info["hash"]
        LAST_CHAT[uid] = chat_id
        CHAT_NAMES[chat_id] = user_data[uid].get("group") or str(chat_id)
        await cb.message.edit_text(
            f"✅ **Approved & Joined:** {user_data[uid].get('group', chat_id)}\n\n"
            "🎵 Ab audio ya video file bhejo"
        )
        await cb.answer("Joined! 🎉")
    else:
        await cb.answer("⏳ Abhi pending hai — thodi der baad try karo", show_alert=True)


# ---------------- media (audio + video) ----------------

@bot.on_message(
    filters.private &
    (filters.audio | filters.voice | filters.video | filters.video_note |
     filters.document | filters.animation)
)
async def media_handler(_, m: Message):
    uid = m.from_user.id
    if not is_allowed(uid):
        return await _deny(m)
    if uid not in user_data or user_data[uid].get("step") not in ("audio", "pending"):
        return await m.reply_text("ℹ️ Pehle /start karo, phir group ka username/link bhejo")

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
    gname = user_data.get(uid, {}).get("group", "?")
    await send_log(
        ("🎬 **Video Request**" if is_video else "🎵 **Music Request**") + "\n\n"
        f"👤 User: {_mention(m.from_user)}\n"
        f"📱 Username: {_uname(m.from_user)}\n"
        f"🆔 ID: `{uid}`\n"
        f"📁 File: `{fname[:40]}`\n"
        f"📻 Group: {gname}"
    )

    msg = await m.reply_text("⬇️ Downloading...")
    try:
        os.makedirs("downloads", exist_ok=True)
        file_path = await m.download("downloads/")
        await msg.edit_text("🎙 Joining voice chat...")
        chat_id = await _resolve_chat_id(uid)
        # sound-first: music hamesha unmuted join (muted join par server audio forward nahi karta)
        MUTE_FLAG.discard(chat_id)
        _save_mute_flag()
        await calls.play(chat_id, MediaStream(file_path))
        old = active_calls.get(uid)
        if old and old != chat_id:
            chat_calls.pop(old, None)
        active_calls[uid] = chat_id
        chat_calls[chat_id] = uid
        paused_calls.discard(chat_id)
        user_data[uid]["audio"] = file_path
        user_data[uid]["kind"] = "video" if is_video else "audio"
        LAST_CHAT[uid] = chat_id
        grp = user_data[uid]["group"]
        CHAT_NAMES[chat_id] = grp
        icon = "🎬" if is_video else "🎵"
        base = os.path.basename(file_path)[:30]
        await msg.edit_text(
            "╭────── 🎧 **Now Playing** ──────╮\n"
            f"  {icon} **{base}**\n"
            f"  📻 Group: **{grp}**\n"
            "  🎭 Mode: Anonymous\n"
            "╰────────────────────────────╯",
            reply_markup=_buttons()
        )
    except Exception as e:
        await msg.edit_text(f"❌ Error:\n`{e}`")


# ---------------- player callbacks ----------------

@bot.on_callback_query(filters.regex("^replay$"))
async def replay(_, cb: CallbackQuery):
    uid = cb.from_user.id
    if not is_allowed(uid):
        return await cb.answer("Not approved", show_alert=True)
    if uid not in active_calls:
        return await cb.answer("Nothing playing", show_alert=True)
    path = user_data.get(uid, {}).get("audio")
    if not path or not os.path.exists(path):
        return await cb.answer("File missing — dobara bhejo", show_alert=True)
    try:
        chat_id = active_calls[uid]
        MUTE_FLAG.discard(chat_id)
        _save_mute_flag()
        await calls.play(chat_id, MediaStream(path))
        paused_calls.discard(chat_id)
        await cb.answer("⟲ Replaying")
        try:
            await cb.message.edit_reply_markup(reply_markup=_buttons())
        except Exception:
            pass
    except Exception as e:
        await cb.answer(f"Error: {e}", show_alert=True)


@bot.on_callback_query(filters.regex("^(pause|resume)$"))
async def pause_resume_cb(_, cb: CallbackQuery):
    uid = cb.from_user.id
    if not is_allowed(uid):
        return await cb.answer("Not approved", show_alert=True)
    chat_id = active_calls.get(uid)
    if not chat_id:
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


@bot.on_callback_query(filters.regex("^stop$"))
async def stop_cb(_, cb: CallbackQuery):
    uid = cb.from_user.id
    if not is_allowed(uid):
        return await cb.answer("Not approved", show_alert=True)
    chat_id = active_calls.get(uid)
    if not chat_id:
        return await cb.answer("Nothing playing", show_alert=True)
    try:
        await calls.leave_call(chat_id)
    except Exception:
        pass
    _clear_state(uid)
    await cb.message.edit_text("⏹ Voice Chat Stopped")
    await cb.answer("Stopped")
    asyncio.ensure_future(_restore_mic(chat_id))


# ---------------- main ----------------

async def main():
    global BOT_UN
    print("🚀 Starting VChidder bot...")
    await bot.start()
    me = await bot.get_me()
    BOT_UN = me.username or ""
    print(f"✅ Bot started: @{BOT_UN}")

    try:
        await userbot.start()
    except Exception as e:
        print(f"❌ Userbot session invalid: {e!r}")
        print("   Regenerate SESSION_STRING and update .env, then restart.")
        raise

    ub = await userbot.get_me()
    print(f"✅ Userbot started: {ub.first_name} ({ub.id})")
    print(f"👑 Owner: {OWNER_ID} | 🛡 Sudo: {sorted(SUDO_IDS)} | ✅ Approved: {len(APPROVED)}")

    await calls.start()
    print("✅ PyTgCalls started")
    asyncio.ensure_future(_mic_keepalive_worker())
    print(f"🎙 Mic keepalive worker running (refresh: {MIC_REFRESH}s)")
    print("🎵 VC Bot Running")

    sudo_txt = ", ".join(f"`{x}`" for x in sorted(SUDO_IDS)) or "—"
    await send_log(
        "🔋 **Bot Started!**\n\n"
        f"🤖 Bot: @{BOT_UN}\n"
        f"👻 Assistant: {ub.first_name} (`{ub.id}`)\n"
        f"👑 Owner: `{OWNER_ID}`\n"
        f"🛡 Sudo: {sudo_txt}\n"
        f"✅ Approved: `{len(APPROVED)}`\n\n"
        "⚡ Fast • 🔒 Secure • 🎭 Anonymous"
    )

    await asyncio.Event().wait()


if __name__ == "__main__":
    loop.run_until_complete(main())
