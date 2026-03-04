# ============================================================
#  vc_movie_bot.py — Single-file Telegram VC Movie Player Bot
#
#  Install:
#    pip install pyrogram tgcrypto pytgcalls yt-dlp pillow
#    sudo apt install ffmpeg
#
#  Run:
#    python vc_movie_bot.py
# ============================================================

import asyncio
import os
import re
import random
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from pyrogram import Client, filters
from pyrogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton,
)
from pytgcalls import PyTgCalls
from pytgcalls.types import Update
from pytgcalls.types.stream import StreamAudioEnded, StreamVideoEnded
from pytgcalls.types.input_stream import AudioVideoPiped, AudioPiped
from pytgcalls.types.input_stream.quality import HighQualityAudio, HighQualityVideo


# ═══════════════════════════════════════════════════════════════
#  CONFIG  ← fill these in before running
# ═══════════════════════════════════════════════════════════════

API_ID      = int(os.getenv("API_ID",    "YOUR_API_ID"))
API_HASH    =     os.getenv("API_HASH",   "YOUR_API_HASH")
BOT_TOKEN   =     os.getenv("BOT_TOKEN",  "YOUR_BOT_TOKEN")

DOWNLOAD_DIR    = os.getenv("DOWNLOAD_DIR", "./downloads")
DEFAULT_VOLUME  = 100
MAX_QUEUE       = 20
YTDLP_FORMAT    = "bestvideo[height<=720]+bestaudio/best[height<=720]"

os.makedirs(DOWNLOAD_DIR, exist_ok=True)


# ═══════════════════════════════════════════════════════════════
#  QUEUE & STATE
# ═══════════════════════════════════════════════════════════════

@dataclass
class MediaItem:
    title:        str
    file_path:    str
    duration:     int
    requested_by: str
    thumbnail:    Optional[str] = None


@dataclass
class ChatState:
    queue:        List[MediaItem] = field(default_factory=list)
    current:      Optional[MediaItem] = None
    position:     int   = 0
    paused:       bool  = False
    volume:       int   = DEFAULT_VOLUME
    speed:        float = 1.0
    loop:         bool  = False
    shuffle:      bool  = False
    panel_msg_id: Optional[int] = None


_states: dict[int, ChatState] = {}
_locks:  dict[int, asyncio.Lock] = {}


def get_state(chat_id: int) -> ChatState:
    if chat_id not in _states:
        _states[chat_id] = ChatState()
    return _states[chat_id]


def get_lock(chat_id: int) -> asyncio.Lock:
    if chat_id not in _locks:
        _locks[chat_id] = asyncio.Lock()
    return _locks[chat_id]


def fmt_time(seconds: int) -> str:
    if seconds <= 0:
        return "0:00"
    h, rem = divmod(int(seconds), 3600)
    m, s   = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def parse_time(text: str) -> Optional[int]:
    text = text.strip()
    try:
        parts = [int(p) for p in text.split(":")]
        if len(parts) == 1: return parts[0]
        if len(parts) == 2: return parts[0] * 60 + parts[1]
        if len(parts) == 3: return parts[0] * 3600 + parts[1] * 60 + parts[2]
    except ValueError:
        pass
    return None


def progress_bar(pos: int, dur: int, width: int = 18) -> str:
    if dur <= 0:
        return "▱" * width
    filled = min(int(width * pos / dur), width)
    return "▰" * filled + "●" + "▱" * (width - filled)


def format_queue(chat_id: int) -> str:
    state = get_state(chat_id)
    if not state.queue and not state.current:
        return "📭 Queue is empty."
    lines = []
    if state.current:
        pos = fmt_time(state.position)
        dur = fmt_time(state.current.duration)
        loop = " 🔁" if state.loop else ""
        lines.append(
            f"▶️ **Now Playing**{loop}\n"
            f"   `{state.current.title}`\n"
            f"   ⏱ {pos} / {dur}  👤 {state.current.requested_by}\n"
        )
    if state.queue:
        lines.append("📋 **Up Next:**")
        for i, item in enumerate(state.queue, 1):
            lines.append(
                f"  {i}. `{item.title}` — {fmt_time(item.duration)}"
                f"  👤 {item.requested_by}"
            )
    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════
