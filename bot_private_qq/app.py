import asyncio
import math
import os
import sys
from datetime import datetime
from pathlib import Path
from time import perf_counter

import nonebot
from nonebot import logger, on_message, on_metaevent
from nonebot.adapters.onebot.v11 import Adapter as OneBotV11Adapter
from nonebot.adapters.onebot.v11 import Bot as OneBotV11Bot
from nonebot.adapters.onebot.v11 import GroupMessageEvent, HeartbeatMetaEvent, LifecycleMetaEvent, Message, MessageEvent, MetaEvent, PrivateMessageEvent
from nonebot.adapters.onebot.v11 import bot as onebot_v11_bot
from nonebot.adapters.onebot.v11.exception import ActionFailed
from nonebot.params import EventPlainText


ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from db import connect, ensure_parent_dir, initialize_schema
from bot_private_qq.onebot_transport import (
    inbound_debug_summary,
    onebot_event_to_inbound,
    send_onebot_reply,
)
from services.bot_connection_watchdog import mark_bot_process_started, record_bot_meta_event
from services.bot_private_service import (
    _append_group_debug,
    _message_handler_error_reply,
    patch_onebot_reply_lookup,
    render_send_timeout_fallback_message,
    is_transport_unstable_error,
)
from services.bot_transport import dispatch_business_message, normalize_legacy_reply
from settings import DATABASE_PATH, LOCAL_TIMEZONE


TEXT_AT_NAMES = [
    item.strip()
    for item in str(os.getenv("GROUP_CHAT_TEXT_AT_NAMES", "maomaoBot")).split(",")
    if item.strip()
]
_SCHEMA_READY = False
_MESSAGE_SQLITE_LOCK_RETRY_COUNT = 3
_MESSAGE_SQLITE_LOCK_RETRY_DELAY_SECONDS = 0.35
_MESSAGE_SLOW_LOG_MS = int(str(os.getenv("BOT_MESSAGE_SLOW_LOG_MS", "1500")).strip() or "1500")
PRIVATE_DEBUG_LOG_PATH = ROOT_DIR / "data" / "tmp" / "private_debug.log"


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


BOT_PRIVATE_DEBUG_LOG_ENABLED = _env_flag("BOT_PRIVATE_DEBUG_LOG_ENABLED", False)


def _append_private_debug(message):
    if not BOT_PRIVATE_DEBUG_LOG_ENABLED:
        return
    timestamp = datetime.now(LOCAL_TIMEZONE).strftime("%Y-%m-%d %H:%M:%S")
    try:
        PRIVATE_DEBUG_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with PRIVATE_DEBUG_LOG_PATH.open("a", encoding="utf-8") as handle:
            handle.write("[{}] {}\n".format(timestamp, message))
    except Exception:
        pass


_MESSAGE_FILE_API_TIMEOUT_SECONDS = _env_positive_float("BOT_MESSAGE_FILE_API_TIMEOUT_SECONDS", 1.5)
_GROUP_CHAT_TO_ME_FALLBACK_ENABLED = _env_flag("GROUP_CHAT_TO_ME_FALLBACK_ENABLED", False)


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
    mark_bot_process_started()

    bot_meta_event = on_metaevent(priority=1, block=False)

    @bot_meta_event.handle()
    async def _handle_meta_event(event: MetaEvent):
        if not isinstance(event, (LifecycleMetaEvent, HeartbeatMetaEvent)):
            return
        record_bot_meta_event(event)

    private_message = on_message(priority=10, block=True)

    @private_message.handle()
    async def _handle_private_message(bot: OneBotV11Bot, event: MessageEvent, text: str = EventPlainText()):
        if not isinstance(event, (PrivateMessageEvent, GroupMessageEvent)):
            return
        request_started = perf_counter()
        inbound = await onebot_event_to_inbound(
            bot,
            event,
            text,
            text_at_names=TEXT_AT_NAMES,
            file_api_timeout_seconds=_MESSAGE_FILE_API_TIMEOUT_SECONDS,
            group_to_me_fallback=_GROUP_CHAT_TO_ME_FALLBACK_ENABLED,
        )
        segment_build_ms = int((perf_counter() - request_started) * 1000)
        _append_private_debug(
            "recv private_event user_id={} raw_text={!r} segments={!r}".format(
                str(event.get_user_id()),
                text,
                inbound_debug_summary(inbound),
            )
        )
        if isinstance(event, GroupMessageEvent):
            _append_group_debug(
                "app group_event group_id={} user_id={} at_bot={} to_me={} raw_text={!r} normalized_text={!r} segments={!r}".format(
                    inbound.address.conversation_id,
                    inbound.address.user_id,
                    inbound.mentioned,
                    bool(getattr(event, "to_me", False)),
                    text,
                    inbound.text,
                    inbound_debug_summary(inbound),
                )
            )

        reply = None
        last_exc = None
        handler_elapsed_ms = 0
        for attempt in range(_MESSAGE_SQLITE_LOCK_RETRY_COUNT):
            connection = connect(DATABASE_PATH)
            handler_started = perf_counter()
            try:
                reply = dispatch_business_message(connection, inbound)
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
                reply = normalize_legacy_reply(
                    _message_handler_error_reply(
                        exc,
                        is_group=isinstance(event, GroupMessageEvent),
                    )
                )
                break
            finally:
                connection.close()

        if reply is None:
            _append_private_debug(
                "reply none user_id={} text={!r} elapsed_ms={}".format(
                    str(event.get_user_id()),
                    text,
                    int((perf_counter() - request_started) * 1000),
                )
            )
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
            _append_private_debug(
                "send attempt user_id={} private={} reply_len={} text={!r}".format(
                    str(event.get_user_id()),
                    True,
                    len(reply.text) + len(reply.attachments),
                    text,
                )
            )
            await send_onebot_reply(bot, event, inbound, reply)
        except ActionFailed as exc:
            _append_private_debug(
                "send failed user_id={} private={} unstable={} error={!r}".format(
                    str(event.get_user_id()),
                    not isinstance(event, GroupMessageEvent),
                    is_transport_unstable_error(exc),
                    str(exc),
                )
            )
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
                fallback_source = reply.text
                if not fallback_source and reply.attachments:
                    fallback_source = "[CQ:image,file=attachment]"
                fallback_message = Message(
                    render_send_timeout_fallback_message(fallback_source)
                )
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
                    f"private={isinstance(event, PrivateMessageEvent)} text={text!r} "
                    f"reply_len={len(reply.text) + len(reply.attachments)}"
                )

    return nonebot.get_asgi()


app = create_app()


if __name__ == "__main__":
    nonebot.run()
