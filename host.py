"""Terminal chat that wires Gemini to the MCP servers.

    uv run host.py

This is the template's `ChatHost` with Anthropic swapped for Google Gemini. The
class, its methods, the `client_name__tool_name` namespacing and the chat loop are
the template's; only the model call and the tool-schema translation are new.

The host owns the agent loop: it hands Gemini the tool list, and every time the
model asks for a tool it runs it through an MCP client and feeds the result back.
Nothing routes by keyword - Gemini decides which server to use. Ask about Haifa and
it drives a browser; ask about San Francisco and it calls an API.
"""

import asyncio
import os
import random
import re
import sys
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from google import genai
from google.genai import types

from client import MCPClient

try:  # the SDK has moved this around between versions
    from google.genai import errors as genai_errors

    _APIError: type[Exception] = genai_errors.APIError
except Exception:  # pragma: no cover
    _APIError = Exception

HERE = Path(__file__).resolve().parent
load_dotenv(HERE / ".env")

MODEL = os.getenv("GEMINI_MODEL", "gemini-3.1-flash-lite").strip()
TEMPERATURE = float(os.getenv("GEMINI_TEMPERATURE", "0.2") or 0.2)
MAX_TOOL_ROUNDS = int(os.getenv("MAX_TOOL_ROUNDS", "10") or 10)

CLOSE_BROWSER_TOOL = "close_weather_browser_israel"

SYSTEM_PROMPT = """\
You are a weather assistant with two toolsets.

For places in Israel, use the weather_Israel tools. They drive a real browser and
must be called in order:
  1. open_weather_forecast_israel()
  2. enter_weather_forecast_city_israel(city)   - city name in Hebrew
  3. select_weather_forecast_city_israel(index) - read the suggestion list first!
  4. extract_weather_forecast_israel()          - read the page, then answer from it

Step 3 matters. The suggestion list often puts a beach or a university before the
city itself: typing "חיפה" offers ["חוף הסטודנטים, חיפה", "חיפה", ...]. Choose the
index whose text is the city the user asked about, not blindly index 0.

Step 4 takes no arguments. Call extract_weather_forecast_israel() plain. Its
defaults already cover today and the next two days; passing small numbers throws
away the data you are about to need.

The browser stays open between questions. If a forecast page is already loaded and
the user asks about a different city, go straight to
enter_weather_forecast_city_israel - there is no need to open the browser again.

For places in the United States, use the weather_USA tools instead.

Always finish by answering the user's actual question in their language, using the
numbers from extract_weather_forecast_israel. Do not tell the user to look at the
browser window - you have the data, so give them the answer.
"""

# --------------------------------------------------------------------------
# MCP tool schemas are JSON Schema; Gemini's are a restricted subset. Passing an
# MCP schema straight through fails on `title`, `default` and `additionalProperties`,
# which FastMCP emits for every tool, and on lowercase type names.
# --------------------------------------------------------------------------
_TYPE_MAP = {
    "string": "STRING",
    "integer": "INTEGER",
    "number": "NUMBER",
    "boolean": "BOOLEAN",
    "array": "ARRAY",
    "object": "OBJECT",
}
_KEEP = {"type", "format", "description", "nullable", "enum", "items", "properties", "required"}


def _convert(node: Any) -> dict[str, Any] | None:
    if not isinstance(node, dict):
        return None

    # Optional[int] arrives as anyOf: [{integer}, {null}]. Gemini wants one type
    # plus nullable, so collapse it.
    if "anyOf" in node:
        variants = [v for v in node["anyOf"] if isinstance(v, dict)]
        non_null = [v for v in variants if v.get("type") != "null"]
        nullable = len(non_null) < len(variants)
        if len(non_null) == 1:
            collapsed = _convert(non_null[0]) or {}
            if nullable:
                collapsed["nullable"] = True
            if node.get("description"):
                collapsed.setdefault("description", node["description"])
            return collapsed
        converted = [c for c in (_convert(v) for v in non_null) if c]
        return {"any_of": converted} if converted else None

    out: dict[str, Any] = {}
    for key, value in node.items():
        if key not in _KEEP:
            continue  # title, default, $schema, additionalProperties, ...
        if key == "type":
            mapped = _TYPE_MAP.get(str(value).lower())
            if mapped:
                out["type"] = mapped
        elif key == "properties":
            props = {k: _convert(v) for k, v in value.items()}
            props = {k: v for k, v in props.items() if v}
            if props:
                out["properties"] = props
        elif key == "items":
            item = _convert(value)
            if item:
                out["items"] = item
        else:
            out[key] = value

    if out.get("type") == "OBJECT" and not out.get("properties"):
        return None  # a no-argument tool: Gemini rejects an empty OBJECT
    return out or None