#  DOWNLOADER
# ═══════════════════════════════════════════════════════════════

def _sanitize(name: str) -> str:
    return re.sub(r'[\\/*?:"<>|]', "_", name)[:80]


async def _run(cmd: list[str]) -> Tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate()
    return proc.returncode, out.decode(), err.decode()


async def probe_duration(path: str) -> int:
    _, out, _ = await _run([
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", path,
    ])
    try:
        return int(float(out.strip()))
    except Exception:
        return 0


async def probe_title(path: str) -> str:
    _, out, _ = await _run([
        "ffprobe", "-v", "error",
        "-show_entries", "format_tags=title",
        "-of", "default=noprint_wrappers=1:nokey=1", path,
    ])
    t = out.strip()
    return t if t else os.path.splitext(os.path.basename(path))[0]


async def extract_thumbnail(video_path: str, out_path: str) -> bool:
    code, _, _ = await _run([
        "ffmpeg", "-y", "-ss", "5",
        "-i", video_path,
        "-frames:v", "1", "-q:v", "2", out_path,
    ])
    return code == 0 and os.path.exists(out_path)


async def download_telegram_file(client: Client, message: Message) -> Optional[str]:
    media = message.video or message.document or message.animation
    if not media:
        return None
    fname = _sanitize(getattr(media, "file_name", None) or f"tg_{media.file_id}.mp4")
    out   = os.path.join(DOWNLOAD_DIR, fname)
    if os.path.exists(out):
        return out
    return await client.download_media(message, file_name=out)


async def download_url(url: str) -> Optional[str]:
    code, info, _ = await _run(["yt-dlp", "--get-title", "--no-playlist", url])
    title = info.strip().splitlines()[0] if code == 0 else "video"
    out   = os.path.join(DOWNLOAD_DIR, _sanitize(title) + ".mp4")
    if os.path.exists(out):
        return out
    code, _, err = await _run([
        "yt-dlp", "-f", YTDLP_FORMAT,
        "--merge-output-format", "mp4",
        "-o", out, "--no-playlist", url,
    ])
    if code != 0:
        print(f"[yt-dlp] {err}")
        return None
    return out if os.path.exists(out) else None


async def resolve_media(client: Client, message: Message, url: Optional[str] = None):
    """Returns (file_path, title, duration, thumb_path|None)"""
    path = None
    if url and url.startswith(("http://", "https://")):
        path = await download_url(url)
        if not path:
            raise ValueError(f"Could not download: {url}")
    else:
        target = message.reply_to_message or message
        path   = await download_telegram_file(client, target)
        if not path:
            raise ValueError("No downloadable media found. Reply to a video or pass a URL.")

    title    = await probe_title(path)
    duration = await probe_duration(path)
    thumb    = os.path.join(DOWNLOAD_DIR, _sanitize(title) + "_thumb.jpg")
    if not os.path.exists(thumb):
        await extract_thumbnail(path, thumb)

    return path, title, duration, thumb if os.path.exists(thumb) else None


# ═══════════════════════════════════════════════════════════════
#  FFMPEG STREAM BUILDER
# ═══════════════════════════════════════════════════════════════

def build_stream(file_path: str, seek: int = 0, speed: float = 1.0,
                 volume: int = 100) -> AudioVideoPiped:
    vf = "null" if speed == 1.0 else f"setpts={1/speed:.4f}*PTS"
    af_parts = []
    if speed != 1.0:
        af_parts.append(f"atempo={speed:.2f}")
    if volume != 100:
        af_parts.append(f"volume={volume/100:.2f}")
    af = ",".join(af_parts) if af_parts else "anull"

    extra = []
    if seek > 0:
        extra += ["-ss", str(seek)]
    extra += ["-vf", vf, "-af", af]

    return AudioVideoPiped(
        file_path,
        audio_parameters=HighQualityAudio(),
        video_parameters=HighQualityVideo(),
        additional_ffmpeg_parameters=" ".join(extra),
    )


