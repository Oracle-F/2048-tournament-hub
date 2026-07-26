from __future__ import annotations

from collections import OrderedDict
from datetime import datetime
from typing import Any, Protocol

from services.bot_transport import (
    BotAddress,
    BotAttachment,
    BotContractError,
    BotInboundMessage,
    BotReply,
    BotTransport,
    BotTransportError,
    SendReceipt,
)
from settings import LOCAL_TIMEZONE


TRANSPORT_NAME = "qq_official"
SUPPORTED_EVENT_TYPES = {
    "C2C_MESSAGE_CREATE",
    "GROUP_AT_MESSAGE_CREATE",
}
_FILE_TYPE_BY_ATTACHMENT = {
    "image": 1,
    "video": 2,
    "audio": 3,
    "file": 4,
}
_RETRYABLE_ERROR_CODES = {
    "40034004",  # rich-media transfer failed
    "40034100",  # proactive quota
    "40054006",  # relationship validation transient failure
    "50055002",  # send transient failure
}
class OfficialQQMediaUploader(Protocol):
    async def upload(
        self,
        api: Any,
        *,
        conversation_kind: str,
        conversation_id: str,
        attachment: BotAttachment,
    ) -> Any:
        ...


def _field(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)


def _event_payload(event: Any) -> tuple[Any, str | None]:
    data = _field(event, "d")
    if data is None:
        return event, _string_or_none(_field(event, "event_id"))
    return data, _string_or_none(_field(event, "id"))


def _string_or_none(value: Any) -> str | None:
    if value in (None, ""):
        return None
    return str(value).strip() or None


def _parse_timestamp(value: Any) -> datetime | None:
    text = _string_or_none(value)
    if text is None:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=LOCAL_TIMEZONE)
    return parsed.astimezone(LOCAL_TIMEZONE)


def _official_attachment(value: Any) -> BotAttachment | None:
    url = _string_or_none(_field(value, "url"))
    filename = _string_or_none(_field(value, "filename"))
    content_type = (_string_or_none(_field(value, "content_type")) or "").lower()
    voice_url = _string_or_none(_field(value, "voice_wav_url"))
    if not any((url, filename, voice_url)):
        return None
    if content_type.startswith("image/"):
        kind = "image"
    elif content_type.startswith("video/"):
        kind = "video"
    elif content_type == "voice" or content_type.startswith("audio/"):
        kind = "audio"
    else:
        kind = "file"
    return BotAttachment(
        kind=kind,
        name=filename,
        content_type=content_type or None,
        url=voice_url if kind == "audio" and voice_url else url,
    )


def _reference_message_id(payload: Any) -> str | None:
    reference = _field(payload, "message_reference")
    value = _string_or_none(_field(reference, "message_id")) if reference else None
    if value:
        return value
    referenced = _field(payload, "referenced_message")
    return _string_or_none(_field(referenced, "id")) if referenced else None


def official_event_dedupe_key(event_type: str, event: Any) -> str | None:
    normalized_type = str(event_type or "").strip().upper()
    if normalized_type not in SUPPORTED_EVENT_TYPES:
        return None
    payload, _outer_event_id = _event_payload(event)
    message_id = _string_or_none(_field(payload, "id"))
    if not message_id:
        return None
    message_scene = _field(payload, "message_scene")
    message_index = None
    for item in _field(message_scene, "ext", ()) or ():
        text = str(item or "")
        if text.startswith("msg_idx="):
            message_index = text.partition("=")[2].strip() or None
            break
    return "{}:{}:{}".format(
        normalized_type,
        message_id,
        message_index or "-",
    )


class OfficialEventDeduplicator:
    def __init__(self, max_entries: int = 4096):
        if int(max_entries) < 1:
            raise ValueError("max_entries must be positive")
        self.max_entries = int(max_entries)
        self._keys: OrderedDict[str, None] = OrderedDict()

    def accept(self, event_type: str, event: Any) -> bool:
        key = official_event_dedupe_key(event_type, event)
        if key is None:
            return True
        if key in self._keys:
            self._keys.move_to_end(key)
            return False
        self._keys[key] = None
        while len(self._keys) > self.max_entries:
            self._keys.popitem(last=False)
        return True


