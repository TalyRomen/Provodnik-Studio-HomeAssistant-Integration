"""Публикации дашбордов из Provodnik Studio.

    POST /api/provodnik/publish/<slug>   загрузка сборки (только администратор)
    GET  /provodnik/<slug>/…             активная версия (без авторизации, как /local)

WebSocket:
    provodnik/publications/list          список публикаций и версий (администратор)
    provodnik/publications/activate      сделать активной другую версию (администратор)
    provodnik/publications/delete        удалить публикацию целиком (администратор)
    provodnik/publications/subscribe     активная версия и её смена (любой пользователь)

Файлы лежат в /config/provodnik/<slug>/<version>/ и попадают в резервные копии.
Какая версия активна — в `.storage/provodnik.publications`. Новая версия
становится активной только после того, как записана целиком.
"""
from __future__ import annotations

import base64
import binascii
import logging
import time
from pathlib import Path
from typing import Any

import voluptuous as vol
from aiohttp import web

from homeassistant.components import websocket_api
from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect, async_dispatcher_send
from homeassistant.helpers.storage import Store

from . import publish
from .const import DOMAIN, PUBLICATIONS_KEY, STORAGE_VERSION

_LOGGER = logging.getLogger(__name__)
DATA = f"{DOMAIN}_publications"


def _signal(slug: str) -> str:
    return f"{DOMAIN}_publication_{slug}"