def build_stream_with_subs(file_path: str, srt_path: str, seek: int = 0,
                            speed: float = 1.0, volume: int = 100) -> AudioVideoPiped:
    esc = srt_path.replace("\\", "/").replace(":", "\\:")
    vf  = f"subtitles='{esc}'"
    if speed != 1.0:
        vf += f",setpts={1/speed:.4f}*PTS"
    af_parts = []
    if speed != 1.0:
        af_parts.append(f"atempo={speed:.2f}")
    if volume != 100:
        af_parts.append(f"volume={volume/100:.2f}")
    af = ",".join(af_parts) if af_parts else "anull"

    extra = []
    if seek > 0:
        extra += ["-ss", str(seek)]
    extra += ["-vf", vf, "-af", af]

    return AudioVideoPiped(
        file_path,
        audio_parameters=HighQualityAudio(),
        video_parameters=HighQualityVideo(),
        additional_ffmpeg_parameters=" ".join(extra),
    )


# ═══════════════════════════════════════════════════════════════
#  UI  (inline keyboards)
# ═══════════════════════════════════════════════════════════════

def player_keyboard(paused: bool = False, loop: bool = False) -> InlineKeyboardMarkup:
    pause_label = "▶️ Resume" if paused else "⏸ Pause"
    pause_data  = "resume"   if paused else "pause"
    loop_label  = "🔁 ON"    if loop   else "🔁 OFF"
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("⏮ Restart",  callback_data="replay"),
            InlineKeyboardButton("⏪ -30s",     callback_data="seek_back"),
            InlineKeyboardButton(pause_label,   callback_data=pause_data),
            InlineKeyboardButton("⏩ +30s",     callback_data="seek_fwd"),
            InlineKeyboardButton("⏭ Skip",     callback_data="skip"),
        ],
        [
            InlineKeyboardButton("🔉 -10",      callback_data="vol_down"),
            InlineKeyboardButton("🔊 +10",      callback_data="vol_up"),
            InlineKeyboardButton(loop_label,     callback_data="toggle_loop"),
            InlineKeyboardButton("📋 Queue",    callback_data="queue"),
            InlineKeyboardButton("❌ Stop",     callback_data="stop"),
        ],
        [
            InlineKeyboardButton("0.5x",  callback_data="speed_0.5"),
            InlineKeyboardButton("1x",    callback_data="speed_1.0"),
            InlineKeyboardButton("1.5x",  callback_data="speed_1.5"),
            InlineKeyboardButton("2x",    callback_data="speed_2.0"),
        ],
    ])


def stopped_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("📋 Queue", callback_data="queue"),
    ]])


# ═══════════════════════════════════════════════════════════════
#  PLAYER ENGINE
# ═══════════════════════════════════════════════════════════════

