"""MCP Server: Israeli weather forecasts, scraped from a browser with Playwright.

There is no API here. The server drives a real Chromium the way a person would:
open weather2day.co.il/forecast, type a city into the search box, pick a city from
the autocomplete list, and then read the forecast page that loads.

The four tools mirror those four steps, and they are stateful on purpose - the
browser stays open between calls, so the LLM can call them in sequence and watch
the page build up. That is the whole point of the exercise: the model gets a hand
on the mouse.

    open_weather_forecast_israel()          -> open the browser on the forecast page
    enter_weather_forecast_city_israel(city)-> type a city, return the suggestions
    select_weather_forecast_city_israel(i)  -> click a suggestion (default: the first)
    extract_weather_forecast_israel()       -> read the loaded page back as text (RAG)
    close_weather_browser_israel()          -> tidy up

In the project template this file is a stub: a FastMCP instance, a FORECAST_URL,
and nothing else. Everything below is the assignment's work area.

Selectors below were read off the live DOM, not guessed. See SELECTORS.
"""

from __future__ import annotations

import asyncio
import os
import re
from dataclasses import dataclass, field

from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP
from playwright.async_api import Browser, Page, Playwright, async_playwright
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeout

load_dotenv()

# The server name and FORECAST_URL are the template's. log_level is not: without it
# the SDK prints "Processing request of type CallToolRequest" to stderr on every
# call, straight into the user's chat.
mcp = FastMCP("weather-Israel", log_level="WARNING")

FORECAST_URL = "https://www.weather2day.co.il/forecast"

# ---------------------------------------------------------------------------
# Selectors, as they actually appear on weather2day.co.il (checked 2026-07).
#
# The page ships TWO search inputs: #city_search lives in the sticky header and
# is hidden on the forecast page, while #city_search_forecast is the visible one
# in the page body. Targeting the wrong one types into an invisible box and the
# autocomplete never opens - the single most likely way to get this wrong.
#
# The suggestion list is a plain <div> whose id is the input's id with
# "autocomplete-list" glued on, holding one <div> per suggestion.
# ---------------------------------------------------------------------------
SEARCH_INPUT = "#city_search_forecast"
SUGGESTION_LIST = "#city_search_forecastautocomplete-list"
SUGGESTION_ITEMS = f"{SUGGESTION_LIST} > div"
COOKIE_ACCEPT = "text=מקובל"

HEADLESS = os.getenv("WEATHER_HEADLESS", "false").lower() in {"1", "true", "yes"}
SLOW_MO_MS = int(os.getenv("WEATHER_SLOW_MO_MS", "0") or 0)
NAV_TIMEOUT_MS = int(os.getenv("WEATHER_TIMEOUT_MS", "45000") or 45000)

# The forecast page pulls in a dozen ad and analytics scripts. They contribute
# nothing to the forecast and everything to `goto` timing out on a slow link, so
# the context refuses to load them. Same-origin requests, which is where the
# autocomplete lives, are untouched.
_BLOCKED_HOSTS = (
    "doubleclick.net",
    "googlesyndication.com",
    "googletagservices.com",
    "google-analytics.com",
    "googletagmanager.com",
    "adservice.google",
    "amazon-adsystem.com",
    "adnxs.com",
    "pubmatic.com",
    "rubiconproject.com",
    "criteo",
    "taboola.com",
    "outbrain.com",
    "hotjar.com",
    "facebook.net",
)


# ---------------------------------------------------------------------------
# Browser session. One browser, kept alive across tool calls.
# ---------------------------------------------------------------------------
@dataclass
class _Session:
    playwright: Playwright | None = None
    browser: Browser | None = None
    page: Page | None = None
    last_city_query: str = ""
    last_suggestions: list[str] = field(default_factory=list)
    selected_city: str = ""

    @property
    def is_open(self) -> bool:
        return self.page is not None and not self.page.is_closed()


_session = _Session()
_lock = asyncio.Lock()


