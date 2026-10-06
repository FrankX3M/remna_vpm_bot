"""
Тонкий клиент Remnawave REST API.

Написан под линейку панели 2.x (PATCH /api/users с uuid в теле, DELETE /api/users/{uuid}).
Если у вас 3.x или поля отличаются — сверяйтесь со Swagger вашей панели и правьте ТОЛЬКО этот файл:
остальной код вызывает лишь методы ниже.
"""
from __future__ import annotations

import logging
from datetime import datetime

import httpx

from .utils import iso

log = logging.getLogger(__name__)


class RemnawaveError(Exception):
    def __init__(self, status: int, detail: str = ""):
        super().__init__(f"Remnawave API error {status}: {detail}")
        self.status = status
        self.detail = detail


class RemnawaveClient:
    def __init__(self, base_url: str, token: str, behind_proxy: bool = False, timeout: float = 15.0):
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        if behind_proxy:
            headers["X-Forwarded-For"] = "127.0.0.1"
            headers["X-Forwarded-Proto"] = "https"
        self._http = httpx.AsyncClient(base_url=base_url.rstrip("/"), headers=headers, timeout=timeout)

    async def close(self) -> None:
        await self._http.aclose()

    async def _request(self, method: str, path: str, *, json: dict | None = None, ok_404: bool = False):
        try:
            r = await self._http.request(method, path, json=json)
        except httpx.HTTPError as e:
            raise RemnawaveError(0, str(e)) from e
        if r.status_code == 404 and ok_404:
            return None
        if r.status_code >= 400:
            log.error("Remnawave %s %s -> %s %s", method, path, r.status_code, r.text[:500])
            raise RemnawaveError(r.status_code, r.text[:500])
        if not r.content:
            return None
        data = r.json()
        return data.get("response", data) if isinstance(data, dict) else data

    async def ping(self) -> None:
        """Проверка доступности панели и валидности токена (читает одного пользователя)."""
        await self._request("GET", "/api/users?size=1&start=0")

    async def get_user(self, uuid: str) -> dict | None:
        return await self._request("GET", f"/api/users/{uuid}", ok_404=True)

    async def find_by_username(self, username: str) -> dict | None:
        return await self._request("GET", f"/api/users/by-username/{username}", ok_404=True)

    async def create_user(
        self,
        *,
        username: str,
        expire_at: datetime,
        telegram_id: int | None = None,
        traffic_limit_bytes: int = 0,
        traffic_strategy: str = "NO_RESET",
        squads: list[str] | None = None,
        hwid_limit: int | None = None,
    ) -> dict:
        body: dict = {
            "username": username,
            "expireAt": iso(expire_at),
            "trafficLimitBytes": traffic_limit_bytes,
            "trafficLimitStrategy": traffic_strategy,
        }
        if telegram_id is not None:
            body["telegramId"] = telegram_id
        if squads:
            body["activeInternalSquads"] = squads
        if hwid_limit:
            body["hwidDeviceLimit"] = hwid_limit
        return await self._request("POST", "/api/users", json=body)

    async def update_user(self, uuid: str, **fields) -> dict:
        return await self._request("PATCH", "/api/users", json={"uuid": uuid, **fields})

    async def delete_user(self, uuid: str) -> None:
        await self._request("DELETE", f"/api/users/{uuid}", ok_404=True)

    async def revoke_subscription(self, uuid: str) -> dict:
        return await self._request("POST", f"/api/users/{uuid}/actions/revoke", json={})