class Player:
    def __init__(self, app: Client):
        self.app  = app
        self.call = PyTgCalls(app)
        self._pos_tasks: dict[int, asyncio.Task] = {}
        self.on_track_change = None
        self.on_queue_empty  = None

        @self.call.on_stream_end()
        async def _on_end(_, update: Update):
            if isinstance(update, (StreamAudioEnded, StreamVideoEnded)):
                await self._on_track_end(update.chat_id)

    async def start(self):
        await self.call.start()

    # ── position tracker ─────────────────────────────────────────

    async def _start_tracker(self, chat_id: int):
        async def _tick():
            while True:
                await asyncio.sleep(1)
                state = get_state(chat_id)
                if state.current and not state.paused:
                    state.position += int(state.speed)
        t = self._pos_tasks.get(chat_id)
        if t and not t.done():
            t.cancel()
        self._pos_tasks[chat_id] = asyncio.create_task(_tick())

    async def _stop_tracker(self, chat_id: int):
        t = self._pos_tasks.pop(chat_id, None)
        if t:
            t.cancel()

    # ── internal streaming ───────────────────────────────────────

    async def _stream(self, chat_id: int, item: MediaItem, seek: int = 0):
        state  = get_state(chat_id)
        stream = build_stream(item.file_path, seek=seek,
                              speed=state.speed, volume=state.volume)
        try:
            await self.call.join_group_call(chat_id, stream, stream_type=None)
        except Exception:
            await self.call.change_stream(chat_id, stream)

        state.current  = item
        state.position = seek
        state.paused   = False
        await self._start_tracker(chat_id)

    async def _on_track_end(self, chat_id: int):
        async with get_lock(chat_id):
            state = get_state(chat_id)
            if state.loop and state.current:
                await self._stream(chat_id, state.current)
                return
            nxt = self._pop_next(chat_id)
            if nxt:
                await self._stream(chat_id, nxt)
                if self.on_track_change:
                    await self.on_track_change(chat_id, nxt)
            else:
                await self._stop_tracker(chat_id)
                state.current  = None
                state.position = 0
                try:
                    await self.call.leave_group_call(chat_id)
                except Exception:
                    pass
                if self.on_queue_empty:
                    await self.on_queue_empty(chat_id)

    def _pop_next(self, chat_id: int) -> Optional[MediaItem]:
        state = get_state(chat_id)
        if not state.queue:
            return None
        if state.shuffle:
            idx = random.randrange(len(state.queue))
            return state.queue.pop(idx)
        return state.queue.pop(0)

    # ── public API ───────────────────────────────────────────────

    async def play(self, chat_id: int, item: MediaItem) -> bool:
        async with get_lock(chat_id):
            try:
                await self._stream(chat_id, item)
                return True
            except Exception as e:
                print(f"[player.play] {e}")
                return False

    async def pause(self, chat_id: int) -> bool:
        state = get_state(chat_id)
        if not state.current or state.paused:
            return False
        try:
            await self.call.pause_stream(chat_id)
            state.paused = True
            return True
        except Exception:
            return False

    async def resume(self, chat_id: int) -> bool:
        state = get_state(chat_id)
        if not state.current or not state.paused:
            return False
        try:
            await self.call.resume_stream(chat_id)
            state.paused = False
            return True
        except Exception:
            return False

    async def stop(self, chat_id: int):
        await self._stop_tracker(chat_id)
        state = get_state(chat_id)
        state.current  = None
        state.position = 0
        state.paused   = False
        state.queue.clear()
        try:
            await self.call.leave_group_call(chat_id)
        except Exception:
            pass

    async def seek(self, chat_id: int, seconds: int) -> bool:
        state = get_state(chat_id)
        if not state.current:
            return False
        async with get_lock(chat_id):
            await self._stream(chat_id, state.current, seek=seconds)
        return True

    async def skip(self, chat_id: int) -> Optional[MediaItem]:
        async with get_lock(chat_id):
            nxt = self._pop_next(chat_id)
            if nxt:
                await self._stream(chat_id, nxt)
                return nxt
            else:
                await self.stop(chat_id)
                return None

    async def set_volume(self, chat_id: int, vol: int) -> bool:
        vol = max(0, min(200, vol))
        state = get_state(chat_id)
        state.volume = vol
        if state.current:
            async with get_lock(chat_id):
                await self._stream(chat_id, state.current, seek=state.position)
        return True

    async def set_speed(self, chat_id: int, speed: float) -> bool:
        speed = max(0.5, min(2.0, round(speed, 1)))
        state = get_state(chat_id)
        state.speed = speed
        if state.current:
            async with get_lock(chat_id):
                await self._stream(chat_id, state.current, seek=state.position)
        return True

    async def replay(self, chat_id: int) -> bool:
        state = get_state(chat_id)
        if not state.current:
            return False
        async with get_lock(chat_id):
            await self._stream(chat_id, state.current, seek=0)
        return True

    async def enable_subtitles(self, chat_id: int, srt_path: str) -> bool:
        state = get_state(chat_id)
        if not state.current:
            return False
        stream = build_stream_with_subs(
            state.current.file_path, srt_path,
            seek=state.position, speed=state.speed, volume=state.volume,
        )
        try:
            await self.call.change_stream(chat_id, stream)
            return True
        except Exception:
            return False

    def now_playing_text(self, chat_id: int) -> str:
        state = get_state(chat_id)
        if not state.current:
            return "⏹ Nothing is playing."
        item = state.current
        pos  = fmt_time(state.position)
        dur  = fmt_time(item.duration)
        bar  = progress_bar(state.position, item.duration)
        icon = "⏸" if state.paused else "▶️"
        spd  = f"  ⚡ {state.speed}x" if state.speed != 1.0 else ""
        loop = "  🔁" if state.loop else ""
        return (
            f"{icon} **{item.title}**\n"
            f"{bar}\n"
            f"⏱ `{pos}` / `{dur}`{spd}  🔊 {state.volume}%{loop}\n"
            f"👤 {item.requested_by}"
        )


