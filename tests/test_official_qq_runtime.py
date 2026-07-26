from __future__ import annotations

import asyncio
import sys
from datetime import datetime, time
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase, TestCase, main
from unittest.mock import AsyncMock, Mock, patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from bot_official_qq.app import OfficialEventOutcome  # noqa: E402
from bot_official_qq.runtime import (  # noqa: E402
    GROUP_AND_C2C_INTENT,
    OfficialQQEventRunner,
    OfficialRuntimeConfig,
    OfficialRuntimeConfigError,
    create_botpy_client,
    preflight_official_qq_bot,
    run_official_qq_bot,
)
from settings import LOCAL_TIMEZONE  # noqa: E402


class FakeIntents:
    def __init__(self):
        self.value = 0

    @classmethod
    def none(cls):
        return cls()

    @property
    def public_messages(self):
        return bool(self.value & GROUP_AND_C2C_INTENT)

    @public_messages.setter
    def public_messages(self, enabled):
        if enabled:
            self.value |= GROUP_AND_C2C_INTENT
        else:
            self.value &= ~GROUP_AND_C2C_INTENT


class FakeClient:
    def __init__(self, *, intents, timeout, is_sandbox, ext_handlers):
        self.intents = intents
        self.timeout = timeout
        self.is_sandbox = is_sandbox
        self.ext_handlers = ext_handlers
        self.api = SimpleNamespace(
            _http=SimpleNamespace(request=AsyncMock()),
            post_group_message=AsyncMock(),
            post_c2c_message=AsyncMock(),
        )
        self.run_calls = []
        self.closed = False

    def run(self, **fields):
        self.run_calls.append(fields)

    async def close(self):
        self.closed = True


class LoopBoundFakeClient(FakeClient):
    def __init__(self, **fields):
        self.constructed_loop = asyncio.get_event_loop()
        super().__init__(**fields)


FAKE_BOTPY = SimpleNamespace(Client=FakeClient, Intents=FakeIntents)
LOOP_BOUND_FAKE_BOTPY = SimpleNamespace(
    Client=LoopBoundFakeClient,
    Intents=FakeIntents,
)


def _config(database_path: Path, **overrides):
    values = {
        "enabled": True,
        "app_id": "app-id",
        "app_secret": "do-not-print-this-secret",
        "sandbox": True,
        "production_confirmed": False,
        "database_path": database_path,
        "http_timeout_seconds": 7,
    }
    values.update(overrides)
    return OfficialRuntimeConfig(**values)


