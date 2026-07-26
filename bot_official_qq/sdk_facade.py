from __future__ import annotations

from typing import Any, Callable

from services.bot_transport import BotTransportError


RouteFactory = Callable[..., Any]


def _default_route_factory(method: str, path: str, **parameters: Any) -> Any:
    try:
        from botpy.http import Route
    except ImportError as exc:
        raise BotTransportError(
            "qq-botpy is not installed",
            code="official_sdk_missing",
            retryable=False,
        ) from exc
    return Route(method, path, **parameters)


def _without_none(fields: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in fields.items() if value is not None}


class Botpy2026ApiFacade:
    """Contain the temporary gap between qq-botpy 1.2.1 and the 2026 API.

    Message calls use botpy's public methods.  The new chunked-upload routes
    go through botpy's authenticated HTTP client until the SDK publishes
    equivalent public methods.  No credential is read or stored here.
    """

    def __init__(
        self,
        botpy_api: Any,
        *,
        route_factory: RouteFactory | None = None,
    ):
        http = getattr(botpy_api, "_http", None)
        if http is None or not callable(getattr(http, "request", None)):
            raise BotTransportError(
                "qq-botpy API has no compatible HTTP client",
                code="official_sdk_incompatible",
                retryable=False,
            )
        self._api = botpy_api
        self._http = http
        self._route_factory = route_factory or _default_route_factory

    async def _post(
        self,
        path: str,
        *,
        route_parameters: dict[str, Any],
        payload: dict[str, Any],
    ) -> Any:
        route = self._route_factory("POST", path, **route_parameters)
        return await self._http.request(route, json=_without_none(payload))

    async def post_group_message(self, **fields: Any) -> Any:
        return await self._api.post_group_message(**fields)

    async def post_c2c_message(self, **fields: Any) -> Any:
        return await self._api.post_c2c_message(**fields)

    async def post_group_file(
        self,
        *,
        group_openid: str,
        file_type: int,
        url: str | None = None,
        srv_send_msg: bool = False,
        file_name: str | None = None,
        upload_id: str | None = None,
    ) -> Any:
        return await self._post(
            "/v2/groups/{group_openid}/files",
            route_parameters={"group_openid": group_openid},
            payload={
                "file_type": file_type,
                "url": url,
                "srv_send_msg": srv_send_msg,
                "file_name": file_name,
                "upload_id": upload_id,
            },
        )

    async def post_c2c_file(
        self,
        *,
        openid: str,
        file_type: int,
        url: str | None = None,
        srv_send_msg: bool = False,
        file_name: str | None = None,
        upload_id: str | None = None,
    ) -> Any:
        return await self._post(
            "/v2/users/{openid}/files",
            route_parameters={"openid": openid},
            payload={
                "file_type": file_type,
                "url": url,
                "srv_send_msg": srv_send_msg,
                "file_name": file_name,
                "upload_id": upload_id,
            },
        )

    async def post_group_upload_prepare(
        self,
        *,
        group_openid: str,
        **fields: Any,
    ) -> Any:
        return await self._post(
            "/v2/groups/{group_openid}/upload_prepare",
            route_parameters={"group_openid": group_openid},
            payload=fields,
        )

    async def post_group_upload_part_finish(
        self,
        *,
        group_openid: str,
        **fields: Any,
    ) -> Any:
        return await self._post(
            "/v2/groups/{group_openid}/upload_part_finish",
            route_parameters={"group_openid": group_openid},
            payload=fields,
        )

    async def post_c2c_upload_prepare(
        self,
        *,
        openid: str,
        **fields: Any,
    ) -> Any:
        return await self._post(
            "/v2/users/{openid}/upload_prepare",
            route_parameters={"openid": openid},
            payload=fields,
        )

    async def post_c2c_upload_part_finish(
        self,
        *,
        openid: str,
        **fields: Any,
    ) -> Any:
        return await self._post(
            "/v2/users/{openid}/upload_part_finish",
            route_parameters={"openid": openid},
            payload=fields,
        )
