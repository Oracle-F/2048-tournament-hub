from __future__ import annotations

import sys
from http.client import RemoteDisconnected
from pathlib import Path
from unittest import TestCase, main
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


from services import verse_adapter as adapter  # noqa: E402


class _FakeResponse:
    def __init__(self, payload: str):
        self._payload = payload.encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self):
        return self._payload


class VerseAdapterRetryTests(TestCase):
    def test_fetch_json_retries_after_remote_disconnect(self):
        calls = {"count": 0}

        def fake_urlopen(request, timeout=None):
            calls["count"] += 1
            if calls["count"] == 1:
                raise RemoteDisconnected("remote closed")
            return _FakeResponse("{\"ok\": true}")

        with patch.object(adapter, "urlopen", side_effect=fake_urlopen):
            payload = adapter.fetch_json("https://example.com/test")

        self.assertEqual(calls["count"], 2)
        self.assertEqual(payload, {"ok": True})


if __name__ == "__main__":
    main()
