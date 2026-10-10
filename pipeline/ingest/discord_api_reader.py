"""Fetch course Discord messages via the Discord REST API using a bot token.

Requires DISCORD_TOKEN in .env. The bot must be a member of the course server.
Relevance is decided as in discord_reader (course_keywords, from courses.yaml),
and the output, one JSON list per server, goes to the folder the caller names
(``pipeline discord-fetch`` passes knowledge-base/raw/forum); each message
carries a ``text`` field, the shape the forum collection is indexed from.

Endpoints used:
  GET /users/@me/guilds          -- list servers the bot is in
  GET /guilds/{id}/channels      -- list channels
  GET /channels/{id}/messages    -- fetch messages (max 100 per call)
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from pipeline.ingest.discord_reader import course_keywords, relevance_pattern

DISCORD_API = "https://discord.com/api/v10"
# Discord asks bots for "DiscordBot (url, version)"; any project URL will do.
USER_AGENT = "DiscordBot (https://github.com/, 1) llm-tutor"

COURSE_KEYWORDS = course_keywords()

# Channel types to skip
SKIP_CHANNEL_TYPES = {2, 4, 13, 14, 15}  # voice, category, stage, directory, forum


@dataclass
class DiscordMsg:
    id: str
    author: str
    author_id: str
    timestamp: str
    content: str
    guild_name: str
    channel_name: str
    is_bot: bool = False


@dataclass
class DiscordGuild:
    id: str
    name: str
    channels: list[dict] = field(default_factory=list)


def _token() -> str:
    token = os.environ.get("DISCORD_TOKEN", "")
    if not token:
        from dotenv import load_dotenv
        load_dotenv()
        token = os.environ.get("DISCORD_TOKEN", "")
    if not token:
        raise ValueError("DISCORD_TOKEN not set in .env")
    return token


def _get(endpoint: str) -> dict | list:
    token = _token()
    req = Request(
        f"{DISCORD_API}{endpoint}",
        headers={
            "Authorization": f"Bot {token}",
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        },
    )
    with urlopen(req) as resp:
        return json.loads(resp.read().decode())


def list_guilds() -> list[DiscordGuild]:
    data = _get("/users/@me/guilds")
    return [DiscordGuild(id=g["id"], name=g["name"]) for g in data]


def list_text_channels(guild_id: str) -> list[dict]:
    channels = _get(f"/guilds/{guild_id}/channels")
    return [
        c for c in channels
        if c.get("type") not in SKIP_CHANNEL_TYPES
        and c.get("type") == 0  # GUILD_TEXT only
    ]


def fetch_channel_messages(
    channel_id: str,
    guild_name: str,
    channel_name: str,
    limit: int = 100,
    filter_relevant: bool = True,
) -> list[DiscordMsg]:
    all_msgs: list[DiscordMsg] = []
    before: str | None = None

    while len(all_msgs) < limit:
        batch_size = min(100, limit - len(all_msgs))
        endpoint = f"/channels/{channel_id}/messages?limit={batch_size}"
        if before:
            endpoint += f"&before={before}"
        try:
            data = _get(endpoint)
        except HTTPError as e:
            if e.code == 403:
                break  # no permission for this channel
            raise
        if not data:
            break
        for m in data:
            content = m.get("content", "").strip()
            if not content:
                continue
            all_msgs.append(DiscordMsg(
                id=m["id"],
                author=m["author"].get("username", ""),
                author_id=m["author"].get("id", ""),
                timestamp=m.get("timestamp", ""),
                content=content,
                guild_name=guild_name,
                channel_name=channel_name,
                is_bot=m["author"].get("bot", False),
            ))
        before = data[-1]["id"]
        time.sleep(0.5)  # rate limit courtesy sleep

    if filter_relevant:
        pattern = relevance_pattern(COURSE_KEYWORDS)
        all_msgs = [m for m in all_msgs if pattern.search(m.content)]

    return all_msgs


def fetch_all_guilds(
    output_dir: Path,
    messages_per_channel: int = 200,
    filter_relevant: bool = True,
    verbose: bool = True,
) -> list[Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []

    try:
        guilds = list_guilds()
    except Exception as e:
        print(f"[discord-api] Could not list guilds: {e}")
        print("[discord-api] Make sure the bot is added to the server and DISCORD_TOKEN is correct.")
        return []

    if verbose:
        print(f"[discord-api] Bot is in {len(guilds)} server(s): {[g.name for g in guilds]}")

    for guild in guilds:
        try:
            channels = list_text_channels(guild.id)
        except Exception as e:
            print(f"[discord-api] Could not list channels for {guild.name}: {e}")
            continue

        if verbose:
            print(f"[discord-api] {guild.name}: {len(channels)} text channels")

        all_msgs: list[DiscordMsg] = []
        for ch in channels:
            ch_name = ch.get("name", ch["id"])
            try:
                msgs = fetch_channel_messages(
                    channel_id=ch["id"],
                    guild_name=guild.name,
                    channel_name=ch_name,
                    limit=messages_per_channel,
                    filter_relevant=filter_relevant,
                )
                all_msgs.extend(msgs)
                if verbose and msgs:
                    print(f"  #{ch_name}: {len(msgs)} relevant messages")
            except HTTPError as e:
                if verbose:
                    print(f"  #{ch_name}: skipped (HTTP {e.code})")

        if all_msgs:
            out_path = output_dir / f"discord-{guild.name.replace(' ', '_')}.json"
            rows = [{**asdict(m), "text": f"[{m.author}] {m.content}", "source_file": out_path.name}
                    for m in all_msgs]
            out_path.write_text(json.dumps(rows, indent=2))
            written.append(out_path)
            if verbose:
                print(f"[discord-api] Saved {len(all_msgs)} messages -> {out_path.name}")

    return written