async def _dismiss_cookie_banner(page: Page) -> None:
    try:
        button = page.locator(COOKIE_ACCEPT).first
        if await button.is_visible(timeout=1500):
            await button.click(timeout=2000)
    except (PlaywrightTimeout, PlaywrightError):
        pass  # no banner, or it already went away. Either way, carry on.


async def _abort(route) -> None:
    try:
        await route.abort()
    except PlaywrightError:
        pass  # the page navigated away mid-flight; the request is moot


async def _block_ads(context) -> None:
    """Drop ad and analytics requests, and only those.

    A catch-all `context.route("**/*")` would work, but it round-trips *every*
    request through this process. On the same event loop that serves MCP over
    stdio, that starves the autocomplete's own XHR and the suggestion list never
    arrives. One narrow pattern per blocked host keeps the matching inside
    Playwright, where it belongs.

    `**` spans slashes, so `**doubleclick.net**` matches the host anywhere in the
    URL - which is what we want, and what `**/*doubleclick.net/**` would miss for
    hosts like `static.criteo.net`.
    """
    for host in _BLOCKED_HOSTS:
        await context.route(f"**{host}**", _abort)


async def _goto_forecast(page: Page) -> None:
    """Navigate to the forecast page and wait for the thing we actually need.

    `wait_until="commit"` returns as soon as the navigation is committed, instead
    of waiting for every third-party script to settle. What matters is that the
    search input exists, so that is what we wait for.
    """
    last_error: Exception | None = None
    for attempt in range(2):
        try:
            await page.goto(FORECAST_URL, wait_until="commit", timeout=NAV_TIMEOUT_MS)
            await _dismiss_cookie_banner(page)
            await page.wait_for_selector(SEARCH_INPUT, state="visible", timeout=NAV_TIMEOUT_MS)
            return
        except PlaywrightTimeout as exc:
            last_error = exc
            if attempt == 0:
                await page.wait_for_timeout(1000)
    raise last_error  # type: ignore[misc]


async def _open_page() -> Page:
    """Start Chromium (once) and land on the forecast page."""
    if _session.playwright is None:
        _session.playwright = await async_playwright().start()
    if _session.browser is None or not _session.browser.is_connected():
        _session.browser = await _session.playwright.chromium.launch(
            headless=HEADLESS, slow_mo=SLOW_MO_MS
        )
    if not _session.is_open:
        context = await _session.browser.new_context(
            locale="he-IL",
            viewport={"width": 1366, "height": 900},
        )
        await _block_ads(context)
        _session.page = await context.new_page()

    page = _session.page
    assert page is not None
    page.set_default_timeout(NAV_TIMEOUT_MS)
    await _goto_forecast(page)
    return page


_STABLE_SUGGESTIONS_JS = f"""() => {{
    const n = document.querySelectorAll('{SUGGESTION_ITEMS}').length;
    if (n === 0) return false;
    const stable = window.__w2dSuggestionCount === n;
    window.__w2dSuggestionCount = n;
    return stable;
}}"""


async def _wait_for_stable_suggestions(page: Page, timeout_ms: int = 6_000) -> None:
    """Wait for the autocomplete to finish, not merely to start.

    The list is rebuilt on every keystroke, so "at least one suggestion exists" is
    true long before the list is complete. Reading it then returns whatever had
    rendered so far - which on a fast connection is one item out of four, and
    which made the tool quietly pick a beach instead of the city. Waiting for the
    count to hold steady across two polls is what makes it deterministic.
    """
    await page.evaluate("() => { window.__w2dSuggestionCount = -1; }")
    await page.wait_for_function(_STABLE_SUGGESTIONS_JS, timeout=timeout_ms, polling=200)


