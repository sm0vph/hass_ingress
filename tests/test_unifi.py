"""Tests for the UniFi ingress response adapter."""

import asyncio
import importlib.util
from pathlib import Path
from types import SimpleNamespace
import unittest


MODULE_PATH = Path(__file__).parents[1] / "custom_components" / "ingress" / "unifi.py"
SPEC = importlib.util.spec_from_file_location("unifi_adapter", MODULE_PATH)
assert SPEC and SPEC.loader
unifi = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(unifi)

_JS_NAVIGATION = unifi._JS_NAVIGATION
_rewrite_cookie = unifi._rewrite_cookie
_rewrite_location = unifi._rewrite_location


class FakeResponse:
    def __init__(self, body: bytes):
        self._body = body
        self.url = SimpleNamespace(
            host="192.168.10.46", origin=lambda: "https://192.168.10.46:11443"
        )

    async def read(self):
        return self._body


class UnifiAdapterTest(unittest.TestCase):
    def test_rewrite_root_location(self):
        self.assertEqual(
            _rewrite_location("/login?redirect=%2F", "192.168.10.46", "/api/ingress/unifi_os"),
            "/api/ingress/unifi_os/login?redirect=%2F",
        )

    def test_rewrite_same_upstream_absolute_location(self):
        self.assertEqual(
            _rewrite_location(
                "https://192.168.10.46:11443/network/",
                "192.168.10.46",
                "/api/ingress/unifi_os",
            ),
            "/api/ingress/unifi_os/network/",
        )

    def test_preserve_external_location(self):
        value = "https://account.ui.com/login"
        self.assertEqual(
            _rewrite_location(value, "192.168.10.46", "/api/ingress/unifi_os"), value
        )

    def test_cookie_is_scoped_and_domain_removed(self):
        self.assertEqual(
            _rewrite_cookie(
                "TOKEN=value; Path=/; Domain=unifi.local; Secure; HttpOnly",
                "/api/ingress/unifi_os",
            ),
            "TOKEN=value; Path=/api/ingress/unifi_os/; Secure; HttpOnly",
        )

    def test_javascript_navigation_rewrite_is_narrow(self):
        body = (
            b'window.location.href="/login";window.location.assign("/network");'
            b"a=window.location;a.pathname;location.pathname;object.location.pathname"
        )
        for pattern, replacement in _JS_NAVIGATION:
            body = pattern.sub(replacement, body)
        self.assertEqual(
            body,
            b'window.__HA_INGRESS_LOCATION__.href="/login";'
            b'window.__HA_INGRESS_LOCATION__.assign("/network");'
            b"a=window.__HA_INGRESS_LOCATION__;a.pathname;"
            b"window.__HA_INGRESS_LOCATION__.pathname;object.location.pathname",
        )

    def test_html_response_is_rewritten_and_bootstrap_is_first(self):
        headers = {"X-Frame-Options": ["SAMEORIGIN"]}
        response_headers, body = asyncio.run(
            unifi.adapt_unifi_response(
                FakeResponse(b'<html><head><script src="/main.js"></script></head></html>'),
                headers,
                "text/html",
                "/api/ingress/unifi_os",
            )
        )
        self.assertNotIn("X-Frame-Options", response_headers)
        self.assertIn(b'data-ingress-path="/api/ingress/unifi_os"', body)
        self.assertIn(b'data-upstream-origin="https://192.168.10.46:11443"', body)
        self.assertIn(b'src="/api/ingress/unifi_os/main.js"', body)
        self.assertLess(body.index(b"unifi-adapter.js"), body.index(b"main.js"))


if __name__ == "__main__":
    unittest.main()
