"""Response adapter for running UniFi OS below a Home Assistant ingress path."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

if TYPE_CHECKING:
    from aiohttp import ClientResponse

LOCATION = "Location"
SET_COOKIE = "Set-Cookie"
X_FRAME_OPTIONS = "X-Frame-Options"


_HTML_URL_ATTRIBUTE = re.compile(
    rb"(?P<prefix>\b(?:src|href|action)\s*=\s*[\"'])/(?!(?:/|api/ingress/))",
    re.IGNORECASE,
)
_CSS_ROOT_URL = re.compile(rb"(?P<prefix>url\(\s*[\"']?)/(?!(?:/|api/ingress/))", re.IGNORECASE)
_HEAD_START = re.compile(rb"<head(?:\s[^>]*)?>", re.IGNORECASE)
_JS_NAVIGATION = (
    (
        re.compile(rb"\bwindow\.location\b"),
        rb"window.__HA_INGRESS_LOCATION__",
    ),
    (
        re.compile(rb"(?<![.\w])location\.(pathname|search|hash)\b"),
        rb"window.__HA_INGRESS_LOCATION__.\1",
    ),
)


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
        body = await response.read()
        escaped_path = ingress_path.encode()
        body = _HTML_URL_ATTRIBUTE.sub(rb"\g<prefix>" + escaped_path + b"/", body)
        bootstrap = (
            b'<script src="/files/ingress/unifi-adapter.js" data-ingress-path="'
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
        body = await response.read()
        body = _CSS_ROOT_URL.sub(rb"\g<prefix>" + ingress_path.encode() + b"/", body)
        return headers, body

    if content_type in ("application/javascript", "text/javascript"):
        body = await response.read()
        for pattern, replacement in _JS_NAVIGATION:
            body = pattern.sub(replacement, body)
        return headers, body

    return headers, None


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
