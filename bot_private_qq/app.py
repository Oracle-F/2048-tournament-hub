import asyncio
import math
import os
import re
import sys
from pathlib import Path
from time import perf_counter
from urllib.parse import urlparse

import nonebot
from nonebot import logger, on_message
from nonebot.adapters.onebot.v11 import Adapter as OneBotV11Adapter
from nonebot.adapters.onebot.v11 import Bot as OneBotV11Bot
from nonebot.adapters.onebot.v11 import GroupMessageEvent, Message, MessageEvent, MessageSegment, PrivateMessageEvent
from nonebot.adapters.onebot.v11 import bot as onebot_v11_bot
from nonebot.adapters.onebot.v11.exception import ActionFailed
from nonebot.params import EventPlainText


ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from db import connect, ensure_parent_dir, initialize_schema
from services.bot_private_service import (
    _append_group_debug,
    _message_handler_error_reply,
    patch_onebot_reply_lookup,
    render_send_timeout_fallback_message,
    is_transport_unstable_error,
    handle_group_message,
    handle_private_message,
)
from settings import DATABASE_PATH


BOT_PLATFORM = os.getenv("BOT_PLATFORM", "qq")
CQ_IMAGE_PATTERN = re.compile(r"\[CQ:image,file=([^\]]+)\]")
TEXT_AT_NAMES = [
    item.strip()
    for item in str(os.getenv("GROUP_CHAT_TEXT_AT_NAMES", "maomaoBot")).split(",")
    if item.strip()
]
_SCHEMA_READY = False
_MESSAGE_SQLITE_LOCK_RETRY_COUNT = 3
_MESSAGE_SQLITE_LOCK_RETRY_DELAY_SECONDS = 0.35
_MESSAGE_SLOW_LOG_MS = int(str(os.getenv("BOT_MESSAGE_SLOW_LOG_MS", "1500")).strip() or "1500")


def _env_flag(name, default=False):
    value = os.getenv(name)
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _env_positive_float(name, default):
    raw = str(os.getenv(name, str(default))).strip()
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return float(default)
    if not math.isfinite(value) or value <= 0:
        return float(default)
    return value


_MESSAGE_FILE_API_TIMEOUT_SECONDS = _env_positive_float("BOT_MESSAGE_FILE_API_TIMEOUT_SECONDS", 1.5)
_GROUP_CHAT_TO_ME_FALLBACK_ENABLED = _env_flag("GROUP_CHAT_TO_ME_FALLBACK_ENABLED", False)


def _extract_candidate_value(data, *keys):
    for key in keys:
        value = data.get(key)
        if value not in (None, ""):
            return value
    return None


def _has_accessible_file_data(data):
    return _extract_candidate_value(data, "path", "local_path", "url", "file_url") is not None


def _looks_like_accessible_file_token(value):
    text = str(value or "").strip()
    if not text:
        return False
    parsed = urlparse(text)
    if parsed.scheme in {"http", "https", "file"}:
        return True
    if len(text) >= 3 and text[1] == ":" and text[2] in {"\\", "/"}:
        return True
    if text.startswith(("/", "\\")):
        return True
    return False


def _normalize_file_api_payload(payload):
    if isinstance(payload, dict) and isinstance(payload.get("data"), dict):
        payload = payload["data"]
    if isinstance(payload, str):
        return {"url": payload}
    if not isinstance(payload, dict):
        return {}
    result = {}
    for key in ("path", "local_path", "url", "file_url", "file", "name", "file_name", "filename"):
        value = payload.get(key)
        if value not in (None, ""):
            result[key] = value
    return result


async def _hydrate_file_segment(bot, segment):
    segment_type = str(segment.get("type") or "").lower()
    data = dict(segment.get("data") or {})
    if segment_type not in {"file", "record", "video"}:
        return {"type": segment_type, "data": data}
    if _has_accessible_file_data(data):
        return {"type": segment_type, "data": data}

    file_id = _extract_candidate_value(data, "file_id", "file")
    if file_id in (None, ""):
        return {"type": segment_type, "data": data}

    candidates = [
        ("get_file", {"file_id": str(file_id)}),
        ("get_file", {"file": str(file_id)}),
        ("get_private_file_url", {"file_id": str(file_id)}),
    ]
    file_token = data.get("file")
    if file_token not in (None, "") and file_token != file_id:
        candidates.append(("get_file", {"file": str(file_token)}))

    enriched = dict(data)
    for api_name, params in candidates:
        try:
            payload = await asyncio.wait_for(
                bot.call_api(api_name, **params),
                timeout=max(0.1, _MESSAGE_FILE_API_TIMEOUT_SECONDS),
            )
        except asyncio.TimeoutError:
            logger.warning(
                f"file segment api timeout api={api_name} user_file_ref={file_id} segment_type={segment_type}"
            )
            continue
        except Exception:
            continue
        normalized = _normalize_file_api_payload(payload)
        for key, value in normalized.items():
            if key not in enriched or enriched.get(key) in (None, ""):
                enriched[key] = value
        if _has_accessible_file_data(enriched):
            return {"type": segment_type, "data": enriched}
        file_value = enriched.get("file")
        if _looks_like_accessible_file_token(file_value):
            return {"type": segment_type, "data": enriched}
    return {"type": segment_type, "data": enriched}


