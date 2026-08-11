"""Authentication and response adaptation for the Unraid web interface."""

import base64
import json
import re
from typing import TYPE_CHECKING
from urllib.parse import urlparse, urlsplit

if TYPE_CHECKING:
    from aiohttp import ClientResponse
    from .config import IngressCfg


LOCATION = "Location"
SET_COOKIE = "Set-Cookie"
X_FRAME_OPTIONS = "X-Frame-Options"
ADAPTED_CACHE_HEADERS = ("ETag", "Last-Modified", "Expires")
ADAPTED_ENTITY_HEADERS = ("Content-Length", "Content-Encoding", "Transfer-Encoding")

_HTML_URL_ATTRIBUTE = re.compile(
    rb"(?P<prefix>\b(?:src|href|action)\s*=\s*[\"'])/(?!(?:/|api/ingress/))",
    re.IGNORECASE,
)
_HTML_JSON_URL = re.compile(rb'(?P<prefix>["\']url["\']\s*:\s*["\'])/(?!(?:/|api/ingress/))')
_CSS_ROOT_URL = re.compile(rb"(?P<prefix>url\(\s*[\"']?)/(?!(?:/|api/ingress/))", re.IGNORECASE)
_PROXMOX_JS_ROOT_URL = re.compile(
    rb"(?P<prefix>[\"'`])/(?P<path>(?:novnc|xtermjs)/)", re.IGNORECASE
)
_PROXMOX_CONSOLE_URL = re.compile(rb"(?P<prefix>[\"'`])/?\?console=")
_HEAD_START = re.compile(rb"<head(?:\s[^>]*)?>", re.IGNORECASE)
_JS_NAVIGATION = (
    (
        re.compile(rb"(?<![$\w])window\.location\b"),
        rb"window.__HA_INGRESS_LOCATION__",
    ),
    (
        re.compile(
            rb"(?<![$.\w])location\."
            rb"(assign|replace|reload|href|pathname|search|hash|origin|host|hostname|port|protocol)\b"
        ),
        rb"window.__HA_INGRESS_LOCATION__.\1",
    ),
)


async def adapt_unraid_response(
    response: "ClientResponse",
    headers: dict[str, list[str]],
    content_type: str,
    ingress_path: str,
    sub_apps: dict[str, str] | None = None,
    *,
    rewrite_root_js: bool = False,
) -> tuple[dict[str, list[str]], bytes | None]:
    """Rewrite Unraid resources so they remain below the ingress path."""
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
        if not _HEAD_START.search(body):
            return headers, body
        escaped_path = ingress_path.encode()
        body = _HTML_URL_ATTRIBUTE.sub(rb"\g<prefix>" + escaped_path + b"/", body)
        body = _HTML_JSON_URL.sub(rb"\g<prefix>" + escaped_path + b"/", body)
        if rewrite_root_js:
            body = _PROXMOX_JS_ROOT_URL.sub(
                rb"\g<prefix>" + escaped_path + rb"/\g<path>", body
            )
            body = _PROXMOX_CONSOLE_URL.sub(
                rb"\g<prefix>" + escaped_path + b"/?console=", body
            )
        for pattern, replacement in _JS_NAVIGATION:
            body = pattern.sub(replacement, body)
        bootstrap = (
            b'<script src="/files/ingress/unifi-adapter.js?v=16" data-ingress-path="'
            + escaped_path
            + b'" data-upstream-origin="'
            + str(response.url.origin()).encode()
            + b'" data-ingress-links="'
            + base64.b64encode(json.dumps(sub_apps or {}, separators=(",", ":")).encode())
            + b'"></script>'
        )
        body, _ = _HEAD_START.subn(lambda match: match.group(0) + bootstrap, body, count=1)
        return headers, body

    if content_type == "text/css":
        _disable_adapted_response_cache(headers)
        body = _CSS_ROOT_URL.sub(
            rb"\g<prefix>" + ingress_path.encode() + b"/", await response.read()
        )
        return headers, body

    if content_type in ("application/javascript", "text/javascript"):
        _disable_adapted_response_cache(headers)
        body = await response.read()
        try:
            json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            pass
        else:
            # Some Unraid endpoints label JSON as JavaScript. FolderView parses
            # these responses directly, so they must remain byte-identical.
            return headers, body
        for pattern, replacement in _JS_NAVIGATION:
            body = pattern.sub(replacement, body)
        if rewrite_root_js:
            body = _PROXMOX_JS_ROOT_URL.sub(
                rb"\g<prefix>" + ingress_path.encode() + rb"/\g<path>", body
            )
            body = _PROXMOX_CONSOLE_URL.sub(
                rb"\g<prefix>" + ingress_path.encode() + b"/?console=", body
            )
        return headers, body

    return headers, None


