"""Публикации: загрузка, раздача, права, версии, откат, подписка."""
import base64
from pathlib import Path

import pytest
from homeassistant import config_entries
from homeassistant.setup import async_setup_component

from custom_components.provodnik import publish

DOMAIN = "provodnik"


def build(html: str, **extra: bytes) -> dict:
    files = {"index.html": html.encode(), **extra}
    return {"files": {k: base64.b64encode(v).decode() for k, v in files.items()}, "note": "тест"}


@pytest.fixture
async def setup(hass, tmp_path):
    # Папка конфигурации тестового HA общая между запусками — публикации пишем во временную.
    hass.config.config_dir = str(tmp_path)
    assert await async_setup_component(hass, DOMAIN, {DOMAIN: {}})
    await hass.async_block_till_done()


async def user_client(hass, hass_client, user):
    refresh = await hass.auth.async_create_refresh_token(user, "http://test/")
    return await hass_client(hass.auth.async_create_access_token(refresh))


async def test_publish_and_serve(hass, setup, hass_client, hass_client_no_auth):
    admin = await hass_client()
    r = await admin.post("/api/provodnik/publish/sonoff", json=build("<p>v1</p>", **{"img/a.png": b"PNG"}))
    assert r.status == 200, await r.text()
    body = await r.json()
    assert body["path"] == "/provodnik/sonoff/"
    assert publish.VERSION_RE.match(body["version"])

    anon = await hass_client_no_auth()
    r = await anon.get("/provodnik/sonoff/")
    assert r.status == 200 and await r.text() == "<p>v1</p>"
    assert r.headers["Cache-Control"] == "no-cache"
    assert (await (await anon.get("/provodnik/sonoff/img/a.png")).read()) == b"PNG"
    r = await anon.get("/provodnik/sonoff", allow_redirects=False)
    assert r.status == 302 and r.headers["Location"] == "/provodnik/sonoff/"
    for bad in ("/provodnik/sonoff/../../secrets.yaml", "/provodnik/sonoff/%2e%2e/x", "/provodnik/nope/", "/provodnik/sonoff/missing.js"):
        assert (await anon.get(bad)).status == 404, bad
    assert (Path(hass.config.path(DOMAIN)) / "sonoff" / body["version"] / "index.html").is_file()


async def test_publish_requires_admin(hass, setup, hass_client, hass_client_no_auth, hass_read_only_user):
    viewer = await user_client(hass, hass_client, hass_read_only_user)
    assert (await viewer.post("/api/provodnik/publish/x", json=build("x"))).status == 403
    anon = await hass_client_no_auth()
    assert (await anon.post("/api/provodnik/publish/x", json=build("x"))).status == 401


@pytest.mark.parametrize("slug,payload", [
    ("Bad Slug", build("x")),
    ("ok", {"files": {"page.html": base64.b64encode(b"x").decode()}}),
    ("ok", build("x", **{"../evil.html": b"x"})),
    ("ok", build("x", **{"/abs.html": b"x"})),
    ("ok", build("x", **{"run.py": b"x"})),
    ("ok", {"files": {"index.html": "не base64!"}}),
    ("ok", {"files": []}),
])
async def test_rejects_bad_builds(hass, setup, hass_client, slug, payload):
    admin = await hass_client()
    r = await admin.post(f"/api/provodnik/publish/{slug}", json=payload)
    assert r.status in (400, 404), await r.text()
    assert not (Path(hass.config.path(DOMAIN)) / "ok").exists() or not any((Path(hass.config.path(DOMAIN)) / "ok").iterdir())


async def test_versions_rollback_and_subscribe(hass, setup, hass_client, hass_client_no_auth, hass_ws_client, hass_read_only_user, hass_read_only_access_token):
    admin = await hass_client()
    anon = await hass_client_no_auth()
    v1 = (await (await admin.post("/api/provodnik/publish/room", json=build("v1"))).json())["version"]

    # Подписка доступна обычному пользователю — так планшет узнаёт об обновлении.
    viewer_ws = await hass_ws_client(hass, hass_read_only_access_token)
    await viewer_ws.send_json({"id": 1, "type": "provodnik/publications/subscribe", "slug": "room"})
    assert (await viewer_ws.receive_json())["success"]
    assert (await viewer_ws.receive_json())["event"] == {"version": v1}

    v2 = (await (await admin.post("/api/provodnik/publish/room", json=build("v2"))).json())["version"]
    assert (await viewer_ws.receive_json())["event"] == {"version": v2}
    assert await (await anon.get("/provodnik/room/")).text() == "v2"

    ws = await hass_ws_client(hass)
    await ws.send_json({"id": 1, "type": "provodnik/publications/list"})
    listed = (await ws.receive_json())["result"]["publications"]
    assert [p["slug"] for p in listed] == ["room"]
    assert listed[0]["active"] == v2 and [v["id"] for v in listed[0]["versions"]] == [v2, v1]

    await ws.send_json({"id": 2, "type": "provodnik/publications/activate", "slug": "room", "version": v1})
    assert (await ws.receive_json())["success"]
    assert await (await anon.get("/provodnik/room/")).text() == "v1"
    assert (await viewer_ws.receive_json())["event"] == {"version": v1}

    await ws.send_json({"id": 3, "type": "provodnik/publications/activate", "slug": "room", "version": "20000101-000000-dead"})
    assert not (await ws.receive_json())["success"]

    # Список, откат и удаление — только администратору.
    await viewer_ws.send_json({"id": 2, "type": "provodnik/publications/list"})
    assert (await viewer_ws.receive_json())["error"]["code"] == "unauthorized"

    await ws.send_json({"id": 4, "type": "provodnik/publications/delete", "slug": "room"})
    assert (await ws.receive_json())["success"]
    assert (await viewer_ws.receive_json())["event"] == {"version": None}
    assert (await anon.get("/provodnik/room/")).status == 404
    assert not (Path(hass.config.path(DOMAIN)) / "room").exists()


async def test_keeps_last_versions(hass, setup, hass_client, monkeypatch):
    admin = await hass_client()
    ids = iter(f"20260928-0000{i:02d}-abcd" for i in range(20))
    monkeypatch.setattr(publish, "new_version_id", lambda: next(ids))
    for i in range(publish.KEEP_VERSIONS + 2):
        assert (await admin.post("/api/provodnik/publish/many", json=build(f"v{i}"))).status == 200
    folder = Path(hass.config.path(DOMAIN)) / "many"
    assert len([p for p in folder.iterdir()]) == publish.KEEP_VERSIONS
    assert not (folder / "20260928-000000-abcd").exists()


async def test_config_flow_single_instance(hass):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert result["type"] == "form"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] == "create_entry"
    await hass.async_block_till_done()
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert result["type"] == "abort"
