import argparse
import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

try:
    from dotenv import load_dotenv
except Exception:
    def load_dotenv(*_args, **_kwargs):
        return False


ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from db import connect, transaction
from settings import DATA_DIR, DATABASE_PATH


DISCORD_API_BASE = "https://discord.com/api/v10"
DEFAULT_USER_AGENT = "tournament-hub-discord-sync/1.0"
FIELD_RE = re.compile(r"^([A-Za-z][A-Za-z _-]{1,40}):\s*(.*)$")


def now_iso_utc():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def load_env_file_fallback(path):
    env_path = Path(path)
    if not env_path.exists() or not env_path.is_file():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key or key in os.environ:
            continue
        os.environ[key] = value.strip()


def parse_args():
    parser = argparse.ArgumentParser(
        description="Sync 2048Verse Game Review channel messages from Discord into tournament_hub.sqlite3"
    )
    parser.add_argument(
        "--channel-id",
        action="append",
        help="Discord channel id to sync (can repeat). If omitted, read DISCORD_REVIEW_CHANNEL_IDS",
    )
    parser.add_argument("--max-pages", type=int, default=10, help="Max pages per channel, each page up to 100 messages")
    parser.add_argument("--limit", type=int, default=100, help="Discord messages per page (1~100)")
    parser.add_argument("--full-scan", action="store_true", help="Ignore saved cursor and rescan recent history")
    parser.add_argument("--download-files", action="store_true", help="Download message attachments (e.g. .vrs)")
    parser.add_argument(
        "--download-only-username",
        action="append",
        help="Only download attachments for matched parsed username (can repeat)",
    )
    parser.add_argument("--dry-run", action="store_true", help="Fetch and parse without writing DB")
    return parser.parse_args()


def parse_channel_ids(cli_values):
    values = []
    if cli_values:
        values.extend(cli_values)
    env_value = os.getenv("DISCORD_REVIEW_CHANNEL_IDS", "")
    if env_value.strip():
        values.extend([part.strip() for part in env_value.split(",") if part.strip()])
    unique = []
    seen = set()
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        unique.append(value)
    return unique


def snowflake_int(value):
    try:
        return int(str(value))
    except Exception:
        return -1


def request_json(token, method, path, query=None, timeout=20):
    url = DISCORD_API_BASE + path
    if query:
        url += "?" + urlencode(query)
    request = Request(
        url=url,
        method=method,
        headers={
            "Authorization": "Bot " + token,
            "User-Agent": DEFAULT_USER_AGENT,
            "Accept": "application/json",
        },
    )
    while True:
        try:
            with urlopen(request, timeout=timeout) as response:
                payload = response.read().decode("utf-8", errors="replace")
            if not payload:
                return None
            return json.loads(payload)
        except HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            if exc.code == 429:
                retry_after = 1.0
                try:
                    info = json.loads(body) if body else {}
                    retry_after = float(info.get("retry_after") or 1.0)
                except Exception:
                    retry_after = 1.0
                time.sleep(max(0.5, retry_after))
                continue
            raise RuntimeError("Discord API error {} {}: {}".format(exc.code, path, body))
        except URLError as exc:
            raise RuntimeError("Discord API network error: {}".format(exc))


def request_binary(token, url, timeout=30):
    request = Request(
        url=url,
        method="GET",
        headers={
            "Authorization": "Bot " + token,
            "User-Agent": DEFAULT_USER_AGENT,
        },
    )
    while True:
        try:
            with urlopen(request, timeout=timeout) as response:
                return response.read()
        except HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            if exc.code == 429:
                retry_after = 1.0
                try:
                    info = json.loads(body) if body else {}
                    retry_after = float(info.get("retry_after") or 1.0)
                except Exception:
                    retry_after = 1.0
                time.sleep(max(0.5, retry_after))
                continue
            raise RuntimeError("Discord file download error {}: {}".format(exc.code, body))
        except URLError as exc:
            raise RuntimeError("Discord file download network error: {}".format(exc))