class OfficialRuntimeConfigTests(TestCase):
    def test_disabled_runtime_fails_before_credentials_are_required(self):
        with self.assertRaises(OfficialRuntimeConfigError) as raised:
            OfficialRuntimeConfig.from_environment({})
        self.assertEqual(raised.exception.code, "runtime_disabled")

    def test_production_requires_second_explicit_confirmation(self):
        with TemporaryDirectory() as temp_dir:
            database = Path(temp_dir) / "bot.sqlite3"
            database.touch()
            with self.assertRaises(OfficialRuntimeConfigError) as raised:
                OfficialRuntimeConfig.from_environment(
                    {
                        "OFFICIAL_QQ_BOT_ENABLED": "true",
                        "OFFICIAL_QQ_APP_ID": "app-id",
                        "OFFICIAL_QQ_APP_SECRET": "secret",
                        "OFFICIAL_QQ_BOT_SANDBOX": "false",
                        "OFFICIAL_QQ_DATABASE_PATH": str(database),
                    }
                )
        self.assertEqual(raised.exception.code, "production_not_confirmed")

    def test_safe_summary_and_repr_never_expose_credentials(self):
        with TemporaryDirectory() as temp_dir:
            database = Path(temp_dir) / "bot.sqlite3"
            database.touch()
            config = _config(database)
            rendered = repr(config)
            summary = str(config.safe_summary())
        self.assertNotIn(config.app_id, rendered)
        self.assertNotIn(config.app_secret, rendered)
        self.assertNotIn(config.app_id, summary)
        self.assertNotIn(config.app_secret, summary)
        self.assertEqual(config.safe_summary()["intent"], 1 << 25)

    def test_config_check_does_not_import_sdk_or_start_network(self):
        with TemporaryDirectory() as temp_dir:
            database = Path(temp_dir) / "bot.sqlite3"
            database.touch()
            environment = {
                "OFFICIAL_QQ_BOT_ENABLED": "true",
                "OFFICIAL_QQ_APP_ID": "app-id",
                "OFFICIAL_QQ_APP_SECRET": "secret",
                "OFFICIAL_QQ_DATABASE_PATH": str(database),
            }
            with patch.dict("os.environ", environment, clear=True), patch(
                "bot_official_qq.runtime._load_botpy"
            ) as load_sdk:
                config = OfficialRuntimeConfig.from_environment()
                summary = config.safe_summary()
        load_sdk.assert_not_called()
        self.assertTrue(summary["database_exists"])

    def test_schedule_requires_target_and_summary_hides_openid(self):
        with TemporaryDirectory() as temp_dir:
            database = Path(temp_dir) / "bot.sqlite3"
            database.touch()
            base = {
                "OFFICIAL_QQ_BOT_ENABLED": "true",
                "OFFICIAL_QQ_APP_ID": "app-id",
                "OFFICIAL_QQ_APP_SECRET": "secret",
                "OFFICIAL_QQ_DATABASE_PATH": str(database),
                "OFFICIAL_QQ_STARS_CUP_SCHEDULE_ENABLED": "true",
            }
            with self.assertRaises(OfficialRuntimeConfigError) as raised:
                OfficialRuntimeConfig.from_environment(base)
            self.assertEqual(raised.exception.code, "schedule_group_missing")
            target = "opaque-private-group-openid"
            config = OfficialRuntimeConfig.from_environment(
                {
                    **base,
                    "OFFICIAL_QQ_STARS_CUP_GROUP_OPENID": target,
                    "OFFICIAL_QQ_STARS_CUP_SEND_TIME": "21:30",
                }
            )
        summary = str(config.safe_summary())
        self.assertTrue(config.stars_cup_schedule_enabled)
        self.assertEqual(config.stars_cup_send_time, time(hour=21, minute=30))
        self.assertNotIn(target, summary)