# ═══════════════════════════════════════════════════════════════
#  BOT SETUP
# ═══════════════════════════════════════════════════════════════

app    = Client("vc_movie_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)
player = Player(app)


# ── panel helpers ─────────────────────────────────────────────

async def send_panel(chat_id: int, text: str, thumb: Optional[str] = None):
    state = get_state(chat_id)
    kb    = player_keyboard(paused=state.paused, loop=state.loop)
    try:
        if thumb and os.path.exists(thumb):
            sent = await app.send_photo(chat_id, thumb, caption=text, reply_markup=kb)
        else:
            sent = await app.send_message(chat_id, text, reply_markup=kb)
        if state.panel_msg_id:
            try:
                await app.delete_messages(chat_id, state.panel_msg_id)
            except Exception:
                pass
        state.panel_msg_id = sent.id
    except Exception as e:
        print(f"[panel] {e}")


async def refresh_panel(chat_id: int):
    state = get_state(chat_id)
    if not state.panel_msg_id:
        return
    text = player.now_playing_text(chat_id)
    kb   = player_keyboard(paused=state.paused, loop=state.loop)
    try:
        await app.edit_message_caption(chat_id, state.panel_msg_id, caption=text, reply_markup=kb)
    except Exception:
        try:
            await app.edit_message_text(chat_id, state.panel_msg_id, text, reply_markup=kb)
        except Exception:
            pass


# ── player callbacks ──────────────────────────────────────────

async def _on_track_change(chat_id: int, item: MediaItem):
    text = player.now_playing_text(chat_id)
    await send_panel(chat_id, text, item.thumbnail)


async def _on_queue_empty(chat_id: int):
    state = get_state(chat_id)
    if state.panel_msg_id:
        try:
            await app.edit_message_text(
                chat_id, state.panel_msg_id,
                "⏹ Playback finished. Queue is empty.",
                reply_markup=stopped_keyboard(),
            )
        except Exception:
            pass


player.on_track_change = _on_track_change
player.on_queue_empty  = _on_queue_empty


async def _requester(msg: Message) -> str:
    u = msg.from_user
    if not u:
        return "Unknown"
    return u.first_name or u.username or str(u.id)


# ═══════════════════════════════════════════════════════════════
#  COMMANDS
# ═══════════════════════════════════════════════════════════════

@app.on_message(filters.command("start") & filters.group)
async def cmd_start(_, msg: Message):
    await msg.reply(
        "🎬 **VC Movie Bot — Commands**\n\n"
        "`/play [url]` — Reply to video or give URL\n"
        "`/pause` `/resume` `/stop` `/skip` `/replay`\n"
        "`/seek 1:23:45` — Jump to timestamp\n"
        "`/speed 1.5` — Playback speed (0.5–2.0)\n"
        "`/volume 80` — Volume (0–200)\n"
        "`/loop` — Toggle loop mode\n"
        "`/shuffle` — Toggle shuffle mode\n"
        "`/queue` — Show queue\n"
        "`/remove 2` — Remove item from queue\n"
        "`/clearqueue` — Clear entire queue\n"
        "`/subtitle` — Reply to .srt file\n"
        "`/status` — Refresh now-playing panel\n"
    )


@app.on_message(filters.command("play") & filters.group)
async def cmd_play(_, msg: Message):
    parts = msg.text.split(None, 1)
    url   = parts[1].strip() if len(parts) > 1 else None

    status = await msg.reply("⏳ Fetching media…")
    try:
        path, title, duration, thumb = await resolve_media(app, msg, url)
    except ValueError as e:
        await status.edit(f"❌ {e}")
        return

    item = MediaItem(
        title=title, file_path=path, duration=duration,
        requested_by=await _requester(msg), thumbnail=thumb,
    )
    state = get_state(msg.chat.id)

    if state.current:
        if len(state.queue) >= MAX_QUEUE:
            await status.edit("❌ Queue is full.")
            return
        state.queue.append(item)
        await status.edit(
            f"📋 Added to queue: **{title}**\n"
            f"Position: #{len(state.queue)}"
        )
        return

    ok = await player.play(msg.chat.id, item)
    if ok:
        await status.delete()
        await send_panel(msg.chat.id, player.now_playing_text(msg.chat.id), thumb)
    else:
        await status.edit("❌ Failed to stream. Is a Voice Chat active in this group?")


@app.on_message(filters.command("pause") & filters.group)
async def cmd_pause(_, msg: Message):
    if await player.pause(msg.chat.id):
        await msg.reply("⏸ Paused.")
        await refresh_panel(msg.chat.id)
    else:
        await msg.reply("Nothing to pause.")


@app.on_message(filters.command("resume") & filters.group)
async def cmd_resume(_, msg: Message):
    if await player.resume(msg.chat.id):
        await msg.reply("▶️ Resumed.")
        await refresh_panel(msg.chat.id)
    else:
        await msg.reply("Nothing to resume.")


@app.on_message(filters.command("stop") & filters.group)
async def cmd_stop(_, msg: Message):
    await player.stop(msg.chat.id)
    await msg.reply("⏹ Stopped and left the voice chat.")


@app.on_message(filters.command("skip") & filters.group)
async def cmd_skip(_, msg: Message):
    nxt = await player.skip(msg.chat.id)
    if nxt:
        await msg.reply(f"⏭ Skipped. Now playing: **{nxt.title}**")
        await refresh_panel(msg.chat.id)
    else:
        await msg.reply("⏭ Skipped. Queue is empty.")


@app.on_message(filters.command("replay") & filters.group)
async def cmd_replay(_, msg: Message):
    if await player.replay(msg.chat.id):
        await msg.reply("⏮ Restarting from beginning.")
        await refresh_panel(msg.chat.id)
    else:
        await msg.reply("Nothing is playing.")


@app.on_message(filters.command("seek") & filters.group)
async def cmd_seek(_, msg: Message):
    parts = msg.text.split(None, 1)
    if len(parts) < 2:
        await msg.reply("Usage: `/seek 1:23` or `/seek 90`")
        return
    secs = parse_time(parts[1])
    if secs is None:
        await msg.reply("❌ Invalid format. Use `MM:SS`, `HH:MM:SS`, or plain seconds.")
        return
    if await player.seek(msg.chat.id, secs):
        await msg.reply(f"⏩ Seeked to `{fmt_time(secs)}`.")
        await refresh_panel(msg.chat.id)
    else:
        await msg.reply("Nothing is playing.")


@app.on_message(filters.command("speed") & filters.group)
async def cmd_speed(_, msg: Message):
    parts = msg.text.split()
    if len(parts) < 2:
        await msg.reply("Usage: `/speed 1.5`  (0.5 – 2.0)")
        return
    try:
        spd = float(parts[1])
    except ValueError:
        await msg.reply("❌ Invalid speed value.")
        return
    await player.set_speed(msg.chat.id, spd)
    state = get_state(msg.chat.id)
    await msg.reply(f"⚡ Speed set to `{state.speed}x`.")
    await refresh_panel(msg.chat.id)


@app.on_message(filters.command("volume") & filters.group)
async def cmd_volume(_, msg: Message):
    parts = msg.text.split()
    if len(parts) < 2:
        state = get_state(msg.chat.id)
        await msg.reply(f"🔊 Current volume: `{state.volume}%`\nUsage: `/volume 80`")
        return
    try:
        vol = int(parts[1])
    except ValueError:
        await msg.reply("❌ Invalid value.")
        return
    await player.set_volume(msg.chat.id, vol)
    state = get_state(msg.chat.id)
    await msg.reply(f"🔊 Volume set to `{state.volume}%`.")
    await refresh_panel(msg.chat.id)


@app.on_message(filters.command("loop") & filters.group)
async def cmd_loop(_, msg: Message):
    state = get_state(msg.chat.id)
    state.loop = not state.loop
    await msg.reply(f"🔁 Loop {'enabled' if state.loop else 'disabled'}.")
    await refresh_panel(msg.chat.id)


@app.on_message(filters.command("shuffle") & filters.group)
async def cmd_shuffle(_, msg: Message):
    state = get_state(msg.chat.id)
    state.shuffle = not state.shuffle
    await msg.reply(f"🔀 Shuffle {'enabled' if state.shuffle else 'disabled'}.")


@app.on_message(filters.command("queue") & filters.group)
async def cmd_queue(_, msg: Message):
    await msg.reply(format_queue(msg.chat.id))


@app.on_message(filters.command("remove") & filters.group)
async def cmd_remove(_, msg: Message):
    parts = msg.text.split()
    if len(parts) < 2:
        await msg.reply("Usage: `/remove 2` (1-based index)")
        return
    try:
        idx = int(parts[1])
    except ValueError:
        await msg.reply("❌ Invalid index.")
        return
    state = get_state(msg.chat.id)
    real  = idx - 1
    if 0 <= real < len(state.queue):
        removed = state.queue.pop(real)
        await msg.reply(f"🗑 Removed: **{removed.title}**")
    else:
        await msg.reply("❌ No item at that position.")


@app.on_message(filters.command("clearqueue") & filters.group)
async def cmd_clearqueue(_, msg: Message):
    get_state(msg.chat.id).queue.clear()
    await msg.reply("🗑 Queue cleared.")


@app.on_message(filters.command("subtitle") & filters.group)
async def cmd_subtitle(_, msg: Message):
    target = msg.reply_to_message
    if not target or not target.document:
        await msg.reply("Reply to an .srt subtitle file with /subtitle")
        return
    if not target.document.file_name.endswith(".srt"):
        await msg.reply("❌ Only .srt files are supported.")
        return
    status   = await msg.reply("⏳ Downloading subtitle…")
    srt_path = await app.download_media(
        target, file_name=f"{DOWNLOAD_DIR}/{target.document.file_id}.srt"
    )
    if await player.enable_subtitles(msg.chat.id, srt_path):
        await status.edit("📝 Subtitles enabled.")
    else:
        await status.edit("❌ Failed. Is something currently playing?")


@app.on_message(filters.command("status") & filters.group)
async def cmd_status(_, msg: Message):
    state = get_state(msg.chat.id)
    thumb = state.current.thumbnail if state.current else None
    await send_panel(msg.chat.id, player.now_playing_text(msg.chat.id), thumb)


# ═══════════════════════════════════════════════════════════════
#  CALLBACK QUERIES  (inline button presses)
# ═══════════════════════════════════════════════════════════════

@app.on_callback_query()
async def on_button(_, query: CallbackQuery):
    chat_id = query.message.chat.id
    data    = query.data
    state   = get_state(chat_id)

    await query.answer()

    if data == "pause":
        await player.pause(chat_id)
    elif data == "resume":
        await player.resume(chat_id)
    elif data == "stop":
        await player.stop(chat_id)
        try:
            await query.message.edit_text("⏹ Stopped.", reply_markup=stopped_keyboard())
        except Exception:
            pass
        return
    elif data == "replay":
        await player.replay(chat_id)
    elif data == "skip":
        nxt = await player.skip(chat_id)
        if not nxt:
            try:
                await query.message.edit_text("⏹ Queue empty.", reply_markup=stopped_keyboard())
            except Exception:
                pass
            return
    elif data == "seek_back":
        await player.seek(chat_id, max(0, state.position - 30))
    elif data == "seek_fwd":
        await player.seek(chat_id, state.position + 30)
    elif data == "vol_down":
        await player.set_volume(chat_id, state.volume - 10)
    elif data == "vol_up":
        await player.set_volume(chat_id, state.volume + 10)
    elif data == "toggle_loop":
        state.loop = not state.loop
    elif data == "queue":
        await query.answer(format_queue(chat_id)[:200], show_alert=True)
        return
    elif data.startswith("speed_"):
        await player.set_speed(chat_id, float(data.split("_", 1)[1]))

    # Refresh panel
    text = player.now_playing_text(chat_id)
    kb   = player_keyboard(paused=state.paused, loop=state.loop)
    try:
        await query.message.edit_caption(caption=text, reply_markup=kb)
    except Exception:
        try:
            await query.message.edit_text(text, reply_markup=kb)
        except Exception:
            pass


# ═══════════════════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════════════════

async def main():
    await player.start()
    await app.start()
    print("✅ VC Movie Bot is running. Press Ctrl+C to stop.")
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
