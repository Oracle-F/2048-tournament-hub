from __future__ import annotations

import sys
from pathlib import Path
from unittest import IsolatedAsyncioTestCase, TestCase, main


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from bot_official_qq.sdk_facade import Botpy2026ApiFacade  # noqa: E402
from services.bot_transport import BotTransportError  # noqa: E402


class FakeHttp:
    def __init__(self):
        self.calls = []

    async def request(self, route, *, json):
        self.calls.append((route, json))
        return {"ok": True}


class FakeBotpyApi:
    def __init__(self):
        self._http = FakeHttp()
        self.message_calls = []

    async def post_group_message(self, **fields):
        self.message_calls.append(("group", fields))
        return {"id": "group-message"}

    async def post_c2c_message(self, **fields):
        self.message_calls.append(("c2c", fields))
        return {"id": "c2c-message"}


def fake_route(method, path, **parameters):
    return {
        "method": method,
        "path": path,
        "parameters": parameters,
    }


class OfficialQQSdkFacadeTests(IsolatedAsyncioTestCase):
    async def test_message_calls_stay_on_public_botpy_methods(self):
        api = FakeBotpyApi()
        facade = Botpy2026ApiFacade(api, route_factory=fake_route)

        group = await facade.post_group_message(
            group_openid="opaque-group",
            msg_type=0,
            content="group",
        )
        c2c = await facade.post_c2c_message(
            openid="opaque-user",
            msg_type=0,
            content="c2c",
        )

        self.assertEqual(group["id"], "group-message")
        self.assertEqual(c2c["id"], "c2c-message")
        self.assertEqual(
            [item[0] for item in api.message_calls],
            ["group", "c2c"],
        )
        self.assertEqual(api._http.calls, [])

    async def test_group_chunk_routes_and_merge_payload_match_official_paths(self):
        api = FakeBotpyApi()
        facade = Botpy2026ApiFacade(api, route_factory=fake_route)

        await facade.post_group_upload_prepare(
            group_openid="opaque-group",
            file_type=1,
            file_size="9",
        )
        await facade.post_group_upload_part_finish(
            group_openid="opaque-group",
            upload_id="upload-1",
            part_index=0,
            block_size="9",
            md5="hash",
        )
        await facade.post_group_file(
            group_openid="opaque-group",
            file_type=1,
            url=None,
            file_name="rank.png",
            upload_id="upload-1",
            srv_send_msg=False,
        )

        routes = [item[0] for item in api._http.calls]
        self.assertEqual(
            [route["path"] for route in routes],
            [
                "/v2/groups/{group_openid}/upload_prepare",
                "/v2/groups/{group_openid}/upload_part_finish",
                "/v2/groups/{group_openid}/files",
            ],
        )
        self.assertTrue(
            all(
                route["parameters"]["group_openid"] == "opaque-group"
                for route in routes
            )
        )
        merge_payload = api._http.calls[-1][1]
        self.assertEqual(merge_payload["upload_id"], "upload-1")
        self.assertNotIn("url", merge_payload)
        self.assertFalse(merge_payload["srv_send_msg"])

    async def test_c2c_chunk_routes_use_opaque_user_openid(self):
        api = FakeBotpyApi()
        facade = Botpy2026ApiFacade(api, route_factory=fake_route)

        await facade.post_c2c_upload_prepare(
            openid="opaque-user",
            file_type=1,
            file_size="9",
        )
        await facade.post_c2c_upload_part_finish(
            openid="opaque-user",
            upload_id="upload-1",
            part_index=0,
            block_size="9",
            md5="hash",
        )
        await facade.post_c2c_file(
            openid="opaque-user",
            file_type=1,
            upload_id="upload-1",
            file_name="rank.png",
        )

        routes = [item[0] for item in api._http.calls]
        self.assertEqual(
            [route["path"] for route in routes],
            [
                "/v2/users/{openid}/upload_prepare",
                "/v2/users/{openid}/upload_part_finish",
                "/v2/users/{openid}/files",
            ],
        )
        self.assertTrue(
            all(
                route["parameters"]["openid"] == "opaque-user"
                for route in routes
            )
        )


class OfficialQQSdkFacadeValidationTests(TestCase):
    def test_incompatible_sdk_is_rejected_without_import_or_network(self):
        with self.assertRaises(BotTransportError) as raised:
            Botpy2026ApiFacade(object(), route_factory=fake_route)

        self.assertEqual(raised.exception.code, "official_sdk_incompatible")
        self.assertFalse(raised.exception.retryable)


if __name__ == "__main__":
    main()
