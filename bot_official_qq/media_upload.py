from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.request import Request, urlopen

from bot_official_qq.transport import BotpyUrlMediaUploader
from services.bot_transport import BotAttachment, BotTransportError


FIRST_HASH_BYTES = 10_002_432
HARD_FILE_SIZE_LIMIT = 200 * 1024 * 1024
PutPart = Callable[[str, bytes], Awaitable[None]]


def _field(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)


def _positive_int(value: Any, label: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise BotTransportError(
            "{} is invalid".format(label),
            code="chunk_upload_contract_invalid",
            retryable=False,
        ) from exc
    if parsed <= 0:
        raise BotTransportError(
            "{} must be positive".format(label),
            code="chunk_upload_contract_invalid",
            retryable=False,
        )
    return parsed


def _file_hashes(path: Path) -> tuple[int, str, str, str]:
    md5 = hashlib.md5(usedforsecurity=False)
    sha1 = hashlib.sha1(usedforsecurity=False)
    md5_first = hashlib.md5(usedforsecurity=False)
    size = 0
    first_remaining = FIRST_HASH_BYTES
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            size += len(block)
            md5.update(block)
            sha1.update(block)
            if first_remaining > 0:
                first_block = block[:first_remaining]
                md5_first.update(first_block)
                first_remaining -= len(first_block)
    return size, md5.hexdigest(), sha1.hexdigest(), md5_first.hexdigest()


def _blocking_put_part(url: str, data: bytes) -> None:
    request = Request(
        url,
        data=data,
        method="PUT",
        headers={"Content-Length": str(len(data))},
    )
    with urlopen(request, timeout=60) as response:
        status = int(getattr(response, "status", 200))
        if status < 200 or status >= 300:
            raise OSError("presigned upload returned HTTP {}".format(status))


async def _default_put_part(url: str, data: bytes) -> None:
    await asyncio.to_thread(_blocking_put_part, url, data)


class ChunkedOfficialQQMediaUploader:
    """Upload local media with QQ's 2026 prepare/PUT/finish/merge flow."""

    def __init__(
        self,
        *,
        put_part: PutPart | None = None,
        max_put_attempts: int = 3,
    ):
        self.put_part = put_part or _default_put_part
        self.max_put_attempts = _positive_int(max_put_attempts, "max_put_attempts")
        self.url_uploader = BotpyUrlMediaUploader()

    async def _prepare(
        self,
        api: Any,
        *,
        conversation_kind: str,
        conversation_id: str,
        fields: dict[str, Any],
    ) -> Any:
        if conversation_kind == "group":
            method = getattr(api, "post_group_upload_prepare", None)
            target = {"group_openid": conversation_id}
        else:
            method = getattr(api, "post_c2c_upload_prepare", None)
            target = {"openid": conversation_id}
        if method is None:
            raise BotTransportError(
                "official QQ chunk prepare API is unavailable",
                code="chunk_upload_api_unavailable",
                retryable=False,
            )
        return await method(**target, **fields)

    async def _finish_part(
        self,
        api: Any,
        *,
        conversation_kind: str,
        conversation_id: str,
        fields: dict[str, Any],
    ) -> Any:
        if conversation_kind == "group":
            method = getattr(api, "post_group_upload_part_finish", None)
            target = {"group_openid": conversation_id}
        else:
            method = getattr(api, "post_c2c_upload_part_finish", None)
            target = {"openid": conversation_id}
        if method is None:
            raise BotTransportError(
                "official QQ chunk finish API is unavailable",
                code="chunk_upload_api_unavailable",
                retryable=False,
            )
        return await method(**target, **fields)

    async def _merge(
        self,
        api: Any,
        *,
        conversation_kind: str,
        conversation_id: str,
        file_type: int,
        file_name: str,
        upload_id: str,
    ) -> Any:
        fields = {
            "file_type": file_type,
            "file_name": file_name,
            "upload_id": upload_id,
            "srv_send_msg": False,
        }
        if conversation_kind == "group":
            return await api.post_group_file(
                group_openid=conversation_id,
                **fields,
            )
        return await api.post_c2c_file(
            openid=conversation_id,
            **fields,
        )

    async def _put_with_retry(
        self,
        url: str,
        data: bytes,
        *,
        retry_delay: float,
    ) -> None:
        last_error = None
        for attempt in range(self.max_put_attempts):
            try:
                await self.put_part(url, data)
                return
            except Exception as exc:
                last_error = exc
                if attempt + 1 < self.max_put_attempts and retry_delay > 0:
                    await asyncio.sleep(min(retry_delay, 5.0))
        raise BotTransportError(
            "official QQ presigned chunk upload failed",
            code="chunk_put_failed",
            retryable=True,
        ) from last_error

    async def upload(
        self,
        api: Any,
        *,
        conversation_kind: str,
        conversation_id: str,
        attachment: BotAttachment,
    ) -> Any:
        if attachment.platform_file_id or attachment.url:
            return await self.url_uploader.upload(
                api,
                conversation_kind=conversation_kind,
                conversation_id=conversation_id,
                attachment=attachment,
            )
        path = attachment.local_path
        if path is None:
            raise BotTransportError(
                "official QQ attachment has no upload source",
                code="media_source_missing",
                retryable=False,
            )
        resolved = path.expanduser().resolve()
        if not resolved.is_file():
            raise BotTransportError(
                "official QQ local media file is missing",
                code="local_media_missing",
                retryable=False,
            )
        size, md5, sha1, md5_first = _file_hashes(resolved)
        if size <= 0:
            raise BotTransportError(
                "official QQ local media file is empty",
                code="local_media_empty",
                retryable=False,
            )
        if size > HARD_FILE_SIZE_LIMIT:
            raise BotTransportError(
                "official QQ local media exceeds 200 MB",
                code="local_media_too_large",
                retryable=False,
            )
        file_type = {
            "image": 1,
            "video": 2,
            "audio": 3,
            "file": 4,
        }[attachment.kind]
        prepared = await self._prepare(
            api,
            conversation_kind=conversation_kind,
            conversation_id=conversation_id,
            fields={
                "file_type": file_type,
                "file_size": str(size),
                "file_name": attachment.name or resolved.name,
                "md5": md5,
                "sha1": sha1,
                "md5_10m": md5_first,
            },
        )
        upload_id = str(_field(prepared, "upload_id") or "").strip()
        parts = _field(prepared, "parts")
        if not upload_id or not isinstance(parts, (list, tuple)) or not parts:
            raise BotTransportError(
                "official QQ chunk prepare response is incomplete",
                code="chunk_upload_contract_invalid",
                retryable=False,
            )
        normalized_parts = []
        seen_indexes = set()
        for part in parts:
            index = int(_field(part, "index", -1))
            url = str(_field(part, "presigned_url") or "").strip()
            part_size = _positive_int(
                _field(part, "block_size"),
                "part block_size",
            )
            if index < 0 or index in seen_indexes or not url.startswith(("http://", "https://")):
                raise BotTransportError(
                    "official QQ chunk part is invalid",
                    code="chunk_upload_contract_invalid",
                    retryable=False,
                )
            seen_indexes.add(index)
            normalized_parts.append((index, url, part_size))
        normalized_parts.sort(key=lambda item: item[0])
        if [item[0] for item in normalized_parts] != list(range(len(normalized_parts))):
            raise BotTransportError(
                "official QQ chunk indexes are not contiguous",
                code="chunk_upload_contract_invalid",
                retryable=False,
            )
        if sum(item[2] for item in normalized_parts) != size:
            raise BotTransportError(
                "official QQ chunk sizes do not match the file",
                code="chunk_upload_contract_invalid",
                retryable=False,
            )
        upload_config = _field(prepared, "upload_config") or {}
        try:
            retry_delay = max(0.0, float(_field(upload_config, "retry_delay", 1)))
        except (TypeError, ValueError):
            retry_delay = 1.0
        with resolved.open("rb") as handle:
            for index, url, part_size in normalized_parts:
                data = handle.read(part_size)
                if len(data) != part_size:
                    raise BotTransportError(
                        "official QQ local media changed during upload",
                        code="local_media_changed",
                        retryable=True,
                    )
                await self._put_with_retry(
                    url,
                    data,
                    retry_delay=retry_delay,
                )
                await self._finish_part(
                    api,
                    conversation_kind=conversation_kind,
                    conversation_id=conversation_id,
                    fields={
                        "upload_id": upload_id,
                        "part_index": index,
                        "block_size": str(len(data)),
                        "md5": hashlib.md5(
                            data,
                            usedforsecurity=False,
                        ).hexdigest(),
                    },
                )
            if handle.read(1):
                raise BotTransportError(
                    "official QQ local media changed during upload",
                    code="local_media_changed",
                    retryable=True,
                )
        return await self._merge(
            api,
            conversation_kind=conversation_kind,
            conversation_id=conversation_id,
            file_type=file_type,
            file_name=attachment.name or resolved.name,
            upload_id=upload_id,
        )
