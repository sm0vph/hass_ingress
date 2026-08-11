"""Server-side authentication helpers for the Unraid web interface."""

from typing import TYPE_CHECKING
from urllib.parse import urlparse

if TYPE_CHECKING:
    from .config import IngressCfg


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


def is_unraid_login_redirect(status: int, location: str | None) -> bool:
    """Return whether Unraid rejected the current session."""
    if status not in (301, 302, 303, 307, 308) or not location:
        return False
    return urlparse(location).path.rstrip("/") == "/login"