async def _build_message_segments(bot, event):
    segments = []
    for segment in event.get_message():
        item = {"type": segment.type, "data": dict(segment.data)}
        item = await _hydrate_file_segment(bot, item)
        segments.append(item)
    return segments


def _segment_debug_summary(segments):
    summary = []
    for segment in segments or []:
        if not isinstance(segment, dict):
            continue
        segment_type = str(segment.get("type") or "").lower()
        data = dict(segment.get("data") or {})
        if segment_type == "at":
            summary.append("at:{}".format(str(data.get("qq") or "")))
            continue
        if segment_type == "reply":
            summary.append("reply:{}".format(str(data.get("id") or data.get("message_id") or "")))
            continue
        if segment_type == "text":
            text_value = str(data.get("text") or "").strip()
            if text_value:
                summary.append("text:{}".format(text_value[:40]))
            else:
                summary.append("text")
            continue
        summary.append(segment_type or "unknown")
    return "|".join(summary)


def _has_reply_segment(event):
    for segment in event.get_message():
        if segment.type == "reply":
            return True
    return False


def _has_at_segment(event):
    for segment in event.get_message():
        if segment.type == "at":
            return True
    return False


def _is_at_bot(bot, event):
    self_id = str(getattr(bot, "self_id", "") or getattr(event, "self_id", "") or "")
    for segment in event.get_message():
        if segment.type != "at":
            continue
        qq = str((segment.data or {}).get("qq") or "")
        if qq == self_id:
            return True
    return False


def _strip_text_mention_prefix(bot, event, text):
    message = str(text or "").strip()
    if not message:
        return message
    self_id = str(getattr(bot, "self_id", "") or getattr(event, "self_id", "") or "")
    candidates = []
    if self_id:
        candidates.append(self_id)
    candidates.extend(TEXT_AT_NAMES)
    for candidate in candidates:
        prefix_pattern = r"^[＠@]\s*{}(?=$|[\s：:，,])".format(re.escape(str(candidate)))
        matched = re.match(prefix_pattern, message, flags=re.IGNORECASE)
        if not matched:
            continue
        stripped = message[matched.end() :].lstrip(" \t：:，,")
        return stripped
    return message


def _render_reply_message(reply):
    text = str(reply or "").strip()
    if not text:
        return Message("")
    matches = list(CQ_IMAGE_PATTERN.finditer(text))
    if not matches:
        return Message(text)

    segments = []
    cursor = 0
    for match in matches:
        prefix = text[cursor : match.start()]
        if prefix.strip():
            segments.append(MessageSegment.text(prefix.strip()))
        file_ref = match.group(1).strip()
        if file_ref:
            segments.append(MessageSegment.image(file=file_ref))
        cursor = match.end()
    suffix = text[cursor:]
    if suffix.strip():
        segments.append(MessageSegment.text(suffix.strip()))
    return Message(segments)


def _render_group_reply_message(reply, user_id):
    base = _render_reply_message(reply)
    if not str(base).strip():
        return base
    # Avoid default @mentions in groups to reduce platform-side anti-spam risk.
    return base


def _is_sqlite_locked_error(exc):
    text = str(exc or "").lower()
    return "database is locked" in text or "database table is locked" in text


def _is_send_timeout_error(exc):
    text = str(exc or "").lower()
    return "timeout" in text and "sendmsg" in text


def _ensure_schema_ready():
    global _SCHEMA_READY
    if _SCHEMA_READY:
        return
    ensure_parent_dir(DATABASE_PATH)
    connection = connect(DATABASE_PATH)
    try:
        try:
            initialize_schema(connection)
            _SCHEMA_READY = True
        except Exception as exc:
            if _is_sqlite_locked_error(exc):
                logger.warning(f"schema init skipped due to sqlite lock at startup: {exc}")
                return
            raise
    finally:
        connection.close()