class Publications:
    """Реестр публикаций: версии, активная версия, запись файлов."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self.root = Path(hass.config.path(DOMAIN))
        self._store: Store[dict[str, Any]] = Store(hass, STORAGE_VERSION, PUBLICATIONS_KEY)
        self.data: dict[str, Any] = {"publications": {}}

    async def async_load(self) -> None:
        loaded = await self._store.async_load()
        if isinstance(loaded, dict) and isinstance(loaded.get("publications"), dict):
            self.data = loaded

    @property
    def items(self) -> dict[str, Any]:
        return self.data["publications"]

    def active(self, slug: str) -> str | None:
        return (self.items.get(slug) or {}).get("active")

    async def async_publish(self, slug: str, files: dict[str, bytes], note: str, user: str | None) -> dict[str, Any]:
        publish.check_slug(slug)
        size = publish.check_files(files)
        version = publish.new_version_id()
        await self.hass.async_add_executor_job(publish.write_version, self.root, slug, files, version)
        entry = self.items.setdefault(slug, {"active": None, "versions": []})
        entry["versions"].insert(0, {
            "id": version, "created": time.time(), "size": size,
            "files": len(files), "note": note[:200], "user": user,
        })
        entry["active"] = version
        # Старые версии удаляем, активную не трогаем никогда.
        stale = entry["versions"][publish.KEEP_VERSIONS:]
        entry["versions"] = entry["versions"][: publish.KEEP_VERSIONS]
        await self._store.async_save(self.data)
        for old in stale:
            await self.hass.async_add_executor_job(publish.remove_version, self.root, slug, old["id"])
        async_dispatcher_send(self.hass, _signal(slug), version)
        return {"slug": slug, "version": version, "path": f"/{DOMAIN}/{slug}/"}

    async def async_activate(self, slug: str, version: str) -> None:
        entry = self.items.get(slug)
        if not entry or not any(v["id"] == version for v in entry["versions"]):
            raise publish.PublishError("Нет такой версии")
        entry["active"] = version
        await self._store.async_save(self.data)
        async_dispatcher_send(self.hass, _signal(slug), version)

    async def async_delete(self, slug: str) -> None:
        if self.items.pop(slug, None) is None:
            raise publish.PublishError("Нет такой публикации")
        await self._store.async_save(self.data)
        await self.hass.async_add_executor_job(publish.remove_publication, self.root, slug)
        async_dispatcher_send(self.hass, _signal(slug), None)


class PublishView(HomeAssistantView):
    """Приём сборки из Studio: JSON {"files": {путь: base64}, "note": "…"}."""

    url = "/api/provodnik/publish/{slug}"
    name = "api:provodnik:publish"
    requires_auth = True

    def __init__(self, registry: Publications) -> None:
        self.registry = registry

    async def post(self, request: web.Request, slug: str) -> web.Response:
        user = request.get("hass_user")
        if user is None or not user.is_admin:
            return self.json_message("Публиковать может только администратор", 403)
        try:
            body = await request.json()
            raw = body.get("files") if isinstance(body, dict) else None
            if not isinstance(raw, dict):
                raise publish.PublishError("Ожидается объект files")
            files = {path: base64.b64decode(data, validate=True) for path, data in raw.items()}
            note = body.get("note") if isinstance(body.get("note"), str) else ""
            result = await self.registry.async_publish(slug, files, note, user.name)
        except (publish.PublishError, binascii.Error, ValueError, TypeError) as err:
            return self.json_message(str(err) or "Некорректная сборка", 400)
        _LOGGER.info("Опубликована %s версии %s (%s)", slug, result["version"], user.name)
        return self.json(result)


class ServeView(HomeAssistantView):
    """Файлы активной версии. Как и /local, без авторизации: секретов в сборке нет."""

    url = "/provodnik/{slug}/{path:.*}"
    extra_urls = ["/provodnik/{slug}"]
    name = "provodnik:serve"
    requires_auth = False

    def __init__(self, registry: Publications) -> None:
        self.registry = registry

    async def get(self, request: web.Request, slug: str, path: str | None = None) -> web.StreamResponse:
        if path is None:  # без завершающего «/» относительные ссылки сборки сломаются
            raise web.HTTPFound(f"/{DOMAIN}/{slug}/")
        version = self.registry.active(slug)
        if not version:
            raise web.HTTPNotFound()
        target = await request.app["hass"].async_add_executor_job(
            publish.resolve, self.registry.root, slug, version, path
        )
        if target is None:
            raise web.HTTPNotFound()
        return web.FileResponse(target, headers={"Cache-Control": "no-cache"})


def _registry(hass: HomeAssistant) -> Publications:
    return hass.data[DATA]


@websocket_api.websocket_command({vol.Required("type"): "provodnik/publications/list"})
@websocket_api.require_admin
@callback
def _ws_list(hass: HomeAssistant, connection, msg) -> None:
    items = _registry(hass).items
    connection.send_result(msg["id"], {
        "publications": [{"slug": slug, **entry} for slug, entry in sorted(items.items())]
    })


@websocket_api.websocket_command({
    vol.Required("type"): "provodnik/publications/activate",
    vol.Required("slug"): str, vol.Required("version"): str,
})
@websocket_api.require_admin
@websocket_api.async_response
async def _ws_activate(hass: HomeAssistant, connection, msg) -> None:
    try:
        await _registry(hass).async_activate(msg["slug"], msg["version"])
    except publish.PublishError as err:
        connection.send_error(msg["id"], "not_found", str(err))
        return
    connection.send_result(msg["id"], {"active": msg["version"]})


@websocket_api.websocket_command({vol.Required("type"): "provodnik/publications/delete", vol.Required("slug"): str})
@websocket_api.require_admin
@websocket_api.async_response
async def _ws_delete(hass: HomeAssistant, connection, msg) -> None:
    try:
        await _registry(hass).async_delete(msg["slug"])
    except publish.PublishError as err:
        connection.send_error(msg["id"], "not_found", str(err))
        return
    connection.send_result(msg["id"])


@websocket_api.websocket_command({vol.Required("type"): "provodnik/publications/subscribe", vol.Required("slug"): str})
@callback
def _ws_subscribe(hass: HomeAssistant, connection, msg) -> None:
    """Дашборд узнаёт активную версию и перезагружается, когда она меняется."""

    @callback
    def forward(version: str | None) -> None:
        connection.send_message(websocket_api.event_message(msg["id"], {"version": version}))

    connection.subscriptions[msg["id"]] = async_dispatcher_connect(hass, _signal(msg["slug"]), forward)
    connection.send_result(msg["id"])
    forward(_registry(hass).active(msg["slug"]))


async def async_setup_publications(hass: HomeAssistant) -> None:
    registry = Publications(hass)
    await registry.async_load()
    hass.data[DATA] = registry
    hass.http.register_view(PublishView(registry))
    hass.http.register_view(ServeView(registry))
    for command in (_ws_list, _ws_activate, _ws_delete, _ws_subscribe):
        websocket_api.async_register_command(hass, command)
