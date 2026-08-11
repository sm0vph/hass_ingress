import importlib.util
from pathlib import Path
from types import SimpleNamespace
import asyncio
import base64
import unittest
import json
import re


ROOT = Path(__file__).parents[1]
spec = importlib.util.spec_from_file_location(
    "unraid_adapter", ROOT / "custom_components" / "ingress" / "unraid.py"
)
assert spec and spec.loader
unraid = importlib.util.module_from_spec(spec)
spec.loader.exec_module(unraid)
add_unraid_cookies = unraid.add_unraid_cookies
add_basic_auth = unraid.add_basic_auth
is_unraid_login_redirect = unraid.is_unraid_login_redirect


class UnraidAdapterTest(unittest.TestCase):
    def test_generic_basic_auth_is_added_server_side(self):
        cfg = SimpleNamespace(username="alice", password="secret")
        headers = {}
        add_basic_auth(headers, cfg)
        self.assertEqual(headers["Authorization"], "Basic YWxpY2U6c2VjcmV0")

    def test_private_cookie_replaces_browser_cookie(self):
        cfg = SimpleNamespace(adapter_cookies={"PHPSESSID": "private"})
        headers = {"Cookie": "theme=dark; PHPSESSID=browser"}
        add_unraid_cookies(headers, cfg)
        self.assertEqual(headers["Cookie"], "theme=dark; PHPSESSID=private")

    def test_login_redirect_detection(self):
        self.assertTrue(is_unraid_login_redirect(302, "/login"))
        self.assertTrue(is_unraid_login_redirect(303, "http://tower/login"))
        self.assertFalse(is_unraid_login_redirect(302, "/Main"))
        self.assertFalse(is_unraid_login_redirect(200, "/login"))

    def test_response_adapter_handles_unraid_root_paths(self):
        class FakeResponse:
            url = SimpleNamespace(
                host="192.168.10.45", origin=lambda: "http://192.168.10.45"
            )

            async def read(self):
                return (
                    b'<html><head><link href="/plugins/theme.css">'
                    b'<script>window.location.href="/logout";'
                    b'window.open("/webterminal/ttyd/")</script></head></html>'
                )

        headers, body = asyncio.run(
            unraid.adapt_unraid_response(
                FakeResponse(),
                {
                    "Location": ["/Main"],
                    "Set-Cookie": ["PHPSESSID=x; Path=/; HttpOnly"],
                    "X-Frame-Options": ["SAMEORIGIN"],
                    "ETag": ['"cached"'],
                },
                "text/html",
                "/api/ingress/unraid",
            )
        )
        self.assertEqual(headers["Location"], ["/api/ingress/unraid/Main"])
        self.assertEqual(
            headers["Set-Cookie"],
            ["PHPSESSID=x; Path=/api/ingress/unraid/; HttpOnly"],
        )
        self.assertNotIn("X-Frame-Options", headers)
        self.assertNotIn("ETag", headers)
        self.assertEqual(headers["Cache-Control"], ["no-store"])
        self.assertIn(b'href="/api/ingress/unraid/plugins/theme.css"', body)
        self.assertIn(
            b"window.__HA_INGRESS_LOCATION__.href=\"/api/ingress/unraid/logout\"",
            body,
        )
        self.assertIn(b"unifi-adapter.js", body)

    def test_subapp_allowlist_is_embedded_in_document(self):
        class FakeResponse:
            url = SimpleNamespace(
                host="192.168.10.45", origin=lambda: "http://192.168.10.45"
            )

            async def read(self):
                return b"<html><head></head></html>"

        links = {
            "http://192.168.10.45:8384/": "/api/ingress/unraid_syncthing/"
        }
        _, body = asyncio.run(
            unraid.adapt_unraid_response(
                FakeResponse(), {}, "text/html", "/api/ingress/unraid", links
            )
        )
        encoded = re.search(rb'data-ingress-links="([^"]+)"', body).group(1)
        self.assertEqual(json.loads(base64.b64decode(encoded)), links)

    def test_html_fragment_does_not_get_bootstrap_prefix(self):
        class FakeResponse:
            url = SimpleNamespace(
                host="192.168.10.45", origin=lambda: "http://192.168.10.45"
            )

            async def read(self):
                return b'{"url":"/Main/Device","script":"window.location.href"}'

        _, body = asyncio.run(
            unraid.adapt_unraid_response(
                FakeResponse(), {}, "text/html", "/api/ingress/unraid"
            )
        )
        self.assertEqual(
            body, b'{"url":"/Main/Device","script":"window.location.href"}'
        )


if __name__ == "__main__":
    unittest.main()
