# 🎵 VChidder — Anonymous VC Player Bot

<p align="center">
  <b>Telegram Voice-Chat Music & Video Streaming Bot</b><br>
  Pyrogram (Kurigram) + PyTgCalls • Multi-Assistant • Mic-Off Hidden Streaming • Approve System
</p>

---

## ✨ Features

- 🎵 Music + 🎬 Video streaming in group voice chats
- 👻 Anonymous playback via assistant (userbot) account
- 🔇 **Mic-Off hidden streaming** — joins VC with muted badge (Webgram-style), audio still flows
- 👥 **Multi-assistant** — add unlimited assistant sessions, all join together (`/addsession`)
- 🎛 `/ac` live panel — see every active presence, one-tap OFF
- ✅ Owner/Sudo **approve system** with inline Approve/Reject buttons
- 📨 Private invite-link support with **join-request flow**
- 📴 Log group integration — starts, users, requests, stops
- ⏯ Player controls: Pause / Resume / Replay / Stop + /ac panel
- ♻️ 25-min auto-refresh keepalive — stream never dies

---

## 🚀 Deploy (VPS / Linux)

```bash
git clone https://github.com/VIP-saksham/VChidder
cd VChidder
python3 -m venv venv
venv/bin/pip install -r requirements.txt
cp .env.example .env   # edit with your values
ffmpeg -y -f lavfi -i "sine=frequency=17400:sample_rate=48000" -f lavfi -i "sine=frequency=60:sample_rate=48000" \
  -filter_complex "[0:a]volume=0.12[a];[1:a]volume=0.004[b];[a][b]amix=inputs=2:normalize=0" \
  -t 1800 -b:a 128k silent.mp3
bash start.sh
tail -f log.txt
```

> **Mic-Off mode needs the pytgcalls patch** — see `PATCHES.md`.

---

## ⚙️ Variables

See [.env.example](.env.example) — API_ID, API_HASH, BOT_TOKEN, SESSION_STRING, OWNER_ID required.

---

## 📖 Commands

| | |
|---|---|
| /start | Premium start panel + buttons |
| /playing /pause /resume /stop | Player controls |
| /status | Uptime, active streams |
| /micon /micoff | Presence with MIC-OFF badge (owner) |
| /ac | Active calls panel, one-tap OFF (owner) |
| /addsession /sessions /delsession | Multi-assistant manager (owner) |
| /approve /unapprove /approved | Access control (owner) |

---

## 👑 Credits

- Owner: [@Truenakshu](https://t.me/Truenakshu)
- Channel: [@TheHellBots](https://t.me/TheHellBots)
