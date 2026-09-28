"""Provodnik — публикации дашбордов из Studio и общая память конструктора.

Публикации (приём сборок из Provodnik Studio, версии, откат) — publications.py.
Ниже — общая память прежнего веб-конструктора, оставлена без изменений.

Заменяет скрытые панели Lovelace: настройки дашборда (блоки, страницы, пресеты,
фон) хранятся в собственном файле `.storage/provodnik.memory` и попадают в
резервные копии Home Assistant. Читать может любой авторизованный пользователь,
писать — только администратор (как было у панелей: чтение всем, сохранение
админу).

Хранимый объект:

    {
      "core": { ... настройки, метаданные страниц и пресетов ... } | None,
      "parts": { "page-1": ..., "presets-1": ..., "img-…": … },
    }

`core` маленький и читается первым (это точка входа); `parts` — крупные куски:
блоки страниц, группы снимков пресетов, картинки фона. Нарезка на части осталась
от прежней схемы (лимит 4 МБ на одно WebSocket-сообщение никуда не делся) — здесь
просто исчезли скрытые панели, вместо них один файл.
"""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import DOMAIN, STORAGE_KEY, STORAGE_VERSION
from .publications import async_setup_publications

# Подключается через «Добавить интеграцию» или строкой `provodnik:` в YAML.
CONFIG_SCHEMA = vol.Schema({vol.Optional(DOMAIN): vol.Any(None, {})}, extra=vol.ALLOW_EXTRA)

_LOGGER = logging.getLogger(__name__)

WS_GET_CORE = f"{DOMAIN}/get_core"
WS_GET_PART = f"{DOMAIN}/get_part"
WS_LIST_PARTS = f"{DOMAIN}/list_parts"
WS_SAVE_CORE = f"{DOMAIN}/save_core"
WS_SAVE_PART = f"{DOMAIN}/save_part"
WS_DELETE_PART = f"{DOMAIN}/delete_part"


class ProvodnikStore:
    """Один JSON-файл на всю общую память дашборда."""

    def __init__(self, hass: HomeAssistant) -> None:
        self._store: Store[dict[str, Any]] = Store(hass, STORAGE_VERSION, STORAGE_KEY)
        self._data: dict[str, Any] | None = None

    async def async_load(self) -> dict[str, Any]:
        if self._data is None:
            loaded = await self._store.async_load()
            self._data = loaded if isinstance(loaded, dict) else {"core": None, "parts": {}}
        return self._data

    async def async_save(self, data: dict[str, Any]) -> None:
        self._data = data
        await self._store.async_save(data)


def _is_admin(connection: websocket_api.ActiveConnection) -> bool:
    user = getattr(connection, "user", None)
    return bool(user is not None and getattr(user, "is_admin", False))


def _deny(connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    connection.send_error(
        msg["id"], websocket_api.const.ERR_UNAUTHORIZED, "Требуются права администратора"
    )


@websocket_api.async_response
async def _handle_get_core(hass: HomeAssistant, connection, msg):
    data = await hass.data[DOMAIN].async_load()
    connection.send_result(msg["id"], {"core": data.get("core")})


@websocket_api.async_response
async def _handle_get_part(hass: HomeAssistant, connection, msg):
    data = await hass.data[DOMAIN].async_load()
    parts = data.get("parts") or {}
    name = msg["name"]
    connection.send_result(msg["id"], {"name": name, "value": parts.get(name)})


@websocket_api.async_response
async def _handle_list_parts(hass: HomeAssistant, connection, msg):
    data = await hass.data[DOMAIN].async_load()
    connection.send_result(msg["id"], {"names": sorted((data.get("parts") or {}).keys())})


@websocket_api.async_response
async def _handle_save_core(hass: HomeAssistant, connection, msg):
    if not _is_admin(connection):
        _deny(connection, msg)
        return
    data = await hass.data[DOMAIN].async_load()
    data["core"] = msg["core"]
    await hass.data[DOMAIN].async_save(data)
    connection.send_result(msg["id"], {"ok": True})


@websocket_api.async_response
async def _handle_save_part(hass: HomeAssistant, connection, msg):
    if not _is_admin(connection):
        _deny(connection, msg)
        return
    data = await hass.data[DOMAIN].async_load()
    parts = dict(data.get("parts") or {})
    parts[msg["name"]] = msg["value"]
    data["parts"] = parts
    await hass.data[DOMAIN].async_save(data)
    connection.send_result(msg["id"], {"ok": True})


@websocket_api.async_response
async def _handle_delete_part(hass: HomeAssistant, connection, msg):
    if not _is_admin(connection):
        _deny(connection, msg)
        return
    data = await hass.data[DOMAIN].async_load()
    parts = dict(data.get("parts") or {})
    parts.pop(msg["name"], None)
    data["parts"] = parts
    await hass.data[DOMAIN].async_save(data)
    connection.send_result(msg["id"], {"ok": True})


def _schema(command: str, **extra: Any) -> vol.Schema:
    return websocket_api.BASE_COMMAND_MESSAGE_SCHEMA.extend(
        {vol.Required("type"): command, **extra}
    )


async def async_setup(hass: HomeAssistant, config: dict[str, Any]) -> bool:
    """Загрузить компонент и зарегистрировать WebSocket-команды."""
    hass.data[DOMAIN] = ProvodnikStore(hass)
    await hass.data[DOMAIN].async_load()
    await async_setup_publications(hass)

    websocket_api.async_register_command(
        hass, WS_GET_CORE, _handle_get_core, _schema(WS_GET_CORE)
    )
    websocket_api.async_register_command(
        hass, WS_GET_PART, _handle_get_part, _schema(WS_GET_PART, name=str)
    )
    websocket_api.async_register_command(
        hass, WS_LIST_PARTS, _handle_list_parts, _schema(WS_LIST_PARTS)
    )
    websocket_api.async_register_command(
        hass, WS_SAVE_CORE, _handle_save_core, _schema(WS_SAVE_CORE, core=object)
    )
    websocket_api.async_register_command(
        hass,
        WS_SAVE_PART,
        _handle_save_part,
        _schema(WS_SAVE_PART, name=str, value=object),
    )
    websocket_api.async_register_command(
        hass, WS_DELETE_PART, _handle_delete_part, _schema(WS_DELETE_PART, name=str)
    )
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Всё регистрируется в async_setup; запись нужна для установки из интерфейса."""
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    return True
