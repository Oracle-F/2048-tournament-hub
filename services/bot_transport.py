from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, Protocol
from urllib.parse import unquote, urlparse
from urllib.request import url2pathname


ConversationKind = Literal["private", "group"]
AttachmentKind = Literal["image", "file", "audio", "video"]

_CQ_IMAGE_PATTERN = re.compile(r"\[CQ:image,file=([^\]]+)\]")
_BOT_PLATFORM_BY_TRANSPORT = {
    "onebot_v11": "qq",
    "qq_official": "qq_official",
}


class BotContractError(ValueError):
    """Raised when data cannot be represented by the transport contract."""


class BotTransportError(RuntimeError):
    def __init__(self, message: str, *, code: str | None = None, retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.retryable = bool(retryable)


@dataclass(frozen=True, slots=True)
class BotAddress:
    transport: str
    conversation_kind: ConversationKind
    conversation_id: str
    user_id: str

    def __post_init__(self) -> None:
        for field_name in ("transport", "conversation_id", "user_id"):
            value = str(getattr(self, field_name) or "").strip()
            if not value:
                raise BotContractError("{} must not be empty".format(field_name))
            object.__setattr__(self, field_name, value)
        if self.conversation_kind not in {"private", "group"}:
            raise BotContractError("unsupported conversation kind: {}".format(self.conversation_kind))


@dataclass(frozen=True, slots=True)
class BotAttachment:
    kind: AttachmentKind
    name: str | None = None
    content_type: str | None = None
    url: str | None = None
    local_path: Path | None = None
    platform_file_id: str | None = None

    def __post_init__(self) -> None:
        if self.kind not in {"image", "file", "audio", "video"}:
            raise BotContractError("unsupported attachment kind: {}".format(self.kind))
        if self.local_path is not None and not isinstance(self.local_path, Path):
            object.__setattr__(self, "local_path", Path(self.local_path))
        for field_name in ("name", "content_type", "url", "platform_file_id"):
            value = getattr(self, field_name)
            if value is not None:
                object.__setattr__(self, field_name, str(value).strip() or None)
        if not any((self.name, self.url, self.local_path, self.platform_file_id)):
            raise BotContractError("attachment must include a name, URL, local path, or platform file ID")


@dataclass(frozen=True, slots=True)
class BotInboundMessage:
    address: BotAddress
    text: str = ""
    attachments: tuple[BotAttachment, ...] = field(default_factory=tuple)
    message_id: str | None = None
    event_id: str | None = None
    reference_id: str | None = None
    mentioned: bool = False
    received_at: datetime | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "text", str(self.text or ""))
        object.__setattr__(self, "attachments", tuple(self.attachments or ()))
        if not all(isinstance(item, BotAttachment) for item in self.attachments):
            raise BotContractError("inbound attachments must use BotAttachment")
        for field_name in ("message_id", "event_id", "reference_id"):
            value = getattr(self, field_name)
            if value is not None:
                object.__setattr__(self, field_name, str(value).strip() or None)
        if self.received_at is not None and not isinstance(self.received_at, datetime):
            raise BotContractError("received_at must be a datetime")


@dataclass(frozen=True, slots=True)
class BotReply:
    text: str = ""
    attachments: tuple[BotAttachment, ...] = field(default_factory=tuple)
    reference_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "text", str(self.text or "").strip())
        object.__setattr__(self, "attachments", tuple(self.attachments or ()))
        if not all(isinstance(item, BotAttachment) for item in self.attachments):
            raise BotContractError("reply attachments must use BotAttachment")
        if self.reference_id is not None:
            object.__setattr__(self, "reference_id", str(self.reference_id).strip() or None)


@dataclass(frozen=True, slots=True)
class SendReceipt:
    transport: str
    target: str
    status: Literal["sent", "failed", "unknown", "dry_run"]
    platform_message_id: str | None = None
    sent_at: datetime | None = None
    error_code: str | None = None
    retryable: bool = False

    def __post_init__(self) -> None:
        if self.status not in {"sent", "failed", "unknown", "dry_run"}:
            raise BotContractError("unsupported receipt status: {}".format(self.status))
        for field_name in ("transport", "target"):
            value = str(getattr(self, field_name) or "").strip()
            if not value:
                raise BotContractError("{} must not be empty".format(field_name))
            object.__setattr__(self, field_name, value)
        for field_name in ("platform_message_id", "error_code"):
            value = getattr(self, field_name)
            if value is not None:
                object.__setattr__(self, field_name, str(value).strip() or None)


