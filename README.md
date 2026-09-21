# 🎵 VChidder — Anonymous VC Player Bot

<p align="center">
  <b>Telegram Voice-Chat Music, Video & YouTube Streaming Bot</b><br>
  Pyrogram (Kurigram) + PyTgCalls • Smart Queue • 5 Volume Levels • Multi-Assistant • Approve System
</p>

---

## ✨ Features

- 🎵 Music + 🎬 Video + ▶️ **YouTube (audio-only)** + 🔗 direct-link streaming
- 📋 **Smart Queue** — files/links add hote hi `Aᴅᴅᴇᴅ Tᴏ Qᴜᴇᴜᴇ Aᴛ #N`, auto next-play,
  ⏭ skip, 🔄 swap buttons (`/sw 1 2` ya inline), first-ready pehle play
- 🔊 **5 volume levels** — Slow / Mid / High / Very High / **Super High** (`/vol` panel)
- ⚡ **Super-fast downloads** — Telegram parallel fetch, `aria2c` 16-connection direct links,
  yt-dlp `-N 8` YouTube audio (1hr ≈ 30s on good pipe)
- 👻 Anonymous playback via assistant (userbot) accounts
- 👥 **Multi-assistant modes** — 1️⃣ Single • 2️⃣ One By One (failover) • 3️⃣ All In One
- 🎙 `/micon` presence with **MIC-OFF badge** (pytgcalls patch — see PATCHES.md)
- 🗣 `/tts` live text-to-speech in VC (hi/en voices)
- ✅ Owner/Sudo **approve system** with inline Approve/Reject buttons
- 📨 Private invite-link support with **join-request flow**
- 📴 Log group integration — starts, users, requests, plays, stops
- 🧪 **22 contract tests** (`test_vc.py`) — `venv/bin/python -m pytest test_vc.py`

---

## 🚀 Deploy (VPS / Linux)

```bash
git clone https://github.com/VIP-saksham/VChidder
cd VChidder
python3 -m venv venv
venv/bin/pip install -r requirements.txt
venv/bin/pip install yt-dlp          # YouTube support
apt-get install -y aria2 ffmpeg      # fast downloads + streaming
cp .env.example .env                 # edit with your values
# silent presence file (mic-ON badge for /micon)
ffmpeg -y -f lavfi -i "sine=frequency=17400:sample_rate=48000" -f lavfi -i "sine=frequency=60:sample_rate=48000" \
  -filter_complex "[0:a]volume=0.12[a];[1:a]volume=0.004[b];[a][b]amix=inputs=2:normalize=0" \
  -t 1800 -b:a 128k silent.mp3
bash start.sh
tail -f log.txt
```

> **Mic-OFF badge needs the pytgcalls patch** — see `PATCHES.md`.

### YouTube "Sign in to confirm you're not a bot"

Datacenter IPs pe YouTube bot-check karta hai. Fix (1 min):
browser me YouTube logged-in → "Get cookies.txt" extension → export karo →
VPS par `/root/VChidder/yt_cookies.txt` daalo (ya `YT_COOKIES` env). Bas.

---

## ⚙️ Variables

See [.env.example](.env.example) — API_ID, API_HASH, BOT_TOKEN, SESSION_STRING, OWNER_ID required.

---

## 📖 Commands

| | |
|---|---|
| /start | Premium start panel + buttons |
| /playing /pause /resume /stop /sk | Player controls |
| /q /sw | Queue view + position swap |
| /vol | Volume panel (Slow → Super High) |
| /tts `<text>` | Live TTS in VC (hi:/en: prefix) |
| /status | Uptime, active streams, assistants |
| /micon /micoff | Presence with MIC-OFF badge (owner) |
| /ac | Active calls panel, one-tap OFF (owner) |
| /addsession /sessions /delsession | Multi-assistant manager (owner) |
| /approve /unapprove /approved | Access control (owner) |

---

## 🎚 Volume Levels

| Level | % |
|---|---|
| Slow | 30 |
| Mid | 70 |
| High (default) | 100 |
| Very High | 140 |
| Super High | 200 |

---

## 👑 Credits

- Owner: [@Truenakshu](https://t.me/Truenakshu)
- Channel: [@TheHellBots](https://t.me/TheHellBots)
