"""End-to-end check of the MCP server: real browser, real site, real DOM.

Slow (it opens Chromium and hits the network) but it is the only test that can
catch the failure that actually matters here - the site changing its markup.

    uv run pytest -m e2e -s

On structure: `client.py` is the template's, unmodified. It enters `stdio_client`
and `ClientSession` into AsyncExitStacks, and anyio requires a cancel scope to be
exited by the task that entered it. A pytest-asyncio *fixture* finalises in a
different task than it sets up, so the connection is opened and closed inside each
test body instead - which is also exactly how host.py uses it.
"""

from __future__ import annotations

import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from client import MCPClient  # noqa: E402

HERE = Path(__file__).resolve().parents[1]
pytestmark = [pytest.mark.e2e, pytest.mark.asyncio]


@pytest.fixture(scope="module", autouse=True)
def headless_env(tmp_path_factory):
    """Run the server headless, without touching the template's client.py.

    The template passes `env=None` to StdioServerParameters, and the MCP SDK then
    hands the child a minimal whitelisted environment - so `WEATHER_HEADLESS` set
    here would never reach it. The server calls `load_dotenv()`, which searches
    upward from the working directory, so a throwaway .env and a chdir get there
    instead.
    """
    workdir = tmp_path_factory.mktemp("headless")
    (workdir / ".env").write_text("WEATHER_HEADLESS=true\n", encoding="utf-8")
    previous = os.getcwd()
    os.chdir(workdir)
    try:
        yield
    finally:
        os.chdir(previous)


@asynccontextmanager
async def israel_server():
    """Start weather_Israel.py and yield a `call(tool, **args)` helper."""
    client = MCPClient(str(HERE / "weather_Israel.py"))
    await client.connect_to_server()

    async def call(tool: str, **args) -> str:
        result = await client.session.call_tool(tool, args)
        return "\n".join(getattr(c, "text", "") for c in result.content).strip()

    try:
        yield call
    finally:
        try:
            await client.session.call_tool("close_weather_browser_israel", {})
        except Exception:
            pass
        await client.cleanup()


def _suggestions(tool_output: str) -> list[str]:
    return [
        line.split("] ", 1)[1].strip()
        for line in tool_output.splitlines()
        if line.strip().startswith("[")
    ]


async def test_tools_are_advertised():
    client = MCPClient(str(HERE / "weather_Israel.py"))
    await client.connect_to_server()
    try:
        response = await client.session.list_tools()
        assert {t.name for t in response.tools} == {
            "open_weather_forecast_israel",
            "enter_weather_forecast_city_israel",
            "select_weather_forecast_city_israel",
            "extract_weather_forecast_israel",
            "close_weather_browser_israel",
        }
    finally:
        await client.cleanup()


async def test_the_client_name_follows_the_template_convention():
    # host.py exposes tools as `{client_name}__{tool_name}`, and client_name comes
    # from the script filename. Renaming a server file renames every one of its tools.
    assert MCPClient(str(HERE / "weather_Israel.py")).client_name == "weather_Israel"
    assert MCPClient(str(HERE / "weather_USA.py")).client_name == "weather_USA"


async def test_full_sequence_for_haifa():
    async with israel_server() as call:
        opened = await call("open_weather_forecast_israel")
        assert "weather2day.co.il/forecast" in opened

        typed = await call("enter_weather_forecast_city_israel", city="חיפה")
        assert "suggestion" in typed

        options = _suggestions(typed)
        # The autocomplete is rebuilt on every keystroke. If we read it mid-render
        # we get one item instead of four, and index 0 is a beach, not the city.
        assert len(options) >= 2, f"suggestion list read too early: {options}"
        assert "חיפה" in options, f"no exact city match in {options}"
        exact = options.index("חיפה")
        assert exact != 0, "the site is expected to rank 'חוף הסטודנטים, חיפה' first"

        selected = await call("select_weather_forecast_city_israel", index=exact)
        forecast_url = selected.split("Forecast page: ")[1].split("\n")[0]
        assert "weather2day.co.il" in forecast_url
        assert not forecast_url.rstrip("/").endswith("/forecast")

        page = await call("extract_weather_forecast_israel")
        assert "Current conditions" in page
        assert "Temperature now" in page
        assert "Hourly forecast" in page
        assert "°" in page


async def test_suggestions_are_read_after_the_list_settles():
    """Regression: blocking ads made the page fast enough to expose the race."""
    async with israel_server() as call:
        await call("open_weather_forecast_israel")
        for _ in range(3):
            typed = await call("enter_weather_forecast_city_israel", city="חיפה")
            assert _suggestions(typed) == [
                "חוף הסטודנטים, חיפה",
                "חיפה",
                "חיפה, אוניברסיטה",
                "חיפה, טכניון",
            ]


async def test_extract_before_selecting_a_city_explains_itself():
    async with israel_server() as call:
        await call("open_weather_forecast_israel")
        assert "city-search page" in await call("extract_weather_forecast_israel")


async def test_unknown_city_does_not_crash():
    async with israel_server() as call:
        await call("open_weather_forecast_israel")
        message = await call("enter_weather_forecast_city_israel", city="זזזזזזזז")
        assert "no suggestions" in message.lower()


async def test_select_without_typing_explains_itself():
    async with israel_server() as call:
        await call("open_weather_forecast_israel")
        message = await call("select_weather_forecast_city_israel")
        assert "enter_weather_forecast_city_israel" in message