class BotTransport(Protocol):
    async def reply(self, inbound: BotInboundMessage, reply: BotReply) -> tuple[SendReceipt, ...]:
        ...

    async def send_group(self, group_id: str, reply: BotReply) -> tuple[SendReceipt, ...]:
        ...


def bot_platform_for_transport(transport: str) -> str:
    normalized = str(transport or "").strip()
    if not normalized:
        raise BotContractError("transport must not be empty")
    return _BOT_PLATFORM_BY_TRANSPORT.get(normalized, normalized)


def _attachment_from_legacy_image(file_reference: str) -> BotAttachment:
    reference = str(file_reference or "").strip()
    if not reference:
        raise BotContractError("legacy CQ image file reference is empty")
    parsed = urlparse(reference)
    if parsed.scheme in {"http", "https"}:
        return BotAttachment(kind="image", url=reference)
    if parsed.scheme == "file":
        path_text = url2pathname(unquote(parsed.path))
        if parsed.netloc and parsed.netloc not in {"", "localhost"}:
            path_text = "//{}/{}".format(parsed.netloc, path_text.lstrip("/"))
        if not path_text:
            raise BotContractError("legacy CQ image file URI has no path")
        return BotAttachment(kind="image", local_path=Path(path_text))
    if parsed.scheme:
        raise BotContractError("unsupported legacy CQ image scheme: {}".format(parsed.scheme))
    path = Path(reference)
    if not path.is_absolute():
        raise BotContractError("legacy CQ image path must be absolute")
    return BotAttachment(kind="image", local_path=path)


def normalize_legacy_reply(reply: Any) -> BotReply | None:
    if reply is None:
        return None
    if isinstance(reply, BotReply):
        return reply
    text = str(reply)
    attachments = tuple(
        _attachment_from_legacy_image(match.group(1))
        for match in _CQ_IMAGE_PATTERN.finditer(text)
    )
    normalized_text = _CQ_IMAGE_PATTERN.sub("", text).strip()
    return BotReply(text=normalized_text, attachments=attachments)


def _attachment_to_legacy_segment(attachment: BotAttachment) -> dict[str, Any]:
    segment_type = {
        "image": "image",
        "file": "file",
        "audio": "record",
        "video": "video",
    }[attachment.kind]
    data: dict[str, Any] = {}
    if attachment.name:
        data["name"] = attachment.name
    if attachment.url:
        data["url"] = attachment.url
    if attachment.local_path:
        data["path"] = str(attachment.local_path)
    if attachment.platform_file_id:
        data["file_id"] = attachment.platform_file_id
    return {"type": segment_type, "data": data}


def dispatch_business_message(connection: Any, inbound: BotInboundMessage) -> BotReply | None:
    from services.bot_private_service import handle_group_message, handle_private_message

    bot_platform = bot_platform_for_transport(inbound.address.transport)
    message_segments = [
        _attachment_to_legacy_segment(attachment)
        for attachment in inbound.attachments
    ]
    if inbound.reference_id:
        message_segments.insert(
            0,
            {"type": "reply", "data": {"id": inbound.reference_id}},
        )
    if inbound.address.conversation_kind == "private":
        legacy_reply = handle_private_message(
            connection,
            bot_platform=bot_platform,
            bot_user_id=inbound.address.user_id,
            text=inbound.text,
            message_segments=message_segments,
        )
    else:
        legacy_reply = handle_group_message(
            connection,
            bot_platform=bot_platform,
            bot_user_id=inbound.address.user_id,
            group_id=inbound.address.conversation_id,
            text=inbound.text,
            message_segments=message_segments,
            is_at_bot=inbound.mentioned,
        )
    return normalize_legacy_reply(legacy_reply)
