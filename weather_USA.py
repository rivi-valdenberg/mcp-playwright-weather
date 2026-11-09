"""MCP Server: US weather, from the National Weather Service API.

This file is the project template's, kept as it was given, with two fixes and one
change. It is the boring-on-purpose counterpart to weather_Israel.py: it asks an
API a question, while the other one drives a browser because no API exists.

No key needed - api.weather.gov is public, and covers the United States only.

Changes from the template
------------------------
1. `verify=False` was hard-coded, which disables TLS certificate verification for
   every request. The template needs it behind Netfree, whose filter terminates
   TLS. Silently shipping it is not acceptable, so it now lives behind
   `VERIFY_SSL=false` in .env, off by default. Turn it on only if you are behind
   such a filter, and know that you are.
2. `format_alert` read `props.get("instructions")`. The NWS field is
   `instruction`, singular, so the template always printed "No specific
   instructions provided".
3. `log_level="WARNING"`: without it the SDK prints "Processing request of type
   CallToolRequest" to stderr on every call, straight into the user's chat.
"""

import os
from typing import Any

import httpx
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

load_dotenv()

mcp = FastMCP("weather-USA", log_level="WARNING")

NWS_API_BASE = "https://api.weather.gov"
USER_AGENT = "weather-app/1.0"
TIMEOUT_SEC = 30.0

# Only for TLS-terminating filters such as Netfree. Default: verify certificates.
VERIFY_SSL = os.getenv("VERIFY_SSL", "true").lower() not in {"0", "false", "no"}


async def make_nws_request(url: str) -> dict[str, Any] | None:
    """Make a request to the NWS API with proper error handling."""
    headers = {"User-Agent": USER_AGENT, "Accept": "application/geo+json"}
    transport = httpx.AsyncHTTPTransport(verify=VERIFY_SSL)

    async with httpx.AsyncClient(transport=transport) as client:
        try:
            response = await client.get(url, headers=headers, timeout=TIMEOUT_SEC)
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, ValueError):
            return None


def format_alert(feature: dict) -> str:
    """Format an alert feature into a readable string."""
    props = feature.get("properties", {})
    return f"""
Event: {props.get("event", "Unknown")}
Area: {props.get("areaDesc", "Unknown")}
Severity: {props.get("severity", "Unknown")}
Description: {props.get("description", "No description available")}
Instructions: {props.get("instruction", "No specific instructions provided")}
"""


@mcp.tool()
async def get_alerts_in_USA(state: str) -> str:
    """Get weather alerts for a USA state.

    Args:
        state: Two-letter USA state code (e.g. CA, NY)
    """
    state = (state or "").strip().upper()
    if len(state) != 2 or not state.isalpha():
        return f"{state!r} is not a two-letter USA state code."

    url = f"{NWS_API_BASE}/alerts/active/area/{state}"
    data = await make_nws_request(url)

    if not data or "features" not in data:
        return "Unable to fetch alerts or no alerts found."

    if not data["features"]:
        return "No active alerts for this state."

    alerts = [format_alert(feature) for feature in data["features"][:10]]
    return "\n---\n".join(alerts)


@mcp.tool()
async def get_forecast_in_USA(latitude: float, longitude: float) -> str:
    """Get weather forecast for a location in USA.

    Args:
        latitude: Latitude of the location
        longitude: Longitude of the location
    """
    points_url = f"{NWS_API_BASE}/points/{latitude},{longitude}"
    points_data = await make_nws_request(points_url)

    if not points_data:
        return (
            f"Unable to fetch forecast data for {latitude},{longitude}. "
            "api.weather.gov only covers the United States."
        )

    forecast_url = points_data.get("properties", {}).get("forecast")
    if not forecast_url:
        return f"No forecast endpoint for {latitude},{longitude}."

    forecast_data = await make_nws_request(forecast_url)
    if not forecast_data:
        return "Unable to fetch detailed forecast"

    periods = forecast_data.get("properties", {}).get("periods") or []
    if not periods:
        return "The forecast came back empty."

    forecasts = []
    for period in periods[:5]:
        forecasts.append(
            f"""
        {period["name"]}:
        Temperature: {period["temperature"]}°{period["temperatureUnit"]}
        Wind: {period["windSpeed"]}  {period["windDirection"]}
        Forecast: {period["detailedForecast"]}
        """
        )

    return "\n---\n".join(forecasts)


def main():
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
