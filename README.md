# MCP with Playwright — Israeli Weather Forecasts

An **MCP server that gives an LLM a hand on the mouse.**

There is no API here. The server drives a real Chromium the way a person would: it opens
[weather2day.co.il/forecast](https://www.weather2day.co.il/forecast), types a city name into
the search box, picks a city from the autocomplete list, and reads the forecast page that
loads.

The host is a terminal chat backed by **Google Gemini**. Nothing routes by keyword — Gemini
itself decides which server to use. A question about Haifa opens a browser; a question about
San Francisco goes to an API.

```
you> מה מזג האוויר עכשיו בחיפה? כמה מעלות ומה הלחות?

  🔧 open_weather_forecast_israel()
     ↳ Opened https://www.weather2day.co.il/forecast …
  🔧 enter_weather_forecast_city_israel(city='חיפה')
     ↳ Typed 'חיפה'. 4 suggestion(s): [0] חוף הסטודנטים, חיפה  [1] חיפה  …
  🔧 select_weather_forecast_city_israel(index=1)
     ↳ Selected [1] 'חיפה'. Forecast page: https://www.weather2day.co.il/haifa …
  🔧 extract_weather_forecast_israel()
     ↳ # מזג אוויר בחיפה … Current conditions …

🤖 נכון לעכשיו בחיפה, הטמפרטורה היא 23.3 מעלות והלחות עומדת על 80%.
```

---

## Quick start

```bash
uv sync                              # dependencies and virtualenv
uv run playwright install chromium   # the browser Playwright drives

# Paste your key into .env:
#   GEMINI_API_KEY=...               https://aistudio.google.com/apikey

uv run host.py                       # terminal chat
```

Tests — 27 of them, seven of which open a real browser against the live site:

```bash
uv run pytest                # everything
uv run pytest -m "not e2e"   # fast tests only, no network
```

---

## Layout

```
mcp-playwright-weather-israel/
├── host.py              # terminal chat: Gemini + the agent loop
├── client.py            # generic MCP client, holds several servers at once
├── weather_Israel.py    # MCP server: Playwright on weather2day.co.il
├── weather_USA.py       # MCP server: api.weather.gov, for contrast
├── .env                 # your API key goes here
└── tests/
    ├── test_tools_e2e.py           # real browser, real site, real DOM
    ├── test_schema_translation.py  # MCP JSON Schema → Gemini Schema
    └── test_retry.py               # backoff on 429 / 503
```

---

## The tools

| Tool | What it does |
| --- | --- |
| `open_weather_forecast_israel()` | Opens the browser on the forecast page |
| `enter_weather_forecast_city_israel(city)` | Types a city name, **returns the suggestion list** |
| `select_weather_forecast_city_israel(index=0)` | Clicks a suggestion, loads its forecast page |
| `extract_weather_forecast_israel()` | **RAG.** Reads the loaded page back as clean markdown |
| `close_weather_browser_israel()` | Closes the browser |

The browser **stays open between calls**. That is deliberate: the model calls the tools in
sequence and watches the page take shape, which is the entire point of the exercise.

### A note on "select the first item"

The assignment says the third tool selects the first item in the list. On the live site,
typing `חיפה` returns:

```
[0] חוף הסטודנטים, חיפה   ← the first item. It is a beach.
[1] חיפה                  ← the city
[2] חיפה, אוניברסיטה
[3] חיפה, טכניון
```

So `index=0` is the default, exactly as specified — but `enter_…` returns the list as
numbered text, and the system prompt instructs the model to pick the index matching the city
the user asked about. In testing, Gemini chooses `index=1` for Haifa and `index=0` for Beer
Sheva, where the city genuinely is first.

---

## Stage 2: RAG

`extract_weather_forecast_israel` is the retrieval step. It does not return the page; it
**reads what matters out of it.**

The forecast page is mostly not forecast. It has a cookie banner, ad slots, a Highcharts
widget whose accessible text reads `"Combination chart with 3 data series"`, and roughly
1,500 words of SEO prose about snowfall on Mount Carmel in 1950. Feeding all of that to the
model buries the three numbers that matter.

The extractor therefore reads **specific nodes** and ignores the rest of the document:

- `.current-weather` → temperature, last update, wind, gust, wind direction, humidity,
  sunrise, sunset
- `.hourly_forecast_container details` → the hourly forecast, day by day, as a markdown table
- Two sources (the European model and the Israel Meteorological Service) render the same days
  twice; the duplicate is filtered out

---

## Questions the agent can answer

| Question | What happens |
| --- | --- |
| `מה מזג האוויר בחיפה?` | browser → search → select → read page → answer in Hebrew |
| `כמה מעלות עכשיו בתל אביב ומה הלחות?` | same path, answers from the extracted values |
| `מה התחזית השעתית להיום בתל אביב?` | returns an hour-by-hour table |
| `האם צפוי גשם מחר בירושלים?` | reads the precipitation column and answers yes/no |
| `ומה בבאר שבע?` | the browser is already open, so it skips `open_…` |
| `what's the forecast for 37.77,-122.42 ?` | routes to `weather-usa`, no browser involved |
| `are there any weather alerts in California?` | `get_alerts("CA")` |

---

## Configuration (`.env`)

| Variable | Default | Purpose |
| --- | --- | --- |
| `GEMINI_API_KEY` | — | **Required.** [aistudio.google.com/apikey](https://aistudio.google.com/apikey) |
| `GEMINI_MODEL` | `gemini-3.1-flash-lite` | See the quota table below |
| `GEMINI_TEMPERATURE` | `0.2` | Not 0: the model must pick an index from a list whose order shifts |
| `MAX_TOOL_ROUNDS` | `10` | `open → enter → select → extract` is four rounds; leave room |
| `WEATHER_HEADLESS` | `false` | `false` lets you watch the browser work, which is the point |
| `WEATHER_SLOW_MO_MS` | `0` | `300`–`500` makes the browser legible to a human watching it |
| `WEATHER_TIMEOUT_MS` | `45000` | Navigation and selector timeout |

---

## When something goes wrong

| What you see | What it means | What to do |
| --- | --- | --- |
| `⏳ 503 …; retrying in 2s` | The model is busy. The host absorbs it, up to 5 attempts | Nothing. It works |
| `⚠️ Gemini error 429 … RequestsPerDay` | **Free-tier daily quota exhausted** for that model | Switch model in `.env` |
| `Typed 'X' but no suggestions appeared` | The city is not on the site, or a typo | Try another Hebrew city name |
| `404 … is not found` | The model id no longer exists | Use one from the table below |
| `CERTIFICATE_VERIFY_FAILED … CA cert does not include key usage extension` | You are behind a TLS-terminating filter, on Python 3.13 | See below |

### Why `.python-version` pins 3.12

Python 3.13 turned on `VERIFY_X509_STRICT` in `ssl.create_default_context()`. Strict
verification rejects any CA certificate that omits the `keyUsage` extension — and the root
CA that a TLS-terminating filter such as **Netfree** installs omits exactly that. Every HTTPS
call then dies before it leaves the machine, Gemini's included.

The fix is not to disable verification. It is to run the interpreter whose defaults the
filter's certificate satisfies, so [`.python-version`](.python-version) pins `3.12`.
Certificates are still verified, against the filter's CA, which the system already points at
through `SSL_CERT_FILE`. `weather_USA.py` has a `VERIFY_SSL` escape hatch for the same
situation; on 3.12 you do not need it, and it stays off.

Free-tier quota is **per model, per project, per day**. Measured against this key on
2026-07-10:

| Model | Requests/day (free tier) | Notes |
| --- | --- | --- |
| `gemini-3.1-flash-lite` | 500 | Fast and cheap. The default; a chat turn costs 4–6 requests |
| `gemini-3-flash-preview` | higher | Stronger; returns 503 when busy — the host retries |
| `gemini-2.5-flash` | — | **404.** Google closed it to projects created after ~2026-06 |

Both surviving models drive the tools correctly.

---

## Five things that broke, and what fixed them

**1. There are two search boxes.** The page ships `#city_search` in the sticky header (hidden
on the forecast page) *and* `#city_search_forecast` in the body (visible). Typing into the
wrong one fills an invisible box and the autocomplete never opens. The selectors in
[`weather_Israel.py`](weather_Israel.py) were read off the live DOM, not guessed.

**2. The suggestion list was read too early.** The autocomplete is rebuilt on every keystroke,
so "at least one suggestion exists" is true long before the list is complete. Reading it then
returned one item out of four — and the tool quietly picked the beach instead of the city.
`_wait_for_stable_suggestions` waits for the count to **hold steady** across two polls, not
merely to become non-zero.

**3. The input is visible before its event handler is bound.** Typing into it produces a
filled box and an empty list, permanently: the handler missed every keystroke and nothing will
fire it again. `_type_city_and_wait` clears the box and types again, up to three times, rather
than guessing how long the page's JavaScript takes to load.

**4. `AsyncExitStack` was closed in a different task than it was opened in.** `stdio_client`
and `ClientSession` are anyio context managers, and anyio requires a cancel scope to be exited
by the task that entered it — which is not true under pytest-asyncio, nor in a host shutting
down from a signal handler. In [`client.py`](client.py), each connection now runs a supervisor
task that enters the contexts, publishes the session, waits to be told to stop, and unwinds
them itself. Closing is just an event, and it is safe to call from anywhere.

**5. A single 503 killed the chat.** MCP errors were handled; the Gemini call had no retry at
all. `Host._generate` now retries up to five times and honours the `retryDelay` the server
asks for instead of guessing. In the last verification run it absorbed three consecutive 503s
and still produced the answer.

Also worth knowing: the site's ad and analytics scripts are blocked (`_BLOCKED_HOSTS`), but
**not** via `context.route("**/*")`. A catch-all route round-trips *every* request through
Python, on the same event loop that serves MCP over stdio — which starves the autocomplete's
own XHR, so the suggestion list never arrives. One narrow pattern per blocked host keeps the
matching inside Playwright, where it belongs.

---

## MCP JSON Schema → Gemini Schema

FastMCP emits JSON Schema with `title`, `default`, `additionalProperties` and lowercase type
names. Gemini's `types.Schema` accepts none of them. `_convert()` in [`host.py`](host.py)
strips the unsupported keys, maps `integer → INTEGER`, collapses `Optional[int]` (which
arrives as `anyOf: [integer, null]`) into `nullable`, and returns `None` for a tool with no
arguments — because Gemini rejects an `OBJECT` with no `properties`.

Each of the six tests in [`test_schema_translation.py`](tests/test_schema_translation.py) is a
`400` that happened before the test was written.

---

## Another library the AI era pulled onto the stage

Playwright predates GenAI and got a second life when agents started needing a browser.

**Pydantic** is another. Written in 2017 as an unglamorous validation library for type hints,
it is now the substrate almost all tool calling stands on: `FastMCP` derives each tool's JSON
Schema from it, and from there — through the `_convert()` above — comes the function
declaration sent to the model. Type validation became the language in which an LLM is told
what it is allowed to call.

---

## Note on the project template

The assignment links to
`github.com/malbruk/dovrot-ai-projects/tree/master/08-mcp/project-template`. That URL returns
**404**: the repository is not publicly accessible, so its code could not be downloaded. The
four files here (`client.py`, `host.py`, `weather_USA.py`, `weather_Israel.py`) were written
from scratch against the structure and filenames the assignment specifies.

Note also that the template's client and host are built on Anthropic's SDK while this project
targets Gemini, so both files would have had to be rewritten regardless.
