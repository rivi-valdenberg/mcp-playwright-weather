"""Proof that this project is built on the assignment's template, not around it.

`client.py` is taken from
`malbruk/dovrot-ai-projects @ master : 08-mcp/project-template/weather-chat_mcp/client.py`
byte for byte. If someone edits it, this test fails and they have to say so out loud.

The hash below was computed from that file on 2026-07-10. The repository is private;
`git clone` it with your own credentials to check.
"""

from __future__ import annotations

import hashlib
import inspect
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parents[1]

# sha256 of 08-mcp/project-template/weather-chat_mcp/client.py at commit 105ab4d
TEMPLATE_CLIENT_SHA256 = "b97f653d0ad223148f3cfb54f9d67482715f79b61e261bbdf3e89c8414fbfa62"


def test_client_py_is_the_template_file_unmodified():
    digest = hashlib.sha256((HERE / "client.py").read_bytes()).hexdigest()
    assert digest == TEMPLATE_CLIENT_SHA256, (
        "client.py no longer matches the template. That may be fine - but say so in "
        "the README's deviations table, and update this hash deliberately."
    )


def test_the_four_template_files_exist_with_their_given_names():
    for name in ("client.py", "host.py", "weather_USA.py", "weather_Israel.py"):
        assert (HERE / name).is_file(), f"the template's {name} is missing"


@pytest.mark.parametrize(
    "module_name, symbol",
    [
        ("client", "MCPClient"),
        ("host", "ChatHost"),
    ],
)
def test_the_template_classes_survive_the_port_to_gemini(module_name, symbol):
    import sys

    sys.path.insert(0, str(HERE))
    module = __import__(module_name)
    assert hasattr(module, symbol)


def test_chathost_keeps_the_templates_public_methods():
    import sys

    sys.path.insert(0, str(HERE))
    from host import ChatHost

    for method in (
        "connect_mcp_clients",
        "get_available_tools",
        "process_query",
        "chat_loop",
        "cleanup",
    ):
        assert inspect.iscoroutinefunction(getattr(ChatHost, method)), method


def test_weather_usa_keeps_the_templates_tool_names():
    import sys

    sys.path.insert(0, str(HERE))
    import weather_USA

    assert hasattr(weather_USA, "get_alerts_in_USA")
    assert hasattr(weather_USA, "get_forecast_in_USA")
    assert weather_USA.mcp.name == "weather-USA"


def test_weather_israel_keeps_the_templates_server_name_and_url():
    import sys

    sys.path.insert(0, str(HERE))
    import weather_Israel

    assert weather_Israel.mcp.name == "weather-Israel"
    assert weather_Israel.FORECAST_URL == "https://www.weather2day.co.il/forecast"


def test_tls_verification_is_on_by_default():
    # The template hard-codes verify=False for the Netfree filter. Shipping that
    # silently disables certificate checking for every request this server makes.
    import sys

    sys.path.insert(0, str(HERE))
    import weather_USA

    assert weather_USA.VERIFY_SSL is True