class OfficialRuntimeClientTests(IsolatedAsyncioTestCase):
    async def test_client_subscribes_only_group_and_c2c_and_routes_callbacks(self):
        with TemporaryDirectory() as temp_dir:
            database = Path(temp_dir) / "bot.sqlite3"
            database.touch()
            runner = SimpleNamespace(handle=AsyncMock())
            client = create_botpy_client(
                _config(database),
                botpy_module=FAKE_BOTPY,
                runner=runner,
            )
            c2c = object()
            group = object()
            await client.on_c2c_message_create(c2c)
            await client.on_group_at_message_create(group)

        self.assertEqual(client.intents.value, 1 << 25)
        self.assertEqual(client.timeout, 7)
        self.assertTrue(client.is_sandbox)
        self.assertFalse(client.ext_handlers)
        self.assertEqual(
            [call.kwargs["event_type"] for call in runner.handle.await_args_list],
            ["C2C_MESSAGE_CREATE", "GROUP_AT_MESSAGE_CREATE"],
        )
        self.assertIs(runner.handle.await_args_list[0].kwargs["event"], c2c)
        self.assertIs(runner.handle.await_args_list[1].kwargs["event"], group)

    async def test_ready_starts_one_guarded_scheduler_and_close_cancels_it(self):
        with TemporaryDirectory() as temp_dir:
            database = Path(temp_dir) / "bot.sqlite3"
            database.touch()
            config = _config(
                database,
                stars_cup_schedule_enabled=True,
                stars_cup_group_openid="opaque-group",
            )
            runner = SimpleNamespace(
                handle=AsyncMock(),
                transport_for=Mock(return_value=object()),
            )
            started = asyncio.Event()

            async def run_forever(_transport):
                started.set()
                await asyncio.Event().wait()

            scheduler = SimpleNamespace(run_forever=run_forever)
            client = create_botpy_client(
                config,
                botpy_module=FAKE_BOTPY,
                runner=runner,
                scheduler=scheduler,
            )
            await client.on_ready()
            await started.wait()
            first_task = client._stars_cup_scheduler_task
            await client.on_ready()
            self.assertIs(client._stars_cup_scheduler_task, first_task)
            runner.transport_for.assert_called_once_with(client.api)
            await client.close()

        self.assertTrue(first_task.cancelled())
        self.assertTrue(client.closed)

    async def test_event_runner_always_closes_short_lived_database_connection(self):
        connection = Mock()
        transport = object()
        runner = OfficialQQEventRunner(
            Path("/unused.sqlite3"),
            connection_factory=Mock(return_value=connection),
            transport_factory=Mock(return_value=transport),
        )
        expected = OfficialEventOutcome(
            inbound=None,
            reply=None,
            ignored=True,
        )
        with patch(
            "bot_official_qq.runtime.process_official_event",
            new=AsyncMock(return_value=expected),
        ) as processor:
            result = await runner.handle(
                event_type="C2C_MESSAGE_CREATE",
                event=object(),
                api=object(),
            )

        self.assertIs(result, expected)
        connection.close.assert_called_once_with()
        processor.assert_awaited_once()

    async def test_event_runner_closes_connection_when_processor_raises(self):
        connection = Mock()
        runner = OfficialQQEventRunner(
            Path("/unused.sqlite3"),
            connection_factory=Mock(return_value=connection),
            transport_factory=Mock(return_value=object()),
        )
        with patch(
            "bot_official_qq.runtime.process_official_event",
            new=AsyncMock(side_effect=RuntimeError("boom")),
        ):
            with self.assertRaises(RuntimeError):
                await runner.handle(
                    event_type="GROUP_AT_MESSAGE_CREATE",
                    event=object(),
                    api=object(),
                )
        connection.close.assert_called_once_with()

    def test_run_owns_python_314_compatible_loop_and_passes_credentials(self):
        with TemporaryDirectory() as temp_dir:
            database = Path(temp_dir) / "bot.sqlite3"
            database.touch()
            config = _config(database)
            created = []

            class RecordingClient(LoopBoundFakeClient):
                def __init__(self, **fields):
                    super().__init__(**fields)
                    created.append(self)

            module = SimpleNamespace(
                Client=RecordingClient,
                Intents=FakeIntents,
            )
            run_official_qq_bot(config, botpy_module=module)
            client = created[0]

        self.assertTrue(client.constructed_loop.is_closed())
        self.assertEqual(
            client.run_calls,
            [{"appid": config.app_id, "secret": config.app_secret}],
        )


class OfficialRuntimePreflightTests(TestCase):
    def test_preflight_constructs_sdk_without_run_or_network(self):
        with TemporaryDirectory() as temp_dir:
            database = Path(temp_dir) / "bot.sqlite3"
            database.touch()
            summary = preflight_official_qq_bot(
                _config(database),
                botpy_module=FAKE_BOTPY,
            )

        self.assertFalse(summary["network_started"])
        self.assertTrue(summary["sdk"]["client_constructed"])
        self.assertTrue(summary["sdk"]["api_facade_compatible"])
        self.assertEqual(summary["sdk"]["intent"], 1 << 25)
        self.assertFalse(summary["stars_cup_snapshot"]["checked"])

    def test_schedule_preflight_checks_snapshot_without_exposing_paths(self):
        with TemporaryDirectory() as temp_dir:
            database = Path(temp_dir) / "bot.sqlite3"
            database.touch()
            loaded = SimpleNamespace(
                run_id="20260727",
                published_at=datetime(
                    2026,
                    7,
                    27,
                    9,
                    0,
                    tzinfo=LOCAL_TIMEZONE,
                ),
                stale=False,
            )
            summary = preflight_official_qq_bot(
                _config(
                    database,
                    stars_cup_schedule_enabled=True,
                    stars_cup_group_openid="opaque-private-group",
                ),
                botpy_module=FAKE_BOTPY,
                snapshot_loader=Mock(return_value=loaded),
            )

        snapshot = summary["stars_cup_snapshot"]
        self.assertTrue(snapshot["checked"])
        self.assertEqual(snapshot["run_id"], "20260727")
        self.assertNotIn("path", str(snapshot).lower())
        self.assertNotIn("opaque-private-group", str(summary))


if __name__ == "__main__":
    main()
