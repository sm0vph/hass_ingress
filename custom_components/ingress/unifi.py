"""Response adapter for running UniFi OS below a Home Assistant ingress path."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

if TYPE_CHECKING:
    from aiohttp import ClientResponse
    from .config import IngressCfg

LOCATION = "Location"
SET_COOKIE = "Set-Cookie"
X_FRAME_OPTIONS = "X-Frame-Options"
ADAPTED_CACHE_HEADERS = ("ETag", "Last-Modified", "Expires")


_HTML_URL_ATTRIBUTE = re.compile(
    rb"(?P<prefix>\b(?:src|href|action)\s*=\s*[\"'])/(?!(?:/|api/ingress/))",
    re.IGNORECASE,
)
_HTML_JSON_URL = re.compile(rb'(?P<prefix>["\']url["\']\s*:\s*["\'])/(?!(?:/|api/ingress/))')
_CSS_ROOT_URL = re.compile(rb"(?P<prefix>url\(\s*[\"']?)/(?!(?:/|api/ingress/))", re.IGNORECASE)
_HEAD_START = re.compile(rb"<head(?:\s[^>]*)?>", re.IGNORECASE)
_JS_NAVIGATION = (
    (
        re.compile(rb"(?<![$\w])window\.location\b"),
        rb"window.__HA_INGRESS_LOCATION__",
    ),
    (
        re.compile(
            rb"(?<![.\w])location\."
            rb"(assign|replace|reload|href|pathname|search|hash|origin|host|hostname|port|protocol)\b"
        ),
        rb"window.__HA_INGRESS_LOCATION__.\1",
    ),
)


async def ensure_unifi_login(websession, cfg: "IngressCfg", *, force: bool = False) -> bool:
    """Create and retain a local UniFi OS session without exposing credentials."""
    if not cfg.username or not cfg.password:
        return False
    async with cfg.adapter_lock:
        if (cfg.adapter_cookies or cfg.adapter_login_attempted) and not force:
            return bool(cfg.adapter_cookies)
        cfg.adapter_cookies.clear()
        cfg.adapter_headers.clear()
        cfg.adapter_login_attempted = True
        login_url = cfg.origin.join(type(cfg.origin)(f"{cfg.sub_path}/api/auth/login"))
        async with websession.post(
            login_url,
            json={
                "username": cfg.username,
                "password": cfg.password,
                "rememberMe": True,
                "token": "",
            },
            headers={"Host": cfg.origin.raw_authority},
            allow_redirects=False,
            ssl=cfg.verify_ssl,
        ) as response:
            update_unifi_session(response, cfg)
        return bool(cfg.adapter_cookies)


def update_unifi_session(response: "ClientResponse", cfg: "IngressCfg") -> None:
    """Capture rotated cookies and CSRF tokens from an upstream response."""
    cfg.adapter_cookies.update(
        (name, morsel.value) for name, morsel in response.cookies.items() if morsel.value
    )
    csrf = response.headers.get("X-Updated-Csrf-Token") or response.headers.get(
        "X-Csrf-Token"
    )
    if csrf:
        cfg.adapter_headers["X-Csrf-Token"] = csrf


def add_unifi_credentials(headers: dict[str, str], cfg: "IngressCfg") -> None:
    """Add the private UniFi session and CSRF token to an upstream request."""
    if cfg.adapter_cookies:
        private_names = {name.lower() for name in cfg.adapter_cookies}
        browser_cookie = headers.get("Cookie", "")
        browser_cookie = "; ".join(
            part.strip()
            for part in browser_cookie.split(";")
            if part.strip() and part.strip().partition("=")[0].lower() not in private_names
        )
        private_cookie = "; ".join(f"{k}={v}" for k, v in cfg.adapter_cookies.items())
        headers["Cookie"] = (
            f"{browser_cookie}; {private_cookie}" if browser_cookie else private_cookie
        )
    headers.update(cfg.adapter_headers)


async def adapt_unifi_response(
    response: "ClientResponse",
    headers: dict[str, list[str]],
    content_type: str,
    ingress_path: str,
) -> tuple[dict[str, list[str]], bytes | None]:
    """Normalize UniFi headers and selected textual resources for ingress."""
    headers.pop(X_FRAME_OPTIONS, None)

    if locations := headers.get(LOCATION):
        headers[LOCATION] = [
            _rewrite_location(value, response.url.host, ingress_path) for value in locations
        ]
    if cookies := headers.get(SET_COOKIE):
        headers[SET_COOKIE] = [_rewrite_cookie(value, ingress_path) for value in cookies]

    if content_type == "text/html":
        _disable_adapted_response_cache(headers)
        body = await response.read()
        escaped_path = ingress_path.encode()
        body = _HTML_URL_ATTRIBUTE.sub(rb"\g<prefix>" + escaped_path + b"/", body)
        body = _HTML_JSON_URL.sub(rb"\g<prefix>" + escaped_path + b"/", body)
        bootstrap = (
            b'<script src="/files/ingress/unifi-adapter.js?v=7" data-ingress-path="'
            + escaped_path
            + b'" data-upstream-origin="'
            + str(response.url.origin()).encode()
            + b'"></script>'
        )
        body, count = _HEAD_START.subn(lambda match: match.group(0) + bootstrap, body, count=1)
        if not count:
            body = bootstrap + body
        return headers, body

    if content_type == "text/css":
        _disable_adapted_response_cache(headers)
        body = await response.read()
        body = _CSS_ROOT_URL.sub(rb"\g<prefix>" + ingress_path.encode() + b"/", body)
        return headers, body

    if content_type in ("application/javascript", "text/javascript"):
        _disable_adapted_response_cache(headers)
        body = await response.read()
        for pattern, replacement in _JS_NAVIGATION:
            body = pattern.sub(replacement, body)
        return headers, body

    return headers, None


def _disable_adapted_response_cache(headers: dict[str, list[str]]) -> None:
    """Prevent clients and intermediary proxies from caching rewritten content."""
    for name in ADAPTED_CACHE_HEADERS:
        headers.pop(name, None)
    headers["Cache-Control"] = ["no-store"]


def _rewrite_location(value: str, upstream_host: str | None, ingress_path: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme and parsed.hostname != upstream_host:
        return value
    path = parsed.path or "/"
    if path.startswith(f"{ingress_path}/"):
        return value
    suffix = f"?{parsed.query}" if parsed.query else ""
    if parsed.fragment:
        suffix += f"#{parsed.fragment}"
    return f"{ingress_path}/{path.lstrip('/')}{suffix}"


def _rewrite_cookie(value: str, ingress_path: str) -> str:
    parts = [part.strip() for part in value.split(";")]
    rewritten = []
    has_path = False
    for part in parts:
        key = part.partition("=")[0]
        key = key.strip().lower()
        if key == "domain":
            continue
        if key == "path":
            rewritten.append(f"Path={ingress_path}/")
            has_path = True
        else:
            rewritten.append(part)
    if not has_path:
        rewritten.append(f"Path={ingress_path}/")
    return "; ".join(rewritten)
