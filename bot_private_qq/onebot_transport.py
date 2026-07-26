from __future__ import annotations

import asyncio
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from nonebot import logger
from nonebot.adapters.onebot.v11 import Message, MessageSegment

from services.bot_private_service import (
    is_group_command_prefixed,
    normalize_group_command_text,
)
from services.bot_transport import (
    BotAddress,
    BotAttachment,
    BotContractError,
    BotInboundMessage,
    BotReply,
    SendReceipt,
)


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
    return text.startswith(("/", "\\"))


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


async def hydrate_file_segment(bot, segment, *, timeout_seconds):
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
                timeout=max(0.1, float(timeout_seconds)),
            )
        except asyncio.TimeoutError:
            logger.warning(
                "file segment api timeout api={} segment_type={}".format(
                    api_name,
                    segment_type,
                )
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


async def build_message_segments(bot, event, *, timeout_seconds):
    segments = []
    for segment in event.get_message():
        item = {"type": segment.type, "data": dict(segment.data)}
        segments.append(
            await hydrate_file_segment(
                bot,
                item,
                timeout_seconds=timeout_seconds,
            )
        )
    return segments


def segment_debug_summary(segments):
    summary = []
    for segment in segments or []:
        if not isinstance(segment, dict):
            continue
        segment_type = str(segment.get("type") or "").lower()
        data = dict(segment.get("data") or {})
        if segment_type == "at":
            summary.append("at:{}".format(str(data.get("qq") or "")))
        elif segment_type == "reply":
            summary.append("reply:{}".format(str(data.get("id") or data.get("message_id") or "")))
        elif segment_type == "text":
            text_value = str(data.get("text") or "").strip()
            summary.append("text:{}".format(text_value[:40]) if text_value else "text")
        else:
            summary.append(segment_type or "unknown")
    return "|".join(summary)


def inbound_debug_summary(inbound):
    parts = []
    if inbound.mentioned:
        parts.append("mentioned")
    if inbound.reference_id:
        parts.append("reply:{}".format(inbound.reference_id))
    parts.extend(attachment.kind for attachment in inbound.attachments)
    return "|".join(parts)


def _has_segment_type(segments, segment_type):
    return any(str((item or {}).get("type") or "").lower() == segment_type for item in segments or [])


def _is_at_bot(bot, event, segments):
    self_id = str(getattr(bot, "self_id", "") or getattr(event, "self_id", "") or "")
    for segment in segments:
        if str(segment.get("type") or "").lower() != "at":
            continue
        if str((segment.get("data") or {}).get("qq") or "") == self_id:
            return True
    return False


def _strip_text_mention_prefix(bot, event, text, text_at_names):
    message = str(text or "").strip()
    if not message:
        return message
    self_id = str(getattr(bot, "self_id", "") or getattr(event, "self_id", "") or "")
    candidates = ([self_id] if self_id else []) + [str(item) for item in text_at_names]
    for candidate in candidates:
        prefix_pattern = r"^[＠@]\s*{}(?=$|[\s：:，,])".format(re.escape(candidate))
        matched = re.match(prefix_pattern, message, flags=re.IGNORECASE)
        if matched:
            return message[matched.end() :].lstrip(" \t：:，,")
    return message


def _segment_to_attachment(segment):
    segment_type = str(segment.get("type") or "").lower()
    data = dict(segment.get("data") or {})
    kind_by_type = {
        "image": "image",
        "file": "file",
        "record": "audio",
        "video": "video",
    }
    kind = kind_by_type.get(segment_type)
    if kind is None:
        return None

    name = _extract_candidate_value(data, "name", "file_name", "filename")
    url_value = _extract_candidate_value(data, "url", "file_url")
    local_value = _extract_candidate_value(data, "path", "local_path")
    file_value = data.get("file")
    platform_file_id = data.get("file_id")
    if file_value not in (None, ""):
        file_text = str(file_value).strip()
        if _looks_like_accessible_file_token(file_text):
            parsed = urlparse(file_text)
            if parsed.scheme in {"http", "https"} and not url_value:
                url_value = file_text
            elif not local_value:
                local_value = file_text
        elif not platform_file_id:
            platform_file_id = file_text
    if not any((name, url_value, local_value, platform_file_id)):
        return None
    return BotAttachment(
        kind=kind,
        name=name,
        content_type=data.get("content_type"),
        url=url_value,
        local_path=Path(local_value) if local_value else None,
        platform_file_id=platform_file_id,
    )


def _reference_id(segments):
    for segment in segments:
        if str(segment.get("type") or "").lower() != "reply":
            continue
        data = segment.get("data") or {}
        value = data.get("id") or data.get("message_id")
        if value not in (None, ""):
            return str(value)
    return None


async def onebot_event_to_inbound(
    bot,
    event,
    text,
    *,
    text_at_names=(),
    file_api_timeout_seconds=1.5,
    group_to_me_fallback=False,
):
    segments = await build_message_segments(
        bot,
        event,
        timeout_seconds=file_api_timeout_seconds,
    )
    user_id = str(event.get_user_id())
    is_group = getattr(event, "group_id", None) not in (None, "")
    conversation_kind = "group" if is_group else "private"
    conversation_id = str(event.group_id) if is_group else user_id
    normalized_text = str(text or "")
    mentioned = False

    if is_group:
        mentioned = _is_at_bot(bot, event, segments)
        has_at = _has_segment_type(segments, "at")
        has_reply = _has_segment_type(segments, "reply")
        if group_to_me_fallback and not mentioned and bool(getattr(event, "to_me", False)):
            if has_at or not has_reply:
                mentioned = True
        if not mentioned and is_group_command_prefixed(normalized_text):
            mentioned = True
            normalized_text = normalize_group_command_text(normalized_text)
        if not mentioned:
            stripped = _strip_text_mention_prefix(
                bot,
                event,
                normalized_text,
                text_at_names,
            )
            if stripped != normalized_text.strip():
                mentioned = True
                normalized_text = stripped

    attachments = tuple(
        attachment
        for attachment in (_segment_to_attachment(segment) for segment in segments)
        if attachment is not None
    )
    message_id = getattr(event, "message_id", None)
    event_time = getattr(event, "time", None)
    received_at = None
    if event_time not in (None, ""):
        try:
            received_at = datetime.fromtimestamp(float(event_time)).astimezone()
        except (TypeError, ValueError, OSError):
            received_at = None
    return BotInboundMessage(
        address=BotAddress(
            transport="onebot_v11",
            conversation_kind=conversation_kind,
            conversation_id=conversation_id,
            user_id=user_id,
        ),
        text=normalized_text,
        attachments=attachments,
        message_id=None if message_id in (None, "") else str(message_id),
        reference_id=_reference_id(segments),
        mentioned=mentioned,
        received_at=received_at,
    )


def _attachment_file_reference(attachment):
    if attachment.url:
        return attachment.url
    if attachment.local_path:
        path = attachment.local_path.expanduser()
        return path.resolve().as_uri() if path.is_absolute() else str(path)
    if attachment.platform_file_id:
        return attachment.platform_file_id
    raise BotContractError("attachment has no sendable OneBot file reference")


def render_onebot_reply(reply):
    if not isinstance(reply, BotReply):
        raise BotContractError("OneBot renderer requires BotReply")
    segments = []
    if reply.text:
        segments.append(MessageSegment.text(reply.text))
    for attachment in reply.attachments:
        file_reference = _attachment_file_reference(attachment)
        if attachment.kind == "image":
            segments.append(MessageSegment.image(file=file_reference))
        else:
            segment_type = {
                "file": "file",
                "audio": "record",
                "video": "video",
            }[attachment.kind]
            segments.append(
                MessageSegment(
                    type=segment_type,
                    data={"file": file_reference},
                )
            )
    return Message(segments)


async def send_onebot_reply(bot, event, inbound, reply):
    message = render_onebot_reply(reply)
    result = await bot.send(event, message)
    message_id = None
    if isinstance(result, dict):
        message_id = result.get("message_id")
    elif result not in (None, ""):
        message_id = result
    return (
        SendReceipt(
            transport="onebot_v11",
            target=inbound.address.conversation_id,
            status="sent",
            platform_message_id=None if message_id in (None, "") else str(message_id),
            sent_at=datetime.now().astimezone(),
        ),
    )
