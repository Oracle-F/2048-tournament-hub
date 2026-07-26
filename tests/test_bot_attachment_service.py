from __future__ import annotations

import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase, main
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from services import bot_attachment_service as attachments  # noqa: E402


class FakeResponse:
    def __init__(self, blocks, *, content_length=None):
        self.blocks = list(blocks)
        self.headers = {}
        if content_length is not None:
            self.headers["Content-Length"] = str(content_length)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, _size=-1):
        if not self.blocks:
            return b""
        return self.blocks.pop(0)


class BotAttachmentDownloadTests(TestCase):
    def _segment(self):
        return {
            "type": "file",
            "data": {
                "url": "https://qq.example.test/signed/replay.txt",
                "name": "../../replay.txt",
            },
        }

    def test_remote_file_is_bounded_and_atomically_published(self):
        with TemporaryDirectory() as temp_dir, patch.object(
            attachments,
            "BOT_UPLOAD_TEMP_DIR",
            Path(temp_dir),
        ), patch.object(
            attachments,
            "urlopen",
            return_value=FakeResponse(
                [b"abc", b"def"],
                content_length=6,
            ),
        ):
            path = attachments.materialize_segment_file(self._segment())

            self.assertEqual(path.read_bytes(), b"abcdef")
            self.assertEqual(path.parent, Path(temp_dir))
            self.assertTrue(path.name.startswith(".._.._replay_"))
            self.assertTrue(path.name.endswith(".txt"))
            self.assertEqual(
                list(Path(temp_dir).glob("*.part")),
                [],
            )

    def test_declared_oversize_is_rejected_without_partial_file(self):
        with TemporaryDirectory() as temp_dir, patch.object(
            attachments,
            "BOT_UPLOAD_TEMP_DIR",
            Path(temp_dir),
        ), patch.dict(
            "os.environ",
            {"BOT_ATTACHMENT_DOWNLOAD_MAX_BYTES": "5"},
            clear=False,
        ), patch.object(
            attachments,
            "urlopen",
            return_value=FakeResponse(
                [b"abcdef"],
                content_length=6,
            ),
        ):
            with self.assertRaisesRegex(ValueError, "download limit"):
                attachments.materialize_segment_file(self._segment())

            self.assertEqual(list(Path(temp_dir).iterdir()), [])

    def test_streamed_oversize_removes_partial_file(self):
        with TemporaryDirectory() as temp_dir, patch.object(
            attachments,
            "BOT_UPLOAD_TEMP_DIR",
            Path(temp_dir),
        ), patch.dict(
            "os.environ",
            {"BOT_ATTACHMENT_DOWNLOAD_MAX_BYTES": "5"},
            clear=False,
        ), patch.object(
            attachments,
            "urlopen",
            return_value=FakeResponse([b"abc", b"def"]),
        ):
            with self.assertRaisesRegex(ValueError, "download limit"):
                attachments.materialize_segment_file(self._segment())

            self.assertEqual(list(Path(temp_dir).iterdir()), [])

    def test_unsupported_remote_scheme_never_calls_urlopen(self):
        segment = {
            "type": "file",
            "data": {
                "url": "ftp://example.test/replay.txt",
                "name": "replay.txt",
            },
        }
        with TemporaryDirectory() as temp_dir, patch.object(
            attachments,
            "BOT_UPLOAD_TEMP_DIR",
            Path(temp_dir),
        ), patch.object(attachments, "urlopen") as opener:
            with self.assertRaisesRegex(ValueError, "Unsupported"):
                attachments.materialize_segment_file(segment)
        opener.assert_not_called()


if __name__ == "__main__":
    main()