async def _type_city_and_wait(page: Page, city: str, attempts: int = 3) -> bool:
    """Type the city and wait for its suggestions. Retry if none arrive.

    The input element is visible before the script that binds the autocomplete has
    run. Typing into it then produces a filled box and an empty list, forever -
    the handler missed every keystroke and nothing will fire it again. Rather than
    guess how long the bundle takes, clear the box and type it again.
    """
    box = page.locator(SEARCH_INPUT)
    await box.wait_for(state="visible", timeout=NAV_TIMEOUT_MS)

    try:  # best effort: if the bundle has already run, this returns immediately
        await page.wait_for_load_state("load", timeout=8_000)
    except PlaywrightTimeout:
        pass

    for attempt in range(attempts):
        await box.click()
        await box.fill("")
        await box.press_sequentially(city, delay=90)
        try:
            await _wait_for_stable_suggestions(page)
            return True
        except PlaywrightTimeout:
            if attempt < attempts - 1:
                await page.wait_for_timeout(700)
    return False


async def _require_page() -> Page:
    """The tools are meant to be called in order, but an LLM will skip a step.

    Rather than fail with "browser not open", reopen it. The tool sequence stays
    the documented one; it just stops being a trap.
    """
    if not _session.is_open:
        return await _open_page()
    return _session.page  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------
@mcp.tool()
async def open_weather_forecast_israel() -> str:
    """Open a browser and navigate to the Israeli weather forecast site.

    Call this first. It leaves the browser sitting on the forecast page with the
    city search box ready. Returns the page title and URL.
    """
    async with _lock:
        try:
            page = await _open_page()
        except (PlaywrightTimeout, PlaywrightError) as exc:
            return f"Failed to open {FORECAST_URL}: {exc}"

        _session.last_suggestions = []
        _session.selected_city = ""
        return (
            f"Opened {page.url}\n"
            f"Title: {await page.title()}\n"
            "The city search box is ready. Next: enter_weather_forecast_city_israel(city)."
        )


@mcp.tool()
async def enter_weather_forecast_city_israel(city: str) -> str:
    """Type an Israeli city name into the forecast search box.

    Returns the autocomplete suggestions, numbered from 0. The list is what the
    site offers - it may contain neighbourhoods and landmarks as well as the city
    itself (typing "חיפה" offers "חוף הסטודנטים, חיפה" before plain "חיפה").
    Read the list before choosing, then call select_weather_forecast_city_israel.

    Args:
        city: City name, in Hebrew (e.g. "תל אביב", "חיפה", "באר שבע").
    """
    city = (city or "").strip()
    if not city:
        return "No city given. Pass a city name in Hebrew, e.g. 'תל אביב'."

    async with _lock:
        page = await _require_page()

        # A previous question may have left us on a city page. Go back to the
        # search page so the box exists.
        if not page.url.rstrip("/").endswith("/forecast"):
            try:
                await _goto_forecast(page)
            except PlaywrightTimeout as exc:
                return f"Could not return to the search page: {exc}"

        # The site's autocomplete listens for real keystrokes; fill() alone sets
        # the value without firing the handler, and no list ever opens.
        try:
            appeared = await _type_city_and_wait(page, city)
        except PlaywrightError as exc:
            return f"Failed to type {city!r}: {exc}"

        if not appeared:
            _session.last_suggestions = []
            return (
                f"Typed {city!r} but no suggestions appeared. "
                "The name may be misspelled, or the site may not cover this place."
            )

        items = page.locator(SUGGESTION_ITEMS)
        count = await items.count()
        suggestions = [
            re.sub(r"\s+", " ", (await items.nth(i).inner_text()).strip()) for i in range(count)
        ]

        _session.last_city_query = city
        _session.last_suggestions = suggestions

        listing = "\n".join(f"  [{i}] {name}" for i, name in enumerate(suggestions))
        return (
            f"Typed {city!r}. {count} suggestion(s):\n{listing}\n\n"
            "Next: select_weather_forecast_city_israel(index). "
            "Index 0 is the first item; pick the one that matches the user's city."
        )