def ensure_tables(connection):
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS discord_review_messages (
            id INTEGER PRIMARY KEY,
            guild_id TEXT,
            channel_id TEXT NOT NULL,
            message_id TEXT NOT NULL,
            author_id TEXT,
            author_name TEXT,
            created_at_discord TEXT,
            content_text TEXT,
            message_kind TEXT NOT NULL,
            parsed_date_text TEXT,
            parsed_score INTEGER,
            parsed_username TEXT,
            parsed_user_id TEXT,
            parsed_board_size TEXT,
            parsed_best_tile INTEGER,
            parsed_flags TEXT,
            metadata_json TEXT,
            raw_json TEXT NOT NULL,
            ingested_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(channel_id, message_id)
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS discord_review_attachments (
            id INTEGER PRIMARY KEY,
            channel_id TEXT NOT NULL,
            message_id TEXT NOT NULL,
            attachment_id TEXT NOT NULL,
            filename TEXT,
            content_type TEXT,
            size_bytes INTEGER,
            url TEXT,
            proxy_url TEXT,
            local_path TEXT,
            sha256 TEXT,
            metadata_json TEXT,
            ingested_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(channel_id, message_id, attachment_id)
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS discord_review_sync_state (
            channel_id TEXT PRIMARY KEY,
            last_message_id TEXT,
            updated_at TEXT NOT NULL
        )
        """
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_discord_review_messages_channel_id ON discord_review_messages(channel_id)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_discord_review_messages_created ON discord_review_messages(created_at_discord)"
    )


def load_last_message_id(connection, channel_id):
    row = connection.execute(
        "SELECT last_message_id FROM discord_review_sync_state WHERE channel_id = ?",
        (channel_id,),
    ).fetchone()
    if row is None:
        return None
    return row["last_message_id"]


def save_last_message_id(connection, channel_id, message_id):
    now = now_iso_utc()
    connection.execute(
        """
        INSERT INTO discord_review_sync_state(channel_id, last_message_id, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(channel_id) DO UPDATE SET
            last_message_id = excluded.last_message_id,
            updated_at = excluded.updated_at
        """,
        (channel_id, str(message_id), now),
    )


def fetch_newer_messages(token, channel_id, *, last_seen_id, max_pages, limit):
    collected = []
    before_id = None
    stop = False
    for _ in range(max_pages):
        query = {"limit": max(1, min(100, int(limit)))}
        if before_id:
            query["before"] = before_id
        page = request_json(token, "GET", "/channels/{}/messages".format(channel_id), query=query)
        if not page:
            break
        if not isinstance(page, list):
            raise RuntimeError("Unexpected Discord payload for channel {}: {}".format(channel_id, page))
        for message in page:
            message_id = str(message.get("id") or "")
            if not message_id:
                continue
            if last_seen_id and snowflake_int(message_id) <= snowflake_int(last_seen_id):
                stop = True
                continue
            collected.append(message)
        before_id = str(page[-1].get("id") or "")
        if stop:
            break
    unique = {}
    for message in collected:
        message_id = str(message.get("id") or "")
        if message_id:
            unique[message_id] = message
    ordered = list(unique.values())
    ordered.sort(key=lambda item: snowflake_int(item.get("id")))
    return ordered


def _clean_text(value):
    return str(value or "").replace("\r\n", "\n").replace("\r", "\n").strip()


def build_message_text(message):
    parts = []
    content = _clean_text(message.get("content"))
    if content:
        parts.append(content)
    for embed in message.get("embeds") or []:
        if not isinstance(embed, dict):
            continue
        title = _clean_text(embed.get("title"))
        description = _clean_text(embed.get("description"))
        if title:
            parts.append(title)
        if description:
            parts.append(description)

        author = embed.get("author")
        if isinstance(author, dict):
            author_name = _clean_text(author.get("name"))
            if author_name:
                parts.append(author_name)

        for field in embed.get("fields") or []:
            if not isinstance(field, dict):
                continue
            field_name = _clean_text(field.get("name"))
            field_value = _clean_text(field.get("value"))
            if field_name and field_value:
                parts.append("{}: {}".format(field_name, field_value))
            elif field_value:
                parts.append(field_value)

        footer = embed.get("footer")
        if isinstance(footer, dict):
            footer_text = _clean_text(footer.get("text"))
            if footer_text:
                parts.append(footer_text)

    compact = []
    for part in parts:
        if not part:
            continue
        if compact and compact[-1] == part:
            continue
        compact.append(part)
    return "\n".join(compact)


def is_redacted_like_message(message, message_text):
    if _clean_text(message_text):
        return False
    if message.get("attachments"):
        return False
    if message.get("embeds"):
        return False
    if message.get("components"):
        return False
    return True


def parse_review_content(content):
    text = (content or "").strip()
    lower = text.lower()
    parsed = {
        "parsed_date_text": None,
        "parsed_score": None,
        "parsed_username": None,
        "parsed_user_id": None,
        "parsed_board_size": None,
        "parsed_best_tile": None,
        "parsed_flags": None,
    }
    if not text:
        return "unknown", parsed

    has_admin_log = "admin log" in lower
    has_relog = "flags:" in lower and "relog" in lower
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    for line in lines:
        match = FIELD_RE.match(line)
        if not match:
            continue
        key = match.group(1).strip().lower().replace(" ", "_")
        value = match.group(2).strip()
        if key == "date":
            parsed["parsed_date_text"] = value
        elif key == "score":
            parsed["parsed_score"] = parse_optional_int(value)
        elif key == "username":
            parsed["parsed_username"] = value
        elif key == "user_id":
            parsed["parsed_user_id"] = value
        elif key == "board_size":
            parsed["parsed_board_size"] = value
        elif key == "best_tile":
            parsed["parsed_best_tile"] = parse_optional_int(value)
        elif key == "flags":
            parsed["parsed_flags"] = value
        elif key == "user_id:":
            parsed["parsed_user_id"] = value

    if has_admin_log or has_relog:
        return "admin_relog", parsed
    if parsed["parsed_score"] is not None and parsed["parsed_username"]:
        return "normal_score", parsed
    return "unknown", parsed


def parse_optional_int(value):
    try:
        return int(str(value).replace(",", "").strip())
    except Exception:
        return None


def upsert_message(connection, channel_id, message, message_text, message_kind, parsed):
    now = now_iso_utc()
    author = message.get("author") or {}
    attachments = message.get("attachments") or []
    embeds = message.get("embeds") or []
    components = message.get("components") or []
    connection.execute(
        """
        INSERT INTO discord_review_messages (
            guild_id, channel_id, message_id, author_id, author_name, created_at_discord,
            content_text, message_kind, parsed_date_text, parsed_score, parsed_username,
            parsed_user_id, parsed_board_size, parsed_best_tile, parsed_flags,
            metadata_json, raw_json, ingested_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(channel_id, message_id) DO UPDATE SET
            author_id = excluded.author_id,
            author_name = excluded.author_name,
            created_at_discord = excluded.created_at_discord,
            content_text = excluded.content_text,
            message_kind = excluded.message_kind,
            parsed_date_text = excluded.parsed_date_text,
            parsed_score = excluded.parsed_score,
            parsed_username = excluded.parsed_username,
            parsed_user_id = excluded.parsed_user_id,
            parsed_board_size = excluded.parsed_board_size,
            parsed_best_tile = excluded.parsed_best_tile,
            parsed_flags = excluded.parsed_flags,
            metadata_json = excluded.metadata_json,
            raw_json = excluded.raw_json,
            updated_at = excluded.updated_at
        """,
        (
            str(message.get("guild_id") or ""),
            channel_id,
            str(message.get("id") or ""),
            str(author.get("id") or ""),
            str(author.get("username") or ""),
            str(message.get("timestamp") or ""),
            str(message_text or ""),
            message_kind,
            parsed.get("parsed_date_text"),
            parsed.get("parsed_score"),
            parsed.get("parsed_username"),
            parsed.get("parsed_user_id"),
            parsed.get("parsed_board_size"),
            parsed.get("parsed_best_tile"),
            parsed.get("parsed_flags"),
            json.dumps(
                {
                    "edited_timestamp": message.get("edited_timestamp"),
                    "attachments_count": len(attachments),
                    "embeds_count": len(embeds),
                    "components_count": len(components),
                    "author_bot": bool(author.get("bot")),
                    "redacted_like": is_redacted_like_message(message, message_text),
                },
                ensure_ascii=False,
            ),
            json.dumps(message, ensure_ascii=False),
            now,
            now,
        ),
    )


def build_attachment_path(channel_id, message_id, filename):
    safe_name = filename.replace("\\", "_").replace("/", "_")
    base = DATA_DIR / "discord_review_files" / str(channel_id) / str(message_id)
    base.mkdir(parents=True, exist_ok=True)
    return (base / safe_name).resolve()


def sha256_bytes(content_bytes):
    digest = hashlib.sha256()
    digest.update(content_bytes)
    return digest.hexdigest()


def upsert_attachment(connection, channel_id, message_id, attachment, *, local_path=None, sha256=None):
    now = now_iso_utc()
    connection.execute(
        """
        INSERT INTO discord_review_attachments (
            channel_id, message_id, attachment_id, filename, content_type, size_bytes,
            url, proxy_url, local_path, sha256, metadata_json, ingested_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(channel_id, message_id, attachment_id) DO UPDATE SET
            filename = excluded.filename,
            content_type = excluded.content_type,
            size_bytes = excluded.size_bytes,
            url = excluded.url,
            proxy_url = excluded.proxy_url,
            local_path = COALESCE(excluded.local_path, discord_review_attachments.local_path),
            sha256 = COALESCE(excluded.sha256, discord_review_attachments.sha256),
            metadata_json = excluded.metadata_json,
            updated_at = excluded.updated_at
        """,
        (
            channel_id,
            message_id,
            str(attachment.get("id") or ""),
            str(attachment.get("filename") or ""),
            str(attachment.get("content_type") or ""),
            parse_optional_int(attachment.get("size")),
            str(attachment.get("url") or ""),
            str(attachment.get("proxy_url") or ""),
            str(local_path) if local_path else None,
            sha256,
            json.dumps(
                {
                    "ephemeral": bool(attachment.get("ephemeral")),
                    "description": attachment.get("description"),
                },
                ensure_ascii=False,
            ),
            now,
            now,
        ),
    )


def download_attachment_if_needed(token, channel_id, message_id, attachment):
    url = str(attachment.get("url") or "")
    filename = str(attachment.get("filename") or "")
    if not url or not filename:
        return None, None
    local_path = build_attachment_path(channel_id, message_id, filename)
    content_bytes = request_binary(token, url)
    local_path.write_bytes(content_bytes)
    return local_path, sha256_bytes(content_bytes)


def sync_channel(
    connection,
    *,
    token,
    channel_id,
    max_pages,
    limit,
    full_scan,
    download_files,
    dry_run,
    download_only_usernames=None,
):
    last_seen_id = None if full_scan else load_last_message_id(connection, channel_id)
    messages = fetch_newer_messages(
        token,
        channel_id,
        last_seen_id=last_seen_id,
        max_pages=max_pages,
        limit=limit,
    )
    if not messages:
        return {"channel_id": channel_id, "fetched": 0, "inserted_or_updated": 0, "downloaded": 0}

    inserted_or_updated = 0
    downloaded = 0
    newest_id = None
    username_filter = {str(x).strip().lower() for x in (download_only_usernames or []) if str(x).strip()}
    for message in messages:
        message_id = str(message.get("id") or "")
        if not message_id:
            continue
        message_text = build_message_text(message)
        message_kind, parsed = parse_review_content(message_text)
        attachments = message.get("attachments") or []
        parsed_username = str(parsed.get("parsed_username") or "").strip().lower()
        attachment_name_match = any(
            str(attachment.get("filename") or "").strip().lower().startswith(username + "_")
            for attachment in attachments
            for username in username_filter
        )
        allow_download_for_message = bool(download_files) and (
            not username_filter
            or (parsed_username and parsed_username in username_filter)
            or attachment_name_match
        )
        if not dry_run:
            upsert_message(connection, channel_id, message, message_text, message_kind, parsed)
            for attachment in attachments:
                local_path = None
                sha256 = None
                if allow_download_for_message:
                    try:
                        local_path, sha256 = download_attachment_if_needed(token, channel_id, message_id, attachment)
                        if local_path is not None:
                            downloaded += 1
                    except Exception as exc:
                        print(
                            "[warn] channel={} message={} attachment={} download_failed: {}".format(
                                channel_id,
                                message_id,
                                attachment.get("filename") or "-",
                                exc,
                            )
                        )
                upsert_attachment(
                    connection,
                    channel_id,
                    message_id,
                    attachment,
                    local_path=local_path,
                    sha256=sha256,
                )
        inserted_or_updated += 1
        if newest_id is None or snowflake_int(message_id) > snowflake_int(newest_id):
            newest_id = message_id

    if not dry_run and newest_id:
        save_last_message_id(connection, channel_id, newest_id)

    return {
        "channel_id": channel_id,
        "fetched": len(messages),
        "inserted_or_updated": inserted_or_updated,
        "downloaded": downloaded,
    }


def main():
    load_dotenv(ROOT_DIR / ".env")
    load_env_file_fallback(ROOT_DIR / ".env")
    load_dotenv(ROOT_DIR / ".env.bot.secret", override=True)
    load_env_file_fallback(ROOT_DIR / ".env.bot.secret")
    args = parse_args()
    token = os.getenv("DISCORD_BOT_TOKEN", "").strip()
    if not token:
        print("Missing DISCORD_BOT_TOKEN in environment.")
        return
    channel_ids = parse_channel_ids(args.channel_id)
    if not channel_ids:
        print("No channel ids provided. Use --channel-id or DISCORD_REVIEW_CHANNEL_IDS.")
        return
    if not DATABASE_PATH.exists():
        print("Database file not found: {}".format(DATABASE_PATH))
        print("Run scripts\\init_db.py first.")
        return

    connection = connect(DATABASE_PATH)
    try:
        with transaction(connection):
            ensure_tables(connection)

        summaries = []
        for channel_id in channel_ids:
            if args.dry_run:
                summary = sync_channel(
                    connection,
                    token=token,
                    channel_id=channel_id,
                    max_pages=max(1, args.max_pages),
                    limit=max(1, min(100, args.limit)),
                    full_scan=bool(args.full_scan),
                    download_files=bool(args.download_files),
                    dry_run=True,
                    download_only_usernames=args.download_only_username,
                )
            else:
                with transaction(connection):
                    summary = sync_channel(
                        connection,
                        token=token,
                        channel_id=channel_id,
                        max_pages=max(1, args.max_pages),
                        limit=max(1, min(100, args.limit)),
                        full_scan=bool(args.full_scan),
                        download_files=bool(args.download_files),
                        dry_run=False,
                        download_only_usernames=args.download_only_username,
                    )
            summaries.append(summary)

        print("Discord review sync done.")
        for item in summaries:
            print(
                "channel={channel_id} fetched={fetched} upserted={inserted_or_updated} downloaded={downloaded}".format(
                    **item
                )
            )
    finally:
        connection.close()


if __name__ == "__main__":
    main()