def _disable_adapted_response_cache(headers: dict[str, list[str]]) -> None:
    # aiohttp decodes response bodies before ``read()`` and the adapters can
    # change their length.  Never forward the upstream entity framing for a
    # body that Home Assistant will emit again.
    for name in ADAPTED_ENTITY_HEADERS:
        headers.pop(name, None)
    for name in ADAPTED_CACHE_HEADERS:
        headers.pop(name, None)
    headers["Cache-Control"] = ["no-store"]


def normalize_ingress_path(path: str, ingress_path: str) -> str:
    """Remove an ingress prefix accidentally retained in a forwarded path."""
    path = path.lstrip("/")
    prefix = ingress_path.strip("/") + "/"
    marker = "/" + prefix
    while marker in path:
        # Proxmox can prepend /api2/extjs to an API URL that an older cached
        # adapter already prefixed. Keep only the path following the embedded
        # ingress marker.
        path = path.split(marker, 1)[1].lstrip("/")
    while path.startswith(prefix):
        path = path[len(prefix) :].lstrip("/")
    return path


def _rewrite_location(value: str, upstream_host: str | None, ingress_path: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme and parsed.hostname != upstream_host:
        return value
    if parsed.path.startswith(f"{ingress_path}/"):
        return value
    suffix = f"?{parsed.query}" if parsed.query else ""
    if parsed.fragment:
        suffix += f"#{parsed.fragment}"
    return f"{ingress_path}/{(parsed.path or '/').lstrip('/')}{suffix}"


def _rewrite_cookie(value: str, ingress_path: str) -> str:
    parts = [part.strip() for part in value.split(";")]
    rewritten, has_path = [], False
    for part in parts:
        key = part.partition("=")[0].strip().lower()
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


async def ensure_unraid_login(websession, cfg: "IngressCfg", *, force: bool = False) -> bool:
    """Create and retain an Unraid PHP session without exposing credentials downstream."""
    if not cfg.username or not cfg.password:
        return False

    async with cfg.adapter_lock:
        if (cfg.adapter_cookies or cfg.adapter_login_attempted) and not force:
            return bool(cfg.adapter_cookies)
        cfg.adapter_cookies.clear()
        cfg.adapter_login_attempted = True
        login_url = cfg.origin.join(type(cfg.origin)(f"{cfg.sub_path}/login"))
        headers = {"Host": cfg.origin.raw_authority}
        async with websession.post(
            login_url,
            data={"username": cfg.username, "password": cfg.password},
            headers=headers,
            allow_redirects=False,
            ssl=cfg.verify_ssl,
        ) as response:
            cfg.adapter_cookies.update(
                (name, morsel.value) for name, morsel in response.cookies.items()
            )
        return bool(cfg.adapter_cookies)


def add_unraid_cookies(headers: dict[str, str], cfg: "IngressCfg") -> None:
    """Add the private upstream session to a proxied request."""
    if not cfg.adapter_cookies:
        return
    adapter_cookie = "; ".join(f"{k}={v}" for k, v in cfg.adapter_cookies.items())
    browser_cookie = headers.get("Cookie")
    if browser_cookie:
        private_names = {name.lower() for name in cfg.adapter_cookies}
        browser_cookie = "; ".join(
            part.strip()
            for part in browser_cookie.split(";")
            if part.strip().partition("=")[0].lower() not in private_names
        )
    headers["Cookie"] = (
        f"{browser_cookie}; {adapter_cookie}" if browser_cookie else adapter_cookie
    )


def add_basic_auth(headers: dict[str, str], cfg: "IngressCfg") -> None:
    """Add optional server-side HTTP Basic authentication for a generic adapter."""
    if not cfg.username or not cfg.password:
        return
    credentials = base64.b64encode(f"{cfg.username}:{cfg.password}".encode()).decode()
    headers["Authorization"] = f"Basic {credentials}"


def is_unraid_login_redirect(status: int, location: str | None) -> bool:
    """Return whether Unraid rejected the current session."""
    if status not in (301, 302, 303, 307, 308) or not location:
        return False
    return urlparse(location).path.rstrip("/") == "/login"