def to_function_declaration(name: str, description: str, schema: dict) -> types.FunctionDeclaration:
    parameters = _convert(schema)
    return types.FunctionDeclaration(
        name=name,
        description=description or name,
        parameters=types.Schema(**parameters) if parameters else None,
    )


# --------------------------------------------------------------------------
# Talking to Gemini. The free tier answers 503 ("high demand") and 429 ("quota")
# often enough that a chat without backoff dies on its fourth tool round, halfway
# through driving a browser. Retry, and prefer the delay the server asks for.
# --------------------------------------------------------------------------
_RETRYABLE = {429, 500, 502, 503, 504}
_MAX_ATTEMPTS = 5
_MAX_BACKOFF_SEC = 60.0
_RETRY_DELAY_PATTERNS = (
    re.compile(r"'retryDelay':\s*'(\d+(?:\.\d+)?)s'"),
    re.compile(r"retry in (\d+(?:\.\d+)?)s"),
)


class ModelUnavailable(RuntimeError):
    """The model could not be reached after retrying."""


def _server_retry_delay(exc: Exception) -> float | None:
    text = str(exc)
    for pattern in _RETRY_DELAY_PATTERNS:
        match = pattern.search(text)
        if match:
            return min(float(match.group(1)), _MAX_BACKOFF_SEC)
    return None


def _tool_result_text(result: Any) -> str:
    """Flatten an MCP CallToolResult into the text Gemini gets back."""
    chunks = []
    for item in getattr(result, "content", []) or []:
        text = getattr(item, "text", None)
        chunks.append(text if text is not None else f"[{type(item).__name__}]")
    body = "\n".join(chunks).strip() or "(the tool returned nothing)"
    if getattr(result, "isError", False):
        return f"TOOL ERROR: {body}"
    return body


def _fmt_args(args: dict[str, Any]) -> str:
    return ", ".join(f"{k}={v!r}" for k, v in args.items())


BANNER = """
🌤️  Weather chat — Gemini + MCP + Playwright
    Israeli cities go through a real browser; US locations go through an API.

    Try:  מה מזג האוויר בחיפה?
          כמה מעלות עכשיו בתל אביב ומה הלחות?
          what's the forecast for 37.77,-122.42 ?

    /tools   list the connected tools      /reset  clear the conversation
    quit     exit
"""


