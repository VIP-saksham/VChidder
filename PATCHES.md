# 🔧 pytgcalls Patches (Mic-Off hidden streaming)

Stock pytgcalls hardcodes `muted=False` when joining group calls, so every
participant shows **mic ON**. To join with a **MIC OFF badge** (like Telegram
Web/Webgram does) while still streaming audio, 3 small patches are applied.

> Backups are saved as `.bak` next to each patched file.

## 1. `pytgcalls/mtproto/pyrogram_client.py`

`join_group_call()` gets a new parameter and uses it instead of the hardcoded flag:

```python
async def join_group_call(
    self, chat_id, json_join, video_stopped, join_as,
    invite_hash=None, public_key=None,
    _vc_join_muted: bool = False,          # ← NEW
):
    ...
    result = await self._invoke(
        JoinGroupCall(
            call=input_call,
            params=DataJSON(data=json_join),
            muted=_vc_join_muted,          # ← was muted=False
            ...
```

## 2. `pytgcalls/mtproto/mtproto_client.py`

The wrapper forwards the new parameter:

```python
async def join_group_call(
    self, chat_id, json_join, video_stopped, join_as,
    invite_hash=None, public_key=None,
    _vc_join_muted: bool = False,          # ← NEW
):
    return await self._bind_client.join_group_call(
        ..., public_key, _vc_join_muted,   # ← forwarded
    )
```

## 3. `pytgcalls/methods/internal/connect_call.py`

Before joining, reads `/root/VChidder/muted_chats.json` (array of chat ids)
and passes the flag:

```python
import json as _vcjson

def _vc_is_muted_chat(chat_id):
    try:
        with open("/root/VChidder/muted_chats.json") as f:
            return chat_id in set(_vcjson.load(f))
    except Exception:
        return False

...
result_params = await self._app.join_group_call(
    chat_id, payload, media_description.camera is None,
    self._cache_user_peer.get(chat_id), config.invite_hash,
    _vc_join_muted=_vc_is_muted_chat(chat_id),   # ← NEW
)
```

## 4. `pytgcalls/methods/internal/update_status.py` (optional but recommended)

Keeps the muted badge when stream state updates:

```python
def _vc_force_muted(chat_id):
    ...  # same file check

await self._app.set_call_status(
    chat_id,
    _vc_force_muted(chat_id) or state.muted,   # ← forced
    ...
)
```

## How the bot uses it

- `/micon` writes the chat id into `muted_chats.json` **before** joining
- Joining happens with `muted=True` → **MIC OFF badge**, audio still streams
- `/micoff` or the `/ac` OFF buttons clear the flag
- If `MUTED_MUSIC=0`, normal music play joins **unmuted**