def create_app():
    nonebot.init()
    driver = nonebot.get_driver()
    driver.register_adapter(OneBotV11Adapter)
    patch_onebot_reply_lookup(onebot_v11_bot)
    _ensure_schema_ready()

    private_message = on_message(priority=10, block=True)

    @private_message.handle()
    async def _handle_private_message(bot: OneBotV11Bot, event: MessageEvent, text: str = EventPlainText()):
        if not isinstance(event, (PrivateMessageEvent, GroupMessageEvent)):
            return
        request_started = perf_counter()
        message_segments = await _build_message_segments(bot, event)
        segment_build_ms = int((perf_counter() - request_started) * 1000)

        reply = None
        last_exc = None
        handler_elapsed_ms = 0
        for attempt in range(_MESSAGE_SQLITE_LOCK_RETRY_COUNT):
            connection = connect(DATABASE_PATH)
            handler_started = perf_counter()
            try:
                if isinstance(event, PrivateMessageEvent):
                    reply = handle_private_message(
                        connection,
                        bot_platform=BOT_PLATFORM,
                        bot_user_id=str(event.get_user_id()),
                        text=text,
                        message_segments=message_segments,
                    )
                else:
                    at_bot = _is_at_bot(bot, event)
                    normalized_text = text
                    to_me = False
                    has_reply = False
                    has_at = _has_at_segment(event)
                    # Narrow fallback: allow platform-level to_me when it is not
                    # caused only by quoting a previous bot message.
                    if _GROUP_CHAT_TO_ME_FALLBACK_ENABLED:
                        to_me = bool(getattr(event, "to_me", False))
                        has_reply = _has_reply_segment(event)
                        if not at_bot and to_me and (has_at or not has_reply):
                            at_bot = True
                    if not at_bot:
                        stripped_text = _strip_text_mention_prefix(bot, event, text)
                        if stripped_text != (text or "").strip():
                            at_bot = True
                            normalized_text = stripped_text
                    _append_group_debug(
                        "app group_event group_id={} user_id={} at_bot={} to_me={} raw_text={!r} normalized_text={!r} segments={!r}".format(
                            str(event.group_id),
                            str(event.get_user_id()),
                            at_bot,
                            to_me,
                            text,
                            normalized_text,
                            _segment_debug_summary(message_segments),
                        )
                    )
                    reply = handle_group_message(
                        connection,
                        bot_platform=BOT_PLATFORM,
                        bot_user_id=str(event.get_user_id()),
                        group_id=str(event.group_id),
                        text=normalized_text,
                        message_segments=message_segments,
                        is_at_bot=at_bot,
                    )
                last_exc = None
                handler_elapsed_ms = int((perf_counter() - handler_started) * 1000)
                break
            except Exception as exc:
                last_exc = exc
                handler_elapsed_ms = int((perf_counter() - handler_started) * 1000)
                if _is_sqlite_locked_error(exc) and attempt + 1 < _MESSAGE_SQLITE_LOCK_RETRY_COUNT:
                    logger.warning(
                        f"message handler sqlite lock attempt={attempt + 1}/{_MESSAGE_SQLITE_LOCK_RETRY_COUNT} "
                        f"user_id={event.get_user_id()} group_id={getattr(event, 'group_id', '') or '-'}: {exc}"
                    )
                    await asyncio.sleep(_MESSAGE_SQLITE_LOCK_RETRY_DELAY_SECONDS)
                    continue
                if _is_sqlite_locked_error(exc):
                    logger.warning(f"message handler hit sqlite lock after retries: {exc}")
                else:
                    if isinstance(event, GroupMessageEvent):
                        logger.warning(
                            f"message handler group_error silenced user_id={event.get_user_id()} "
                            f"group_id={getattr(event, 'group_id', '') or '-'}: {exc}"
                        )
                reply = _message_handler_error_reply(exc, is_group=isinstance(event, GroupMessageEvent))
                break
            finally:
                connection.close()

        if reply is None:
            total_elapsed_ms = int((perf_counter() - request_started) * 1000)
            if total_elapsed_ms >= _MESSAGE_SLOW_LOG_MS:
                logger.warning(
                    f"message request slow_no_reply total_ms={total_elapsed_ms} "
                    f"segment_build_ms={segment_build_ms} handler_ms={handler_elapsed_ms} "
                    f"user_id={event.get_user_id()} group_id={getattr(event, 'group_id', '') or '-'} "
                    f"private={isinstance(event, PrivateMessageEvent)} text={text!r} exc={last_exc!r}"
                )
            return
        send_started = perf_counter()
        try:
            if isinstance(event, GroupMessageEvent):
                await private_message.finish(_render_group_reply_message(reply, str(event.get_user_id())))
            else:
                await private_message.finish(_render_reply_message(reply))
        except ActionFailed as exc:
            if is_transport_unstable_error(exc):
                logger.warning(
                    f"message reply transport unstable user_id={event.get_user_id()} "
                    f"group_id={getattr(event, 'group_id', '') or '-'}: {exc}"
                )
                return
            if _is_send_timeout_error(exc):
                logger.warning(
                    f"message reply send degraded user_id={event.get_user_id()} "
                    f"group_id={getattr(event, 'group_id', '') or '-'}: {exc}"
                )
                fallback_message = Message(render_send_timeout_fallback_message(reply))
                if str(fallback_message).strip():
                    try:
                        await private_message.send(fallback_message)
                    except Exception:
                        pass
                return
            raise
        finally:
            send_elapsed_ms = int((perf_counter() - send_started) * 1000)
            total_elapsed_ms = int((perf_counter() - request_started) * 1000)
            if total_elapsed_ms >= _MESSAGE_SLOW_LOG_MS:
                logger.warning(
                    f"message request slow total_ms={total_elapsed_ms} "
                    f"segment_build_ms={segment_build_ms} handler_ms={handler_elapsed_ms} send_ms={send_elapsed_ms} "
                    f"user_id={event.get_user_id()} group_id={getattr(event, 'group_id', '') or '-'} "
                    f"private={isinstance(event, PrivateMessageEvent)} text={text!r} reply_len={len(str(reply or ''))}"
                )

    return nonebot.get_asgi()


app = create_app()


if __name__ == "__main__":
    nonebot.run()