class ChatHost:
    def __init__(self):
        self.mcp_clients: list[MCPClient] = [
            MCPClient(str(HERE / "weather_Israel.py")),
            MCPClient(str(HERE / "weather_USA.py")),
        ]
        self.tool_clients: dict[str, tuple[MCPClient, str]] = {}
        self.clients_connected = False
        self.exit_stack = AsyncExitStack()

        api_key = os.getenv("GEMINI_API_KEY", "").strip()
        if not api_key:
            raise SystemExit(
                "GEMINI_API_KEY is not set. Copy .env.example to .env and paste your key "
                "from https://aistudio.google.com/apikey"
            )
        self.genai = genai.Client(api_key=api_key)
        self.history: list[types.Content] = []

    async def connect_mcp_clients(self):
        """Connect all configured MCP clients once."""
        if self.clients_connected:
            return

        for client in self.mcp_clients:
            if client.session is None:
                await client.connect_to_server()

        if not self.mcp_clients:
            raise RuntimeError("No MCP clients are connected")

        self.clients_connected = True

    async def get_available_tools(self) -> list[types.FunctionDeclaration]:
        """Collect tools from all MCP clients and map them back to their owner."""
        await self.connect_mcp_clients()
        self.tool_clients = {}
        available_tools: list[types.FunctionDeclaration] = []

        for client in self.mcp_clients:
            if client.session is None:
                print(f"Warning: MCP client {client.client_name} is not connected, skipping")
                continue

            try:
                response = await client.session.list_tools()
                for tool in response.tools:
                    exposed_name = f"{client.client_name}__{tool.name}"
                    if exposed_name in self.tool_clients:
                        raise RuntimeError(f"Duplicate tool name detected: {exposed_name}")

                    self.tool_clients[exposed_name] = (client, tool.name)
                    available_tools.append(
                        to_function_declaration(
                            exposed_name,
                            f"[{client.client_name}] {tool.description}",
                            tool.inputSchema or {"type": "object", "properties": {}},
                        )
                    )
            except Exception as e:
                print(f"Warning: Failed to get tools from {client.client_name}: {str(e)}")
                continue

        if not available_tools:
            raise RuntimeError("No tools available from any MCP client")

        return available_tools

    def _config(self, tools: list[types.FunctionDeclaration]) -> types.GenerateContentConfig:
        return types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT,
            temperature=TEMPERATURE,
            tools=[types.Tool(function_declarations=tools)],
            # We run the tools ourselves through MCP; the SDK must not try.
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        )

    async def _generate(self, config: types.GenerateContentConfig):
        last: Exception | None = None
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            try:
                return await self.genai.aio.models.generate_content(
                    model=MODEL, contents=self.history, config=config
                )
            except _APIError as exc:  # type: ignore[misc]
                code = getattr(exc, "code", None)
                if code not in _RETRYABLE or attempt == _MAX_ATTEMPTS:
                    raise ModelUnavailable(f"Gemini error {code}: {exc}") from exc
                delay = _server_retry_delay(exc) or min(2.0**attempt, 16.0)
                print(f"  ⏳ {code} from {MODEL}; retrying in {delay:.0f}s", flush=True)
                await asyncio.sleep(delay + random.uniform(0, 0.5))
                last = exc
        raise ModelUnavailable(f"gave up after {_MAX_ATTEMPTS} attempts: {last}")

    async def call_tool(self, exposed_name: str, args: dict[str, Any]) -> str:
        """Route a tool call back to the MCP client that advertised it."""
        if exposed_name not in self.tool_clients:
            return f"TOOL ERROR: unknown tool {exposed_name!r}"

        client, original_tool_name = self.tool_clients[exposed_name]
        if client.session is None:
            return f"TOOL ERROR: MCP client {client.client_name} is not connected"
        try:
            result = await client.session.call_tool(original_tool_name, args)
        except Exception as exc:  # a broken tool must not kill the chat
            return f"TOOL ERROR: {type(exc).__name__}: {exc}"
        return _tool_result_text(result)

    async def process_query(self, query: str) -> str:
        """Process a query using Gemini and the available tools."""
        available_tools = await self.get_available_tools()
        config = self._config(available_tools)
        self.history.append(types.Content(role="user", parts=[types.Part(text=query)]))

        for _ in range(MAX_TOOL_ROUNDS):
            response = await self._generate(config)
            candidate = (response.candidates or [None])[0]
            if candidate is None or candidate.content is None:
                return "(the model returned nothing)"

            self.history.append(candidate.content)
            calls = response.function_calls or []
            if not calls:
                return (response.text or "").strip() or "(the model returned no text)"

            parts: list[types.Part] = []
            for call in calls:
                args = dict(call.args or {})
                print(f"  🔧 {call.name}({_fmt_args(args)})", flush=True)

                result_text = await self.call_tool(call.name, args)

                preview = result_text.replace("\n", " ")[:100]
                print(f"     ↳ {preview}{'…' if len(result_text) > 100 else ''}", flush=True)
                parts.append(
                    types.Part.from_function_response(
                        name=call.name, response={"result": result_text}
                    )
                )
            self.history.append(types.Content(role="user", parts=parts))

        return f"Stopped after {MAX_TOOL_ROUNDS} tool rounds without a final answer."

    def reset(self) -> None:
        self.history.clear()

    async def chat_loop(self):
        """Run an interactive chat loop."""
        await self.get_available_tools()
        print(BANNER)
        print(f"model: {MODEL}\n")

        while True:
            try:
                query = (await asyncio.to_thread(input, "you> ")).strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break

            if not query:
                continue
            if query.lower() in {"quit", "exit", "/quit", "/exit"}:
                break
            if query == "/reset":
                self.reset()
                print("(conversation cleared)\n")
                continue
            if query == "/tools":
                for name in sorted(self.tool_clients):
                    print(f"  {name}")
                print()
                continue

            try:
                answer = await self.process_query(query)
            except ModelUnavailable as exc:
                # A dead model must not take the browser session with it.
                print(f"\n⚠️  {exc}")
                print("   Try again, or set a different GEMINI_MODEL in .env.\n")
                self.reset()
                continue
            except Exception as e:
                print(f"\nchat_loop Error: {str(e)}\n")
                continue

            print(f"\n🤖 {answer}\n")

    async def close_browser(self):
        """Leave no orphaned Chromium behind."""
        for exposed_name in self.tool_clients:
            if exposed_name.endswith(CLOSE_BROWSER_TOOL):
                await self.call_tool(exposed_name, {})
                return

    async def cleanup(self):
        """Clean up resources."""
        await self.close_browser()
        for client in reversed(self.mcp_clients):
            await client.cleanup()
        await self.exit_stack.aclose()


async def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    host = ChatHost()
    try:
        await host.chat_loop()
    finally:
        await host.cleanup()
        print("bye 👋")


if __name__ == "__main__":
    asyncio.run(main())
