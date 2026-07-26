"""Pure official-QQ event processing.

This module intentionally has no SDK import and no startup side effect.  A
future authorized runtime can call ``process_official_event`` from botpy
callbacks while keeping credentials and gateway lifecycle outside business
services.
"""

from __future__ import annotations

from dataclasses import dataclass

from bot_official_qq.transport import (
    OfficialEventDeduplicator,
    OfficialQQTransport,
    official_event_to_inbound,
)
from services.bot_private_service import _message_handler_error_reply
from services.bot_transport import (
    BotInboundMessage,
    BotReply,
    SendReceipt,
    dispatch_business_message,
    normalize_legacy_reply,
)


@dataclass(frozen=True, slots=True)
class OfficialEventOutcome:
    inbound: BotInboundMessage | None
    reply: BotReply | None
    receipts: tuple[SendReceipt, ...] = ()
    ignored: bool = False
    duplicate: bool = False


async def process_official_event(
    connection,
    *,
    event_type: str,
    event,
    transport: OfficialQQTransport,
    deduplicator: OfficialEventDeduplicator | None = None,
) -> OfficialEventOutcome:
    if deduplicator is not None and not deduplicator.accept(event_type, event):
        return OfficialEventOutcome(
            inbound=None,
            reply=None,
            ignored=True,
            duplicate=True,
        )
    inbound = official_event_to_inbound(event_type, event)
    if inbound is None:
        return OfficialEventOutcome(
            inbound=None,
            reply=None,
            ignored=True,
        )
    try:
        reply = dispatch_business_message(connection, inbound)
    except Exception as exc:
        reply = normalize_legacy_reply(
            _message_handler_error_reply(
                exc,
                is_group=inbound.address.conversation_kind == "group",
            )
        )
    if reply is None:
        return OfficialEventOutcome(inbound=inbound, reply=None)
    receipts = await transport.reply(inbound, reply)
    return OfficialEventOutcome(
        inbound=inbound,
        reply=reply,
        receipts=receipts,
    )