def official_event_to_inbound(
    event_type: str,
    event: Any,
) -> BotInboundMessage | None:
    normalized_type = str(event_type or "").strip().upper()
    if normalized_type not in SUPPORTED_EVENT_TYPES:
        return None
    payload, outer_event_id = _event_payload(event)
    author = _field(payload, "author")
    if normalized_type == "C2C_MESSAGE_CREATE":
        conversation_kind = "private"
        user_id = (
            _string_or_none(_field(author, "user_openid"))
            or _string_or_none(_field(author, "id"))
        )
        conversation_id = user_id
        mentioned = False
    else:
        conversation_kind = "group"
        user_id = (
            _string_or_none(_field(author, "member_openid"))
            or _string_or_none(_field(author, "id"))
        )
        conversation_id = _string_or_none(_field(payload, "group_openid"))
        mentioned = True
    if not user_id or not conversation_id:
        raise BotContractError(
            "{} event is missing required OpenID fields".format(normalized_type)
        )
    attachments = tuple(
        attachment
        for attachment in (
            _official_attachment(item)
            for item in (_field(payload, "attachments") or ())
        )
        if attachment is not None
    )
    return BotInboundMessage(
        address=BotAddress(
            transport=TRANSPORT_NAME,
            conversation_kind=conversation_kind,
            conversation_id=conversation_id,
            user_id=user_id,
        ),
        text=str(_field(payload, "content") or ""),
        attachments=attachments,
        message_id=_string_or_none(_field(payload, "id")),
        event_id=outer_event_id,
        reference_id=_reference_message_id(payload),
        mentioned=mentioned,
        received_at=_parse_timestamp(_field(payload, "timestamp")),
    )


