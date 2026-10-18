"""Regression coverage for combining upstream 1.4 with the fork adapters."""

from unittest.mock import patch

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestServer
import pytest
from yarl import URL

from custom_components.ingress import CONFIG_SCHEMA, _parse_config, setup_domain
from custom_components.ingress.config import IngressStore
from custom_components.ingress.config_flow import _panel_form, _panel_from_form, _panel_schema
from custom_components.ingress.const import DOMAIN
from custom_components.ingress.ingress import IngressView
from custom_components.ingress.tls import tls_context


@pytest.mark.parametrize("adapter", ["unifi", "unraid", "generic", "proxmox"])
def test_existing_yaml_and_ui_roundtrip(hass, adapter):
    saved = dict(url="https://localhost", title="App", adapter=adapter,
                 username="test-user", password="test-password", verify_ssl=False)
    validated = CONFIG_SCHEMA({DOMAIN: {"app": saved}})
    cfgs, _ = _parse_config(hass, validated)
    assert cfgs[0].ssl is False
    assert cfgs[0].adapter == adapter
    assert cfgs[0].username == saved["username"]
    form = _panel_form(saved)
    checked = _panel_schema(editing=True)(form)
    restored = _panel_from_form(checked)
    assert restored["adapter"] == adapter
    assert restored["password"] == saved["password"]
    assert tls_context(restored) is False
    assert "verify_ssl" not in restored


def test_explicit_tls_takes_precedence():
    assert tls_context(dict(url="https://localhost", verify_ssl=False, tls_mode="system")) is True
    assert tls_context(dict(url="http://localhost", verify_ssl=False)) is False


@pytest.fixture
async def proxy_factory(hass, aiohttp_client, socket_enabled):
    sessions = []
    servers = []

    async def create(handler, **settings):
        backend = web.Application()
        backend.router.add_route("*", "/{path:.*}", handler)
        server = TestServer(backend)
        await server.start_server()
        servers.append(server)
        config = CONFIG_SCHEMA({DOMAIN: {"app": {
            "url": str(server.make_url("/")), "static_token": "test-token", **settings,
        }}})
        store = IngressStore()
        await setup_domain(hass, {"config": store, "panels": set()}, config)
        session = aiohttp.ClientSession(cookie_jar=aiohttp.DummyCookieJar())
        sessions.append(session)
        view = IngressView(hass, store, session)
        app = web.Application()

        async def proxy(request):
            return await view._handle(request, **request.match_info)

        app.router.add_route("*", "/api/ingress/{name}/{path:.*}", proxy)
        client = await aiohttp_client(app)
        client.session.cookie_jar.update_cookies({"ingress_token": "t-test-token"})
        return client, store.get("app"), session

    yield create
    for session in sessions:
        await session.close()
    for server in servers:
        await server.close()


@pytest.mark.parametrize("adapter", ["unifi", "unraid", "generic", "proxmox"])
async def test_adapter_rewrite_and_compression(proxy_factory, adapter):
    async def backend(request):
        return web.Response(text='<html><head></head><body><a href="/Main">App</a></body></html>',
                            content_type="text/html", headers={"ETag": '"old"'})

    client, _, _ = await proxy_factory(backend, adapter=adapter, enable_compress=True)
    response = await client.get("/api/ingress/app/", headers={"Accept-Encoding": "gzip"})
    assert response.status == 200
    body = await response.text()
    assert '/api/ingress/app/Main' in body
    assert response.headers["Content-Encoding"] == "gzip"
    assert "ETag" not in response.headers


@pytest.mark.parametrize("adapter,cookie,login_path", [
    ("unifi", "TOKEN", "/api/auth/login"), ("unraid", "PHPSESSID", "/login"),
])
async def test_private_login_retry_uses_resolved_tls(proxy_factory, adapter, cookie, login_path):
    logins = 0

    async def backend(request):
        nonlocal logins
        if request.path == login_path:
            logins += 1
            response = web.Response(text="ok")
            response.set_cookie(cookie, str(logins))
            return response
        if request.cookies.get(cookie) != "2":
            if adapter == "unifi":
                return web.Response(status=401)
            return web.Response(status=302, headers={"Location": "/login"})
        return web.Response(text='<html><head></head><body>logged in</body></html>',
                            content_type="text/html")

    client, cfg, session = await proxy_factory(backend, adapter=adapter, username="test",
                                               password="test", verify_ssl=False,
                                               enable_compress=True)
    with patch.object(session, "_request", wraps=session._request) as calls:
        response = await client.get("/api/ingress/app/")
        assert response.status == 200
        assert "logged in" in await response.text()
        assert logins == 2
        assert len(calls.call_args_list) == 4
        assert all(call.kwargs["ssl"] is False for call in calls.call_args_list)
    assert cfg.adapter_cookies[cookie] == "2"
    assert not list(session.cookie_jar)


async def test_interactive_unifi_cookie_is_not_shared(proxy_factory):
    received = []

    async def backend(request):
        received.append(request.cookies.get("TOKEN"))
        response = web.Response(text="ok")
        response.set_cookie("TOKEN", "browser-session")
        return response

    client, cfg, _ = await proxy_factory(backend, adapter="unifi")
    first = await client.get("/api/ingress/app/")
    await first.read()
    assert cfg.adapter_cookies == {}
    client.session.cookie_jar.clear()
    second = await client.get("/api/ingress/app/", cookies={"ingress_token": "t-test-token"})
    await second.read()
    assert received == [None, None]


async def test_signed_websocket_ticket_is_preserved(proxy_factory):
    ticket = "vncticket=PVE%3Atest%2Bticket%2Fvalue%3D&port=5900"

    async def backend(request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        await ws.send_str(request.rel_url.raw_query_string)
        async for message in ws:
            if message.type == aiohttp.WSMsgType.TEXT:
                await ws.send_str(message.data)
        return ws

    client, _, _ = await proxy_factory(backend, adapter="proxmox")
    async with client.ws_connect(URL("/api/ingress/app/ws?" + ticket, encoded=True)) as ws:
        assert (await ws.receive()).data == ticket
        await ws.send_str("console")
        assert (await ws.receive()).data == "console"


@pytest.mark.parametrize("compress", [False, True])
async def test_multipart_boundary_survives_proxy(proxy_factory, compress):
    content_type = "multipart/x-mixed-replace; boundary=test-boundary"
    body = b"--test-boundary\r\nContent-Type: text/plain\r\n\r\nhello\r\n--test-boundary--\r\n"

    async def backend(request):
        return web.Response(body=body, headers={"Content-Type": content_type})

    client, _, _ = await proxy_factory(backend, adapter="generic", enable_compress=compress)
    response = await client.get("/api/ingress/app/")
    assert response.headers["Content-Type"] == content_type
    assert await response.read() == body
