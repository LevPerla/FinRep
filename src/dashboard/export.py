from pathlib import Path
from urllib.parse import urlencode, urlsplit, urlunsplit

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

from src import config


EXPORT_VIEWPORT = {"width": 1440, "height": 1200}
EXPORT_WAIT_MS = 2000


def build_dashboard_url(
    base_url: str,
    currency: str,
    tab: str,
    year: str | None = None,
    month: str | None = None,
) -> str:
    split_url = urlsplit(base_url)
    params = {"currency": currency, "tab": tab}
    if year is not None:
        params["year"] = year
    if month is not None:
        params["month"] = month
    query = urlencode(params)
    return urlunsplit((split_url.scheme, split_url.netloc, split_url.path or "/", query, ""))


def export_dashboard_page(
    base_url: str,
    currency: str,
    tab: str,
    export_format: str,
    year: str | None = None,
    month: str | None = None,
) -> Path:
    export_format = export_format.lower()
    if export_format not in {"png", "pdf"}:
        raise ValueError("export_format must be 'png' or 'pdf'")

    export_dir = Path(config.REPORTS_PATH) / "dashboard_exports" / currency
    export_dir.mkdir(parents=True, exist_ok=True)
    export_path = export_dir / _export_filename(currency, tab, export_format, year, month)
    dashboard_url = build_dashboard_url(base_url, currency, tab, year, month)

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            page = browser.new_page(viewport=EXPORT_VIEWPORT)
            page.goto(dashboard_url, wait_until="domcontentloaded")
            page.wait_for_selector("#dashboard-content")
            page.wait_for_timeout(EXPORT_WAIT_MS)
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