def _media_payload(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        file_info = value.strip()
        if file_info:
            return {"file_info": file_info}
    if isinstance(value, dict):
        file_info = _string_or_none(value.get("file_info"))
        if file_info:
            return value
    file_info = _string_or_none(_field(value, "file_info"))
    if file_info:
        return {"file_info": file_info}
    raise BotTransportError(
        "official QQ upload returned no file_info",
        code="media_file_info_missing",
        retryable=False,
    )


class BotpyUrlMediaUploader:
    """Use botpy's whole-file URL upload.

    Current PyPI botpy does not expose the 2026 chunked local-file flow.  A
    runtime that sends local files must inject an uploader implementing
    ``OfficialQQMediaUploader``; this default never invents a public URL.
    """

    async def upload(
        self,
        api: Any,
        *,
        conversation_kind: str,
        conversation_id: str,
        attachment: BotAttachment,
    ) -> Any:
        if attachment.platform_file_id:
            return {"file_info": attachment.platform_file_id}
        if not attachment.url:
            raise BotTransportError(
                "local official QQ media upload is not configured",
                code="local_media_upload_unconfigured",
                retryable=False,
            )
        file_type = _FILE_TYPE_BY_ATTACHMENT[attachment.kind]
        if conversation_kind == "group":
            value = await api.post_group_file(
                group_openid=conversation_id,
                file_type=file_type,
                url=attachment.url,
                srv_send_msg=False,
            )
        else:
            value = await api.post_c2c_file(
                openid=conversation_id,
                file_type=file_type,
                url=attachment.url,
                srv_send_msg=False,
            )
        return _media_payload(value)


def _exception_code(exc: BaseException) -> str | None:
    for source in (
        exc,
        _field(exc, "response"),
        _field(exc, "data"),
        _field(_field(exc, "response"), "data"),
    ):
        for key in ("code", "error_code", "status", "status_code"):
            value = _field(source, key)
            if value not in (None, ""):
                return str(value)
    return None


def _retryable_code(code: str | None) -> bool:
    if code is None:
        return False
    if code in _RETRYABLE_ERROR_CODES or code == "429":
        return True
    try:
        return 500 <= int(code) <= 599
    except ValueError:
        return False


def _receipt_from_exception(
    exc: BaseException,
    *,
    target: str,
    uncertain_without_code: bool,
) -> SendReceipt:
    if isinstance(exc, BotTransportError):
        code = exc.code
        return SendReceipt(
            transport=TRANSPORT_NAME,
            target=target,
            status="failed",
            error_code=code,
            retryable=exc.retryable,
        )
    code = _exception_code(exc)
    if code is None and uncertain_without_code:
        return SendReceipt(
            transport=TRANSPORT_NAME,
            target=target,
            status="unknown",
            error_code="transport_exception",
            retryable=False,
        )
    retryable = _retryable_code(code)
    return SendReceipt(
        transport=TRANSPORT_NAME,
        target=target,
        status="failed",
        error_code=code or "transport_exception",
        retryable=retryable,
    )


def _response_receipt(response: Any, *, target: str) -> SendReceipt:
    message_id = _string_or_none(_field(response, "id"))
    timestamp = _parse_timestamp(_field(response, "timestamp"))
    if message_id is None:
        return SendReceipt(
            transport=TRANSPORT_NAME,
            target=target,
            status="unknown",
            error_code="message_id_missing",
        )
    return SendReceipt(
        transport=TRANSPORT_NAME,
        target=target,
        status="sent",
        platform_message_id=message_id,
        sent_at=timestamp or datetime.now(LOCAL_TIMEZONE),
    )


def _message_reference(reference_id: str | None) -> dict[str, str] | None:
    return {"message_id": reference_id} if reference_id else None


async def _post_message(
    api: Any,
    *,
    conversation_kind: str,
    conversation_id: str,
    msg_type: int,
    msg_id: str | None,
    event_id: str | None,
    msg_seq: int,
    content: str | None = None,
    media: dict[str, Any] | None = None,
    reference_id: str | None = None,
) -> Any:
    fields = {
        "msg_type": msg_type,
        "msg_id": msg_id,
        "event_id": event_id,
        "msg_seq": msg_seq,
        "content": content,
        "media": media,
        "message_reference": _message_reference(reference_id),
    }
    fields = {key: value for key, value in fields.items() if value is not None}
    if conversation_kind == "group":
        return await api.post_group_message(
            group_openid=conversation_id,
            **fields,
        )
    return await api.post_c2c_message(
        openid=conversation_id,
        **fields,
    )


async def _send_reply_parts(
    api: Any,
    *,
    conversation_kind: str,
    conversation_id: str,
    reply: BotReply,
    msg_id: str | None,
    event_id: str | None,
    uploader: OfficialQQMediaUploader,
) -> tuple[SendReceipt, ...]:
    if not msg_id and not event_id:
        raise BotContractError("official passive reply requires msg_id or event_id")
    part_count = int(bool(reply.text)) + len(reply.attachments)
    part_limit = 5 if conversation_kind == "group" else 4
    if part_count > part_limit:
        return (
            SendReceipt(
                transport=TRANSPORT_NAME,
                target=conversation_id,
                status="failed",
                error_code="reply_part_limit_exceeded",
                retryable=False,
            ),
        )
    receipts: list[SendReceipt] = []
    sequence = 1
    if reply.text:
        try:
            response = await _post_message(
                api,
                conversation_kind=conversation_kind,
                conversation_id=conversation_id,
                msg_type=0,
                msg_id=msg_id,
                event_id=event_id,
                msg_seq=sequence,
                content=reply.text,
                reference_id=reply.reference_id,
            )
        except Exception as exc:
            receipts.append(
                _receipt_from_exception(
                    exc,
                    target=conversation_id,
                    uncertain_without_code=True,
                )
            )
            return tuple(receipts)
        receipt = _response_receipt(response, target=conversation_id)
        receipts.append(receipt)
        if receipt.status != "sent":
            return tuple(receipts)
        sequence += 1
    for attachment in reply.attachments:
        try:
            uploaded = await uploader.upload(
                api,
                conversation_kind=conversation_kind,
                conversation_id=conversation_id,
                attachment=attachment,
            )
            media = _media_payload(uploaded)
        except Exception as exc:
            receipts.append(
                _receipt_from_exception(
                    exc,
                    target=conversation_id,
                    uncertain_without_code=False,
                )
            )
            return tuple(receipts)
        try:
            response = await _post_message(
                api,
                conversation_kind=conversation_kind,
                conversation_id=conversation_id,
                msg_type=7,
                msg_id=msg_id,
                event_id=event_id,
                msg_seq=sequence,
                media=media,
                reference_id=reply.reference_id,
            )
        except Exception as exc:
            receipts.append(
                _receipt_from_exception(
                    exc,
                    target=conversation_id,
                    uncertain_without_code=True,
                )
            )
            return tuple(receipts)
        receipt = _response_receipt(response, target=conversation_id)
        receipts.append(receipt)
        if receipt.status != "sent":
            return tuple(receipts)
        sequence += 1
    return tuple(receipts)


async def send_official_reply(
    api: Any,
    inbound: BotInboundMessage,
    reply: BotReply,
    *,
    uploader: OfficialQQMediaUploader | None = None,
) -> tuple[SendReceipt, ...]:
    if inbound.address.transport != TRANSPORT_NAME:
        raise BotContractError("official reply requires an official QQ inbound")
    return await _send_reply_parts(
        api,
        conversation_kind=inbound.address.conversation_kind,
        conversation_id=inbound.address.conversation_id,
        reply=reply,
        msg_id=inbound.message_id,
        event_id=inbound.event_id,
        uploader=uploader or BotpyUrlMediaUploader(),
    )


async def send_official_proactive_group(
    api: Any,
    group_openid: str,
    reply: BotReply,
    *,
    uploader: OfficialQQMediaUploader | None = None,
) -> tuple[SendReceipt, ...]:
    target = str(group_openid or "").strip()
    if not target:
        raise BotContractError("official group OpenID must not be empty")
    receipts: list[SendReceipt] = []
    media_uploader = uploader or BotpyUrlMediaUploader()
    if reply.text:
        try:
            response = await _post_message(
                api,
                conversation_kind="group",
                conversation_id=target,
                msg_type=0,
                msg_id=None,
                event_id=None,
                msg_seq=1,
                content=reply.text,
                reference_id=reply.reference_id,
            )
        except Exception as exc:
            return (
                _receipt_from_exception(
                    exc,
                    target=target,
                    uncertain_without_code=True,
                ),
            )
        receipt = _response_receipt(response, target=target)
        receipts.append(receipt)
        if receipt.status != "sent":
            return tuple(receipts)
    for attachment in reply.attachments:
        try:
            uploaded = await media_uploader.upload(
                api,
                conversation_kind="group",
                conversation_id=target,
                attachment=attachment,
            )
            media = _media_payload(uploaded)
        except Exception as exc:
            receipts.append(
                _receipt_from_exception(
                    exc,
                    target=target,
                    uncertain_without_code=False,
                )
            )
            return tuple(receipts)
        try:
            response = await _post_message(
                api,
                conversation_kind="group",
                conversation_id=target,
                msg_type=7,
                msg_id=None,
                event_id=None,
                msg_seq=1,
                media=media,
                reference_id=reply.reference_id,
            )
        except Exception as exc:
            receipts.append(
                _receipt_from_exception(
                    exc,
                    target=target,
                    uncertain_without_code=True,
                )
            )
            return tuple(receipts)
        receipt = _response_receipt(response, target=target)
        receipts.append(receipt)
        if receipt.status != "sent":
            return tuple(receipts)
    return tuple(receipts)


class OfficialQQTransport(BotTransport):
    transport_name = TRANSPORT_NAME

    def __init__(
        self,
        api: Any,
        *,
        uploader: OfficialQQMediaUploader | None = None,
    ):
        self.api = api
        self.uploader = uploader or BotpyUrlMediaUploader()

    async def reply(
        self,
        inbound: BotInboundMessage,
        reply: BotReply,
    ) -> tuple[SendReceipt, ...]:
        return await send_official_reply(
            self.api,
            inbound,
            reply,
            uploader=self.uploader,
        )

    async def send_group(
        self,
        group_id: str,
        reply: BotReply,
    ) -> tuple[SendReceipt, ...]:
        return await send_official_proactive_group(
            self.api,
            group_id,
            reply,
            uploader=self.uploader,
        )
