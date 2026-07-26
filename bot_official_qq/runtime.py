"""Guarded qq-botpy runtime wiring for the official QQ transport.

Importing this module has no gateway, credential, database, or send side
effect.  A caller must explicitly validate an enabled configuration and then
call :func:`run_official_qq_bot` before qq-botpy can connect.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from datetime import time
from pathlib import Path
from types import ModuleType
from typing import Any, Mapping

from bot_official_qq.app import OfficialEventOutcome, process_official_event
from bot_official_qq.media_upload import ChunkedOfficialQQMediaUploader
from bot_official_qq.scheduler import OfficialStarsCupDailyScheduler
from bot_official_qq.sdk_facade import Botpy2026ApiFacade
from bot_official_qq.transport import OfficialEventDeduplicator, OfficialQQTransport
from db import connect
from settings import DATABASE_PATH


LOGGER = logging.getLogger(__name__)
GROUP_AND_C2C_INTENT = 1 << 25
_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})


class OfficialRuntimeConfigError(ValueError):
    """Raised before any SDK import or network action when runtime is unsafe."""

    def __init__(self, message: str, *, code: str):
        super().__init__(message)
        self.code = code


def _env_flag(
    environment: Mapping[str, str],
    name: str,
    *,
    default: bool,
) -> bool:
    raw = environment.get(name)
    if raw is None:
        return default
    return str(raw).strip().lower() in _TRUE_VALUES


def _positive_int(
    environment: Mapping[str, str],
    name: str,
    *,
    default: int,
) -> int:
    raw = str(environment.get(name, default)).strip()
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise OfficialRuntimeConfigError(
            "{} 必须是正整数".format(name),
            code="invalid_integer",
        ) from exc
    if value < 1:
        raise OfficialRuntimeConfigError(
            "{} 必须是正整数".format(name),
            code="invalid_integer",
        )
    return value


def _daily_time(
    environment: Mapping[str, str],
    name: str,
    *,
    default: str,
) -> time:
    raw = str(environment.get(name, default)).strip()
    try:
        parsed = time.fromisoformat(raw)
    except ValueError as exc:
        raise OfficialRuntimeConfigError(
            "{} 必须是 HH:MM 或 HH:MM:SS".format(name),
            code="invalid_daily_time",
        ) from exc
    if parsed.tzinfo is not None:
        raise OfficialRuntimeConfigError(
            "{} 不能包含时区；固定使用 Asia/Singapore".format(name),
            code="invalid_daily_time",
        )
    return parsed


@dataclass(frozen=True, slots=True)
class OfficialRuntimeConfig:
    """Validated runtime values.

    ``app_secret`` is deliberately omitted from repr and from
    :meth:`safe_summary`.
    """

    enabled: bool
    app_id: str
    app_secret: str = ""
    sandbox: bool = True
    production_confirmed: bool = False
    database_path: Path = DATABASE_PATH
    http_timeout_seconds: int = 5
    stars_cup_schedule_enabled: bool = False
    stars_cup_group_openid: str = ""
    stars_cup_send_time: time = time(hour=9)
    stars_cup_poll_seconds: int = 30
    stars_cup_retry_seconds: int = 900

    def __repr__(self) -> str:
        return (
            "OfficialRuntimeConfig(enabled={!r}, app_id_configured={!r}, "
            "app_secret_configured={!r}, sandbox={!r}, "
            "production_confirmed={!r}, database_path={!r}, "
            "http_timeout_seconds={!r}, stars_cup_schedule_enabled={!r}, "
            "stars_cup_group_configured={!r}, stars_cup_send_time={!r})"
        ).format(
            self.enabled,
            bool(self.app_id),
            bool(self.app_secret),
            self.sandbox,
            self.production_confirmed,
            self.database_path,
            self.http_timeout_seconds,
            self.stars_cup_schedule_enabled,
            bool(self.stars_cup_group_openid),
            self.stars_cup_send_time,
        )

    @classmethod
    def from_environment(
        cls,
        environment: Mapping[str, str] | None = None,
    ) -> "OfficialRuntimeConfig":
        values = os.environ if environment is None else environment
        enabled = _env_flag(
            values,
            "OFFICIAL_QQ_BOT_ENABLED",
            default=False,
        )
        if not enabled:
            raise OfficialRuntimeConfigError(
                "官方 QQ Bot 默认关闭；需显式设置 OFFICIAL_QQ_BOT_ENABLED=true",
                code="runtime_disabled",
            )
        app_id = str(values.get("OFFICIAL_QQ_APP_ID", "")).strip()
        app_secret = str(values.get("OFFICIAL_QQ_APP_SECRET", "")).strip()
        if not app_id:
            raise OfficialRuntimeConfigError(
                "缺少 OFFICIAL_QQ_APP_ID",
                code="appid_missing",
            )
        if not app_secret:
            raise OfficialRuntimeConfigError(
                "缺少 OFFICIAL_QQ_APP_SECRET",
                code="appsecret_missing",
            )
        sandbox = _env_flag(
            values,
            "OFFICIAL_QQ_BOT_SANDBOX",
            default=True,
        )
        production_confirmed = _env_flag(
            values,
            "OFFICIAL_QQ_BOT_PRODUCTION_CONFIRMED",
            default=False,
        )
        if not sandbox and not production_confirmed:
            raise OfficialRuntimeConfigError(
                "正式环境还需显式设置 OFFICIAL_QQ_BOT_PRODUCTION_CONFIRMED=true",
                code="production_not_confirmed",
            )
        raw_database_path = str(
            values.get("OFFICIAL_QQ_DATABASE_PATH", "")
        ).strip()
        database_path = (
            Path(raw_database_path).expanduser()
            if raw_database_path
            else DATABASE_PATH
        ).resolve()
        if not database_path.is_file():
            raise OfficialRuntimeConfigError(
                "官方 QQ Bot 数据库不存在",
                code="database_missing",
            )
        schedule_enabled = _env_flag(
            values,
            "OFFICIAL_QQ_STARS_CUP_SCHEDULE_ENABLED",
            default=False,
        )
        group_openid = str(
            values.get("OFFICIAL_QQ_STARS_CUP_GROUP_OPENID", "")
        ).strip()
        if schedule_enabled and not group_openid:
            raise OfficialRuntimeConfigError(
                "启用群星杯定时发送时缺少 OFFICIAL_QQ_STARS_CUP_GROUP_OPENID",
                code="schedule_group_missing",
            )
        return cls(
            enabled=True,
            app_id=app_id,
            app_secret=app_secret,
            sandbox=sandbox,
            production_confirmed=production_confirmed,
            database_path=database_path,
            http_timeout_seconds=_positive_int(
                values,
                "OFFICIAL_QQ_HTTP_TIMEOUT_SECONDS",
                default=5,
            ),
            stars_cup_schedule_enabled=schedule_enabled,
            stars_cup_group_openid=group_openid,
            stars_cup_send_time=_daily_time(
                values,
                "OFFICIAL_QQ_STARS_CUP_SEND_TIME",
                default="09:00",
            ),
            stars_cup_poll_seconds=_positive_int(
                values,
                "OFFICIAL_QQ_STARS_CUP_POLL_SECONDS",
                default=30,
            ),
            stars_cup_retry_seconds=_positive_int(
                values,
                "OFFICIAL_QQ_STARS_CUP_RETRY_SECONDS",
                default=900,
            ),
        )

    def safe_summary(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "appid_configured": bool(self.app_id),
            "appsecret_configured": bool(self.app_secret),
            "sandbox": self.sandbox,
            "production_confirmed": self.production_confirmed,
            "database_exists": self.database_path.is_file(),
            "database_name": self.database_path.name,
            "http_timeout_seconds": self.http_timeout_seconds,
            "stars_cup_schedule_enabled": self.stars_cup_schedule_enabled,
            "stars_cup_group_configured": bool(
                self.stars_cup_group_openid
            ),
            "stars_cup_send_time": self.stars_cup_send_time.isoformat(
                timespec="minutes"
            ),
            "stars_cup_poll_seconds": self.stars_cup_poll_seconds,
            "stars_cup_retry_seconds": self.stars_cup_retry_seconds,
            "intent": GROUP_AND_C2C_INTENT,
            "intent_events": [
                "C2C_MESSAGE_CREATE",
                "GROUP_AT_MESSAGE_CREATE",
            ],
        }


class OfficialQQEventRunner:
    """Open one short-lived SQLite connection per inbound event."""

    def __init__(
        self,
        database_path: Path,
        *,
        deduplicator: OfficialEventDeduplicator | None = None,
        transport_factory=None,
        connection_factory=connect,
    ):
        self.database_path = Path(database_path)
        self.deduplicator = deduplicator or OfficialEventDeduplicator()
        self.transport_factory = transport_factory or self._default_transport
        self.connection_factory = connection_factory
        self._api: Any = None
        self._transport: OfficialQQTransport | None = None

    @staticmethod
    def _default_transport(api: Any) -> OfficialQQTransport:
        facade = Botpy2026ApiFacade(api)
        return OfficialQQTransport(
            facade,
            uploader=ChunkedOfficialQQMediaUploader(),
        )

    def transport_for(self, api: Any) -> OfficialQQTransport:
        if self._transport is None or api is not self._api:
            self._api = api
            self._transport = self.transport_factory(api)
        return self._transport

    async def handle(
        self,
        *,
        event_type: str,
        event: Any,
        api: Any,
    ) -> OfficialEventOutcome:
        connection = self.connection_factory(self.database_path)
        try:
            outcome = await process_official_event(
                connection,
                event_type=event_type,
                event=event,
                transport=self.transport_for(api),
                deduplicator=self.deduplicator,
            )
        finally:
            connection.close()
        LOGGER.info(
            "official QQ event handled type=%s ignored=%s duplicate=%s receipts=%s",
            event_type,
            outcome.ignored,
            outcome.duplicate,
            ",".join(receipt.status for receipt in outcome.receipts) or "-",
        )
        return outcome


def _load_botpy() -> ModuleType:
    try:
        import botpy
    except ImportError as exc:
        raise OfficialRuntimeConfigError(
            "未安装 qq-botpy；请使用 requirements-official-qq.txt",
            code="sdk_missing",
        ) from exc
    if not hasattr(botpy, "Client") or not hasattr(botpy, "Intents"):
        raise OfficialRuntimeConfigError(
            "qq-botpy 缺少 Client 或 Intents",
            code="sdk_incompatible",
        )
    return botpy


def create_botpy_client(
    config: OfficialRuntimeConfig,
    *,
    botpy_module: ModuleType | Any | None = None,
    runner: OfficialQQEventRunner | Any | None = None,
    scheduler: OfficialStarsCupDailyScheduler | Any | None = None,
) -> Any:
    """Build the SDK client without starting it."""

    botpy = botpy_module or _load_botpy()
    intents = botpy.Intents.none()
    intents.public_messages = True
    if int(getattr(intents, "value", -1)) != GROUP_AND_C2C_INTENT:
        raise OfficialRuntimeConfigError(
            "qq-botpy public_messages intent 与官方 1<<25 不一致",
            code="intent_mismatch",
        )
    event_runner = runner or OfficialQQEventRunner(config.database_path)
    daily_scheduler = scheduler
    if daily_scheduler is None and config.stars_cup_schedule_enabled:
        daily_scheduler = OfficialStarsCupDailyScheduler(
            group_openid=config.stars_cup_group_openid,
            send_time=config.stars_cup_send_time,
            poll_seconds=config.stars_cup_poll_seconds,
            retry_seconds=config.stars_cup_retry_seconds,
        )
    client_base = botpy.Client

    class StarsCupOfficialQQClient(client_base):
        _stars_cup_scheduler_task = None

        async def on_ready(self):
            LOGGER.info(
                "official QQ gateway ready sandbox=%s intent=%s",
                config.sandbox,
                GROUP_AND_C2C_INTENT,
            )
            if daily_scheduler is not None and (
                self._stars_cup_scheduler_task is None
                or self._stars_cup_scheduler_task.done()
            ):
                transport = event_runner.transport_for(self.api)
                self._stars_cup_scheduler_task = asyncio.create_task(
                    daily_scheduler.run_forever(transport),
                    name="stars-cup-official-qq-daily",
                )

        async def on_c2c_message_create(self, message):
            await event_runner.handle(
                event_type="C2C_MESSAGE_CREATE",
                event=message,
                api=self.api,
            )

        async def on_group_at_message_create(self, message):
            await event_runner.handle(
                event_type="GROUP_AT_MESSAGE_CREATE",
                event=message,
                api=self.api,
            )

        async def on_error(self, event_method, *_args, **_kwargs):
            LOGGER.exception(
                "official QQ callback failed event=%s",
                event_method,
            )

        async def close(self):
            task = self._stars_cup_scheduler_task
            if task is not None and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            await super().close()

    return StarsCupOfficialQQClient(
        intents=intents,
        timeout=config.http_timeout_seconds,
        is_sandbox=config.sandbox,
        ext_handlers=False,
    )


def run_official_qq_bot(
    config: OfficialRuntimeConfig,
    *,
    botpy_module: ModuleType | Any | None = None,
) -> None:
    """Start the blocking SDK gateway loop.

    This is the only function in the package that authorizes a real login.
    qq-botpy 1.2.1 still calls ``asyncio.get_event_loop()`` in its synchronous
    constructor.  Python 3.14 no longer creates that loop implicitly, so this
    boundary owns one explicit loop for the full blocking SDK lifetime.
    """

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass
    else:
        raise OfficialRuntimeConfigError(
            "阻塞式官方 QQ 入口不能在已运行的 asyncio loop 内启动",
            code="runtime_loop_active",
        )
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        client = create_botpy_client(config, botpy_module=botpy_module)
        client.run(appid=config.app_id, secret=config.app_secret)
    finally:
        asyncio.set_event_loop(None)
        loop.close()
