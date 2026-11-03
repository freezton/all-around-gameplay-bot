# all-around-bot

A Discord bot that archives one user's messages and reposts random ones as
Discord-style "screenshot" images.

## Current features

- **`/collect [user]`** — backfills every readable text channel (admin-only).
  With no options it imports **everyone's** messages; with `user:` only that person's.
- **Live capture** — new messages are stored as they arrive (or only
  `TARGET_USER_ID`'s, if that is set).
- **`/ping [user] [reply_original]`** — picks a random archived message and uploads
  it as a rendered PNG that looks like a Discord message. Pass `user:` to only use
  that person's messages; omit it to pick from the whole archive. Set
  `reply_original:true` to post the card as a reply to the archived message (or with
  a jump link when the original is in another channel) so you can verify it.
  Includes colour emoji (Twemoji assets and real custom server emoji).
- **`/stats [user]`** — shows how many messages (and users) are archived.
- **`/play [link, playlist, or search] [stay_after_end]`** — joins your voice
  channel and streams the audio. Playlist links enqueue every video (up to 100).
  If the bot is already playing it **queues** the track instead of interrupting.
  While a session is playing, `/play` only works from the bot's current voice
  channel and from the text channel where the session started; calls from
  anywhere else are rejected, so the bot cannot be dragged away or hijacked.
  After `/stop`, `/play` rejoins your voice channel, continues the saved track
  from its timestamp, and appends the new request to the end of the saved queue.
  Run `/play` **without** a query to resume a stopped session, or to make the bot
  join your channel and wait silently for tracks — it stays until `/stop` while
  the queue is empty. When the queue ends it disconnects unless
  `stay_after_end:true`.
- **`/queue`** — prints the current track, who requested it, and the upcoming
  tracks. After `/stop` it shows the saved track and timestamp.
- **`/jump <index>`** — jumps straight to the given position in the upcoming
  queue (1 = next), discarding the tracks before it.
- **`/clearqueue`** — removes every upcoming track, keeping the current one.
  After `/stop` it discards the saved track, timestamp, and queue entirely.
- **`/skip`** — skips the current track. With an empty queue it leaves (unless
  `stay_after_end` is set).
- **`/stop`** — stops playback and leaves the voice channel but **keeps the
  queue and the current track's position** so `/play` can resume. While a
  session is playing it obeys the same voice-channel/text-channel rules as
  `/play`.
- **Now-playing message** — the bot keeps one player message per session with
  ⏮️ previous, ⏸️/▶️ pause/resume, and ⏭️ next buttons. Every control (buttons,
  `/skip`, `/jump`, `/clearqueue`, `/stop`) must come from the session's voice
  channel **and** its text channel.
- **`/voice [test_tone:true]`** — prints voice diagnostics (encryption/DAVE state,
  mute flags, latency) or plays a 3-second test tone.

Messages are stored in SQLite (`data/bot.db` by default). Only messages with text
are archived — attachments, embeds, and reactions are ignored for now.

## Setup

Requires Python 3.13+ and [uv](https://docs.astral.sh/uv/). Voice also needs
**FFmpeg** and **libopus** on the host:

```bash
brew install ffmpeg opus        # macOS
sudo apt install ffmpeg libopus0 # Debian/Ubuntu
```

```bash
uv sync
cp .env.example .env
```

### 1. Create the Discord application

1. Go to the [Developer Portal](https://discord.com/developers/applications) → **New Application**.
2. **Bot** tab → **Reset Token** → copy the token into `DISCORD_TOKEN`.
3. On the same page, enable **Message Content Intent** under *Privileged Gateway Intents*.
   Without it, `message.content` is always empty and nothing gets archived.
4. **OAuth2 → URL Generator**: scope `bot` + `applications.commands`, permissions
   `Read Message History`, `View Channels`, `Send Messages`, `Attach Files`,
   `Connect`, `Speak`. Invite the bot with the generated URL.

### Private channels

Give the bot a dedicated role, then allow that role **View Channel** and
**Read Message History** on the private channel (and on its category, if the
channel syncs permissions from one). No extra intent is required — role-based
access is enough, and `/collect` includes private channels it can see.

### 2. Fill in the IDs

Enable **Settings → Advanced → Developer Mode** in Discord, then:

- Right-click the target user → **Copy User ID** → `TARGET_USER_ID` (optional;
  leave empty to archive everybody).
- Optionally right-click the server → **Copy Server ID** → `TARGET_GUILD_ID`
  (recommended: keeps the archive to one server).
- For fast command updates while developing, set `DEV_GUILD_ID` to the same server ID.
  Global commands can take up to an hour to appear; guild commands are instant.

### 3. Run

```bash
uv run all-around-bot
```

Then in the server: `/collect` to import history (progress is reported on the
ephemeral reply), and `/ping` to see a random message.

## How it works

```
src/all_around_bot/
├── __main__.py     entry point
├── bot.py          intents, extension loading, slash command sync, lifecycle
├── config.py       pydantic-settings config from .env
├── db.py           SQLite store (aiosqlite), dedupe by message ID
├── render.py       Pillow renderer for the "screenshot" card
├── text.py         Discord markup cleanup, word wrapping, timestamps
└── cogs/
    ├── capture.py  live listener + /collect + /stats
    ├── fun.py      /ping
    └── voice.py    /play, /queue, /jump, /clearqueue, /skip, /stop, /voice
```

The renderer draws a message card at 2× scale (crisp on high-DPI displays) using
the first available system font (Arial on macOS, DejaVu on Linux, Segoe on
Windows). The real avatar is fetched and cached; if that fails, a colored circle
with the user's initial is used.

## Development

```bash
uv run pytest
uv run ruff check .
uv run ruff format .
```

## Known limitations

- `/collect` walks full channel history; on very large servers it can exceed
  Discord's 15-minute interaction window. Re-running is safe — inserts are deduped.
- Emoji assets are fetched on first use (Twemoji from jsDelivr, custom emoji from
  `cdn.discordapp.com`) and cached in `data/emoji/`. If a download fails, that
  emoji falls back to its text form (`:name:`).
- Animated custom emoji render as their first frame.
- Voice is one queue per server (one voice connection). The queue, the saved
  `/stop` position, and the playback buttons live in memory: restarting the bot
  loses them, and the buttons on old player messages stop working. There is no
  seek command; resuming after `/stop` is the only way to start mid-track.
- `davey` is pinned below 0.2.0 on purpose. discord.py 2.7.x calls the old DAVE
  API; with 0.2.0 the encryption session never becomes ready and Discord drops
  the bot's audio (it looks like it is speaking but nobody hears it).
- YouTube occasionally blocks yt-dlp ("confirm you're not a bot"); refreshing
  `yt-dlp` or supplying cookies fixes it. Streaming YouTube audio may conflict
  with YouTube's terms of service — that's your call.
- Active threads are scanned; archived threads and forum posts are not.
- Message edits and deletions are not synced after capture.
