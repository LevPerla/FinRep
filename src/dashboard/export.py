import os
import re
from pathlib import Path
from threading import Lock
from urllib.parse import urlencode, urlsplit, urlunsplit

import dash_bootstrap_components as dbc
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

from src import config


EXPORT_VIEWPORT = {"width": 1440, "height": 1200}
EXPORT_READY_TIMEOUT_MS = 90_000
EXPORT_SETTLE_MS = 1_000
EXPORT_TABS = {"main", "year", "month", "planning", "input", "debts", "investments"}
_EXPORT_LOCK = Lock()


class ExportBusyError(RuntimeError):
    pass


def build_dashboard_url(
    currency: str,
    tab: str,
    year: str | None = None,
    month: str | None = None,
) -> str:
    if not isinstance(currency, str) or currency not in config.UNIQUE_TICKERS:
        raise ValueError("Unsupported export currency")
    if not isinstance(tab, str) or tab not in EXPORT_TABS:
        raise ValueError("Unsupported export tab")
    if year is not None and (not isinstance(year, str) or not re.fullmatch(r"[0-9]{4}", year) or year == "0000"):
        raise ValueError("Invalid export year")
    if month is not None and (not isinstance(month, str) or not re.fullmatch(r"0[1-9]|1[0-2]", month)):
        raise ValueError("Invalid export month")
    # Match server startup: PORT, then FINREP_DASH_PORT/default. Never use Host/href.
    port = int(os.environ.get("PORT") or os.environ.get("FINREP_DASH_PORT", "8050"))
    if not 1 <= port <= 65535:
        raise ValueError("Invalid dashboard server port")
    params = {"currency": currency, "tab": tab}
    if year is not None:
        params["year"] = year
    if month is not None:
        params["month"] = month
    query = urlencode(params)
    return urlunsplit(("http", f"127.0.0.1:{port}", "/", query, ""))


def _route_export_request(route, origin: str) -> None:
    request = route.request
    url = urlsplit(request.url)
    local = (url.scheme, url.netloc) == ("http", urlsplit(origin).netloc)
    # Bootstrap is the app's only required external stylesheet, not an arbitrary CDN URL.
    stylesheet = (request.url == dbc.themes.BOOTSTRAP
                  and request.resource_type == "stylesheet" and request.method == "GET")
    if not local and not stylesheet:
        route.abort()
        return
    headers = None if local else {"cookie": "", "authorization": "", "referer": ""}
    response = route.fetch(max_redirects=0, headers=headers)
    try:
        # route.continue_ can follow redirects without rechecking the destination.
        if 300 <= response.status < 400:
            route.abort()
        else:
            route.fulfill(response=response)
    finally:
        response.dispose()


def export_dashboard_page(
    currency: str,
    tab: str,
    export_format: str,
    year: str | None = None,
    month: str | None = None,
    session_cookie: str | None = None,
    session_cookie_name: str = "session",
) -> Path:
    if not isinstance(export_format, str):
        raise ValueError("export_format must be 'png' or 'pdf'")
    export_format = export_format.lower()
    if export_format not in {"png", "pdf"}:
        raise ValueError("export_format must be 'png' or 'pdf'")

    dashboard_url = build_dashboard_url(currency, tab, year, month)
    if not _EXPORT_LOCK.acquire(blocking=False):
        raise ExportBusyError("Экспорт уже выполняется. Повторите после завершения.")

    try:
        origin = urlsplit(dashboard_url)
        origin_url = f"{origin.scheme}://{origin.netloc}"
        export_dir = Path(config.REPORTS_PATH) / "dashboard_exports" / currency
        export_dir.mkdir(parents=True, exist_ok=True)
        export_path = export_dir / _export_filename(currency, tab, export_format, year, month)

        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch()
                context = browser.new_context(viewport=EXPORT_VIEWPORT, service_workers="block")
                context.route("**/*", lambda route: _route_export_request(route, origin_url))
                # A routed WebSocket stays local unless connect_to_server() is called.
                context.route_web_socket("**/*", lambda websocket: None)
                if session_cookie:
                    context.add_cookies([
                        {
                            "name": session_cookie_name,
                            "value": session_cookie,
                            "url": origin_url,
                            "httpOnly": True,
                            "sameSite": "Lax",
                            "secure": origin.scheme == "https",
                        }
                    ])
                page = context.new_page()
                page.goto(dashboard_url, wait_until="domcontentloaded")
                _wait_for_dashboard_ready(page)
                if export_format == "png":
                    page.screenshot(path=export_path, full_page=True)
                else:
                    page_size = _page_size(page)
                    page.pdf(
                        path=export_path,
                        print_background=True,
                        width=f"{page_size['width']}px",
                        height=f"{page_size['height']}px",
                        margin={"top": "0", "right": "0", "bottom": "0", "left": "0"},
                    )
                browser.close()
        except PlaywrightError as exc:
            raise RuntimeError(
                "Playwright Chromium is not available. Run: uv run playwright install chromium"
            ) from exc

        return export_path
    finally:
        _EXPORT_LOCK.release()


def _wait_for_dashboard_ready(page) -> None:
    page.wait_for_function(
        """() => {
            const content = document.querySelector("#dashboard-content");
            if (!content || document.querySelector('[data-dash-is-loading="true"]')) {
                return false;
            }
            const visibleChild = Array.from(content.children).some((element) => {
                const style = window.getComputedStyle(element);
                return style.visibility !== "hidden"
                    && style.display !== "none"
                    && element.getBoundingClientRect().height > 0;
            });
            return visibleChild && content.textContent.trim().length > 0;
        }""",
        timeout=EXPORT_READY_TIMEOUT_MS,
    )
    page.evaluate("() => document.fonts ? document.fonts.ready : Promise.resolve()")
    page.wait_for_timeout(EXPORT_SETTLE_MS)


def _export_filename(
    currency: str,
    tab: str,
    export_format: str,
    year: str | None = None,
    month: str | None = None,
) -> str:
    from datetime import datetime

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    period = "_".join(part for part in [year, month] if part)
    period_part = f"_{period}" if period else ""
    return f"dashboard_{tab}{period_part}_{currency}_{timestamp}.{export_format}"


def _page_size(page) -> dict[str, int]:
    size = page.evaluate(
        """() => ({
            width: Math.ceil(Math.max(
                document.documentElement.scrollWidth,
                document.body.scrollWidth,
                window.innerWidth
            )),
            height: Math.ceil(Math.max(
                document.documentElement.scrollHeight,
                document.body.scrollHeight,
                window.innerHeight
            ))
        })"""
    )
    return {
        "width": max(size["width"], EXPORT_VIEWPORT["width"]),
        "height": max(size["height"], EXPORT_VIEWPORT["height"]),
    }
