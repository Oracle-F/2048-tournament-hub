from __future__ import annotations

import hashlib
import sys
import tempfile
from pathlib import Path
from unittest import IsolatedAsyncioTestCase, main


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from bot_official_qq.media_upload import (  # noqa: E402
    ChunkedOfficialQQMediaUploader,
)
from services.bot_transport import BotAttachment, BotTransportError  # noqa: E402


class FakeChunkApi:
    def __init__(self, prepared):
        self.prepared = prepared
        self.calls = []

    async def post_group_upload_prepare(self, **fields):
        self.calls.append(("prepare", fields))
        return self.prepared

    async def post_group_upload_part_finish(self, **fields):
        self.calls.append(("finish", fields))
        return {}

    async def post_group_file(self, **fields):
        self.calls.append(("merge", fields))
        return {
            "file_info": "merged-file-info",
            "ttl": 300,
        }


class OfficialQQChunkUploadTests(IsolatedAsyncioTestCase):
    async def test_local_file_uses_prepare_put_finish_and_merge_in_order(self):
        data = b"abcdefghi"
        prepared = {
            "upload_id": "upload-1",
            "parts": [
                {
                    "index": 0,
                    "presigned_url": "https://cos.example/part-1?secret=one",
                    "block_size": "4",
                },
                {
                    "index": 1,
                    "presigned_url": "https://cos.example/part-2?secret=two",
                    "block_size": "5",
                },
            ],
            "upload_config": {"retry_delay": 0},
        }
        api = FakeChunkApi(prepared)
        puts = []

        async def put_part(url, part):
            puts.append((url, part))

        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory) / "总榜.png"
            image.write_bytes(data)
            uploader = ChunkedOfficialQQMediaUploader(put_part=put_part)
            media = await uploader.upload(
                api,
                conversation_kind="group",
                conversation_id="opaque-group",
                attachment=BotAttachment(
                    kind="image",
                    name="总榜.png",
                    local_path=image,
                ),
            )

        self.assertEqual(media["file_info"], "merged-file-info")
        self.assertEqual([item[1] for item in puts], [b"abcd", b"efghi"])
        self.assertEqual(
            [call[0] for call in api.calls],
            ["prepare", "finish", "finish", "merge"],
        )
        prepare = api.calls[0][1]
        self.assertEqual(prepare["group_openid"], "opaque-group")
        self.assertEqual(prepare["file_type"], 1)
        self.assertEqual(prepare["file_size"], "9")
        self.assertEqual(
            prepare["md5"],
            hashlib.md5(data, usedforsecurity=False).hexdigest(),
        )
        self.assertEqual(
            prepare["sha1"],
            hashlib.sha1(data, usedforsecurity=False).hexdigest(),
        )
        self.assertEqual(prepare["md5_10m"], prepare["md5"])
        first_finish = api.calls[1][1]
        self.assertEqual(first_finish["part_index"], 0)
        self.assertEqual(first_finish["block_size"], "4")
        self.assertEqual(
            first_finish["md5"],
            hashlib.md5(b"abcd", usedforsecurity=False).hexdigest(),
        )
        merge = api.calls[-1][1]
        self.assertEqual(merge["upload_id"], "upload-1")
        self.assertFalse(merge["srv_send_msg"])
        self.assertNotIn("url", merge)

    async def test_presigned_put_is_retried_without_repeating_prepare(self):
        prepared = {
            "upload_id": "upload-1",
            "parts": [
                {
                    "index": 0,
                    "presigned_url": "https://cos.example/part?secret=value",
                    "block_size": "4",
                }
            ],
            "upload_config": {"retry_delay": 0},
        }
        api = FakeChunkApi(prepared)
        attempts = 0

        async def flaky_put(_url, _part):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise OSError("temporary")

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rank.png"
            path.write_bytes(b"data")
            media = await ChunkedOfficialQQMediaUploader(
                put_part=flaky_put,
                max_put_attempts=2,
            ).upload(
                api,
                conversation_kind="group",
                conversation_id="opaque-group",
                attachment=BotAttachment(kind="image", local_path=path),
            )

        self.assertEqual(media["file_info"], "merged-file-info")
        self.assertEqual(attempts, 2)
        self.assertEqual(
            [call[0] for call in api.calls].count("prepare"),
            1,
        )

    async def test_failed_put_is_retryable_and_never_finishes_or_merges(self):
        prepared = {
            "upload_id": "upload-1",
            "parts": [
                {
                    "index": 0,
                    "presigned_url": "https://cos.example/part?secret=value",
                    "block_size": "4",
                }
            ],
            "upload_config": {"retry_delay": 0},
        }
        api = FakeChunkApi(prepared)

        async def failed_put(_url, _part):
            raise OSError("offline")

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rank.png"
            path.write_bytes(b"data")
            with self.assertRaises(BotTransportError) as raised:
                await ChunkedOfficialQQMediaUploader(
                    put_part=failed_put,
                    max_put_attempts=2,
                ).upload(
                    api,
                    conversation_kind="group",
                    conversation_id="opaque-group",
                    attachment=BotAttachment(kind="image", local_path=path),
                )

        self.assertEqual(raised.exception.code, "chunk_put_failed")
        self.assertTrue(raised.exception.retryable)
        self.assertEqual([call[0] for call in api.calls], ["prepare"])

    async def test_malformed_part_contract_is_rejected_before_presigned_put(self):
        prepared = {
            "upload_id": "upload-1",
            "parts": [
                {
                    "index": 2,
                    "presigned_url": "https://cos.example/part?secret=value",
                    "block_size": "4",
                }
            ],
        }
        api = FakeChunkApi(prepared)
        puts = []

        async def put_part(url, data):
            puts.append((url, data))

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rank.png"
            path.write_bytes(b"data")
            with self.assertRaises(BotTransportError) as raised:
                await ChunkedOfficialQQMediaUploader(
                    put_part=put_part,
                ).upload(
                    api,
                    conversation_kind="group",
                    conversation_id="opaque-group",
                    attachment=BotAttachment(kind="image", local_path=path),
                )

        self.assertEqual(
            raised.exception.code,
            "chunk_upload_contract_invalid",
        )
        self.assertEqual(puts, [])
        self.assertEqual([call[0] for call in api.calls], ["prepare"])

    async def test_missing_local_file_is_terminal_without_api_calls(self):
        api = FakeChunkApi({})
        with self.assertRaises(BotTransportError) as raised:
            await ChunkedOfficialQQMediaUploader().upload(
                api,
                conversation_kind="group",
                conversation_id="opaque-group",
                attachment=BotAttachment(
                    kind="image",
                    local_path=Path("/missing/rank.png"),
                ),
            )

        self.assertEqual(raised.exception.code, "local_media_missing")
        self.assertFalse(raised.exception.retryable)
        self.assertEqual(api.calls, [])


if __name__ == "__main__":
    main()