@mcp.tool()
async def select_weather_forecast_city_israel(index: int = 0) -> str:
    """Click a city from the autocomplete list and load its forecast page.

    Args:
        index: Which suggestion to click, numbered from 0. Defaults to 0, the
            first item - but the first item is not always the city itself, so
            prefer the index whose text matches the city the user asked for.
    """
    async with _lock:
        page = await _require_page()
        items = page.locator(SUGGESTION_ITEMS)

        try:
            count = await items.count()
        except PlaywrightError as exc:
            return f"Could not read the suggestion list: {exc}"

        if count == 0:
            return (
                "The suggestion list is empty. Call enter_weather_forecast_city_israel(city) first."
            )
        if not 0 <= index < count:
            available = "\n".join(f"  [{i}] {s}" for i, s in enumerate(_session.last_suggestions))
            return f"Index {index} is out of range; {count} suggestion(s):\n{available}"

        chosen = re.sub(r"\s+", " ", (await items.nth(index).inner_text()).strip())
        try:
            await items.nth(index).click()
            await page.wait_for_load_state("domcontentloaded", timeout=NAV_TIMEOUT_MS)
            await page.wait_for_selector("h1", timeout=NAV_TIMEOUT_MS)
        except (PlaywrightTimeout, PlaywrightError) as exc:
            return f"Clicked {chosen!r} but the forecast page did not load: {exc}"

        _session.selected_city = chosen
        heading = ""
        try:
            heading = (await page.locator("h1").first.inner_text()).strip()
        except PlaywrightError:
            pass

        return (
            f"Selected [{index}] {chosen!r}.\n"
            f"Forecast page: {page.url}\n"
            f"Heading: {heading}\n"
            "The forecast is now open in the browser. "
            "Call extract_weather_forecast_israel() to read it."
        )


# ---------------------------------------------------------------------------
# Stage 2: pull the page's content back out so the LLM can answer from it.
#
# The page is mostly not forecast: cookie banners, ad slots, a Highcharts widget
# whose accessible text is "Combination chart with 3 data series", and ~1500
# words of SEO prose about Mount Carmel's snowfall in 1950. Handing all of that
# to the model buries the three numbers that matter. So the extraction reads the
# specific nodes that hold data, and ignores the rest of the document.
# ---------------------------------------------------------------------------
_EXTRACT_JS = r"""() => {
    const clean = s => (s || '').replace(/\s+/g, ' ').trim();

    const current = {};
    const cw = document.querySelector('.current-weather');
    if (cw) {
        current.temperature = clean(cw.querySelector('.temperature')?.textContent);
        cw.querySelectorAll('.weather-detail').forEach(d => {
            const key = clean(d.querySelector('span')?.textContent).replace(/:$/, '');
            const value = clean(d.querySelector('strong')?.textContent);
            if (key) current[key] = value;
        });
        current._times = [...cw.querySelectorAll('.time')].map(t => clean(t.textContent));
        const daylight = cw.querySelector('#daylight');
        if (daylight) current._daylight = clean(daylight.innerText);
    }

    // Two sources (European model, IMS) render the same days twice. Keep the
    // first block per day title and drop the duplicate.
    const seen = new Set();
    const days = [];
    document.querySelectorAll('.hourly_forecast_container details').forEach(d => {
        const title = clean(d.querySelector('summary')?.textContent);
        if (!title || seen.has(title)) return;
        const rows = [...d.querySelectorAll('tr')]
            .map(tr => [...tr.cells].map(c => clean(c.textContent)).filter(Boolean))
            .filter(cells => cells.length >= 2);
        if (!rows.length) return;
        seen.add(title);
        days.push({title, rows});
    });

    const sources = [...document.querySelectorAll('.forecast-source strong, .forecast-source h3')]
        .map(e => clean(e.textContent)).filter(Boolean);

    return {
        url: location.href,
        heading: clean(document.querySelector('h1')?.innerText),
        title: document.title,
        current,
        days,
        sources,
    };
}"""

_HOURLY_COLUMNS = ("hour", "temp", "precipitation", "humidity", "wind", "gust")


