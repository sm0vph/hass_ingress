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
normalize_ingress_path = unraid.normalize_ingress_path


class UnraidAdapterTest(unittest.TestCase):
    def test_repeated_ingress_prefix_with_leading_slash_is_removed(self):
        self.assertEqual(
            normalize_ingress_path(
                "/api/ingress/proxmox/api2/extjs/nodes/pve2/lxc/101/config",
                "/api/ingress/proxmox",
            ),
            "api2/extjs/nodes/pve2/lxc/101/config",
        )

    def test_embedded_proxmox_ingress_prefix_is_removed(self):
        self.assertEqual(
            normalize_ingress_path(
                "api2/extjs/api/ingress/proxmox/api2/extjs/nodes/pve2/lxc/103/config",
                "/api/ingress/proxmox",
            ),
            "api2/extjs/nodes/pve2/lxc/103/config",
        )

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
                    "Content-Length": ["42"],
                    "Content-Encoding": ["gzip"],
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
        self.assertNotIn("Content-Length", headers)
        self.assertNotIn("Content-Encoding", headers)
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

    def test_angular_location_service_is_not_rewritten(self):
        class FakeResponse:
            url = SimpleNamespace(
                host="192.168.10.45", origin=lambda: "http://192.168.10.45"
            )

            async def read(self):
                return b"$location.hash(); location.hash; $window.location.hash();"

        _, body = asyncio.run(
            unraid.adapt_unraid_response(
                FakeResponse(), {}, "application/javascript", "/api/ingress/app"
            )
        )
        self.assertEqual(
            body,
            b"$location.hash(); window.__HA_INGRESS_LOCATION__.hash; "
            b"$window.location.hash();",
        )

    def test_proxmox_javascript_module_paths_are_rewritten(self):
        class FakeResponse:
            url = SimpleNamespace(
                host="192.168.10.32", origin=lambda: "https://192.168.10.32:8006"
            )

            async def read(self):
                return b'import("/novnc/app.js?ver=1.7.0-2"); fetch("/api2/json/version")'

        _, body = asyncio.run(
            unraid.adapt_unraid_response(
                FakeResponse(),
                {},
                "application/javascript",
                "/api/ingress/proxmox",
                rewrite_root_js=True,
            )
        )
        self.assertEqual(
            body,
            b'import("/api/ingress/proxmox/novnc/app.js?ver=1.7.0-2"); '
            b'fetch("/api2/json/version")',
        )

    def test_proxmox_adapter_does_not_rewrite_regular_expression_source(self):
        class FakeResponse:
            url = SimpleNamespace(
                host="192.168.10.32", origin=lambda: "https://192.168.10.32:8006"
            )

            async def read(self):
                return b'const separator="/"; const matcher=/foo\\/bar/g;'

        _, body = asyncio.run(
            unraid.adapt_unraid_response(
                FakeResponse(),
                {},
                "application/javascript",
                "/api/ingress/proxmox",
                rewrite_root_js=True,
            )
        )
        self.assertEqual(body, b'const separator="/"; const matcher=/foo\\/bar/g;')

    def test_proxmox_console_popup_path_is_rewritten(self):
        class FakeResponse:
            url = SimpleNamespace(
                host="192.168.10.32", origin=lambda: "https://192.168.10.32:8006"
            )

            async def read(self):
                return b"const url = '/?console=' + type + '&vmid=' + vmid;"

        _, body = asyncio.run(
            unraid.adapt_unraid_response(
                FakeResponse(),
                {},
                "application/javascript",
                "/api/ingress/proxmox",
                rewrite_root_js=True,
            )
        )
        self.assertEqual(
            body,
            b"const url = '/api/ingress/proxmox/?console=' + type + '&vmid=' + vmid;",
        )

    def test_proxmox_relative_console_popup_path_is_rewritten(self):
        class FakeResponse:
            url = SimpleNamespace(
                host="192.168.10.32", origin=lambda: "https://192.168.10.32:8006"
            )

            async def read(self):
                return b"let url = '?console=' + type + '&xtermjs=1';"

        _, body = asyncio.run(
            unraid.adapt_unraid_response(
                FakeResponse(),
                {},
                "application/javascript",
                "/api/ingress/proxmox",
                rewrite_root_js=True,
            )
        )
        self.assertEqual(
            body,
            b"let url = '/api/ingress/proxmox/?console=' + type + '&xtermjs=1';",
        )

    def test_javascript_content_type_with_json_body_is_not_rewritten(self):
        class FakeResponse:
            url = SimpleNamespace(
                host="192.168.10.45", origin=lambda: "http://192.168.10.45"
            )

            async def read(self):
                return b'{"script":"window.location.href","url":"/Docker"}'

        _, body = asyncio.run(
            unraid.adapt_unraid_response(
                FakeResponse(),
                {},
                "application/javascript",
                "/api/ingress/unraid",
            )
        )
        self.assertEqual(
            body, b'{"script":"window.location.href","url":"/Docker"}'
        )


if __name__ == "__main__":
    unittest.main()