def _format_current(current: dict) -> list[str]:
    if not current:
        return ["_The page did not expose a current-conditions block._"]

    lines = []
    if current.get("temperature"):
        lines.append(f"- **Temperature now:** {current['temperature']}")
    for key, value in current.items():
        if key.startswith("_") or key == "temperature" or not value:
            continue
        lines.append(f"- **{key}:** {value}")
    for stamp in current.get("_times", []):
        if stamp:
            lines.append(f"- {stamp}")
    if current.get("_daylight"):
        lines.append(f"- {current['_daylight']}")
    return lines


def _format_day(day: dict, max_hours: int) -> list[str]:
    lines = [f"#### {day['title']}", "", "| " + " | ".join(_HOURLY_COLUMNS) + " |",
             "| " + " | ".join("---" for _ in _HOURLY_COLUMNS) + " |"]
    for cells in day["rows"][:max_hours]:
        padded = (list(cells) + [""] * len(_HOURLY_COLUMNS))[: len(_HOURLY_COLUMNS)]
        lines.append("| " + " | ".join(padded) + " |")
    dropped = max(0, len(day["rows"]) - max_hours)
    if dropped:
        lines.append(f"\n_({dropped} further hours omitted)_")
    lines.append("")
    return lines


@mcp.tool()
async def extract_weather_forecast_israel(max_days: int = 3, max_hours_per_day: int = 8) -> str:
    """Read the forecast off the page that is currently open, as clean markdown.

    Call this after select_weather_forecast_city_israel, and call it with NO
    arguments. The defaults already return the current conditions (temperature,
    wind, gust, wind direction, humidity, sunrise, sunset, last update) plus three
    days of hourly forecast, which is enough for almost any question. Shrinking
    the window costs you the data you are about to be asked about.

    Args:
        max_days: Days of hourly forecast. Leave unset unless the user explicitly
            asks about several days ahead. Clamped to 1-10.
        max_hours_per_day: Hours per day. Leave unset. Clamped to 3-24.
    """
    async with _lock:
        if not _session.is_open:
            return "No browser is open. Call open_weather_forecast_israel() first."

        page = _session.page
        assert page is not None

        if page.url.rstrip("/").endswith("/forecast"):
            return (
                "The browser is on the city-search page, not on a city's forecast. "
                "Call enter_weather_forecast_city_israel(city) and then "
                "select_weather_forecast_city_israel(index)."
            )

        try:
            data = await page.evaluate(_EXTRACT_JS)
        except PlaywrightError as exc:
            return f"Failed to read the page: {exc}"

    # Floors, not just ceilings. Models minimise numeric arguments when they are
    # unsure, and `max_hours_per_day=1` returns a single row - technically what
    # was asked for, useless for "what's the weather today".
    max_days = max(1, min(max_days, 10))
    max_hours_per_day = max(3, min(max_hours_per_day, 24))

    lines = [
        f"# {data.get('heading') or data.get('title') or 'Forecast'}",
        "",
        f"Source: {data['url']}",
    ]
    if data.get("sources"):
        lines.append(f"Models: {', '.join(dict.fromkeys(data['sources']))}")
    lines += ["", "## Current conditions", ""]
    lines += _format_current(data.get("current") or {})

    days = data.get("days") or []
    if days:
        lines += ["", "## Hourly forecast", ""]
        for day in days[:max_days]:
            lines += _format_day(day, max_hours_per_day)
        if len(days) > max_days:
            lines.append(f"_({len(days) - max_days} further days available on the page.)_")
    else:
        lines += ["", "_No hourly forecast table was found on this page._"]

    return "\n".join(lines)


@mcp.tool()
async def close_weather_browser_israel() -> str:
    """Close the browser opened by these tools. Safe to call at any time."""
    async with _lock:
        if _session.browser is not None:
            try:
                await _session.browser.close()
            except PlaywrightError:
                pass
        if _session.playwright is not None:
            try:
                await _session.playwright.stop()
            except Exception:  # pragma: no cover - shutdown races
                pass
        _session.browser = None
        _session.page = None
        _session.playwright = None
        _session.last_suggestions = []
        _session.selected_city = ""
        return "Browser closed."


def main():
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
