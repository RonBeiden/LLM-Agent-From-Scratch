# Part 3A — MCP Inspector Worksheet
# Ron Beiden 206628505

## Connection Details
- **Transport Type**: Streamable HTTP
- **URL**: `https://agent-stocks.vercel.app/api/mcp`
- **Header**: `X-API-Key: ax_BWv1adPRuOIX6Lx8mH5sQlrxeQ51XQoqhqgreHNOt4w`
- **Team**: Ron-Beiden
- **Date**: June 21, 2026

> **Note**: MCP Inspector GUI can be launched with `npx @modelcontextprotocol/inspector`.
> The results below were captured via direct MCP JSON-RPC calls (HTTP POST to the endpoint).

---

## Tool Results

### 1. get_symbols

```json
{
  "items": [
    {"name": "Apple Inc.",         "symbol": "AAPL",  "last_cents": 29801,  "tick_cents": 1, "prev_close_cents": 29595},
    {"name": "Amazon.com Inc.",    "symbol": "AMZN",  "last_cents": 24439,  "tick_cents": 1, "prev_close_cents": 23750},
    {"name": "Alphabet Inc.",      "symbol": "GOOGL", "last_cents": 36803,  "tick_cents": 1, "prev_close_cents": 36379},
    {"name": "IBM Corp.",          "symbol": "IBM",   "last_cents": 24910,  "tick_cents": 1, "prev_close_cents": 26235},
    {"name": "Meta Platforms Inc.","symbol": "META",  "last_cents": 57722,  "tick_cents": 1, "prev_close_cents": 56758},
    {"name": "Microsoft Corp.",    "symbol": "MSFT",  "last_cents": 37940,  "tick_cents": 1, "prev_close_cents": 37891},
    {"name": "NVIDIA Corp.",       "symbol": "NVDA",  "last_cents": 21069,  "tick_cents": 1, "prev_close_cents": 20465},
    {"name": "Tuttle Capital SPAC ETF", "symbol": "SPCX", "last_cents": 18500, "tick_cents": 1, "prev_close_cents": 19182}
  ]
}
```

**8 symbols**: AAPL ($298.01), AMZN ($244.39), GOOGL ($368.03), IBM ($249.10), META ($577.22), MSFT ($379.40), NVDA ($210.69), SPCX ($185.00).
Most expensive: **META** at $577.22. Cheapest: **SPCX** at $185.00.

---

### 2. get_quote (symbol: META)

```json
{"symbol": "META", "last_cents": 57722, "prev_close_cents": 56758}
```

**META**: $577.22 current, $567.58 prev close (+1.70% today).

---

### 3. get_news (limit=5) — top 5 returned

```json
{"items": [
  {"at": "2026-06-21T07:05:21Z", "id": 69208, "symbol": null,   "headline": "US VP Vance arrives in Switzerland for peace talks with Iran"},
  {"at": "2026-06-21T06:11:18Z", "id": 68804, "symbol": "NVDA", "headline": "Why This $26.5 Trillion Projection Is Critical to Understand Before Buying SpaceX Stock"},
  {"at": "2026-06-21T04:14:13Z", "id": 67927, "symbol": "AMZN", "headline": "Amazon quietly building a moat to outlast the AI boom"},
  {"at": "2026-06-21T00:15:01Z", "id": 66134, "symbol": "GOOGL","headline": "Who needs rate cuts? Even the Fed's new chair admits companies are easily raising capital"},
  {"at": "2026-06-21T00:13:01Z", "id": 66117, "symbol": "AAPL", "headline": "Why Intel Is Up 7.6% After Prospective Apple U.S. Chip Foundry Partnership News"}
]}
```

**Market context**: Iran peace talks (macro positive), NVDA/AMZN AI strength, Apple-Intel chip partnership news.

---

### 4. get_portfolio (BEFORE trading)

```json
{
  "team_name": "Ron-Beiden",
  "cash_cents": 10000000,
  "positions": [],
  "open_orders": []
}
```

**Starting state**: $100,000.00 cash, no positions.

---

### 5. place_order — buy 1 share of SPCX

**Request**: `{"symbol": "SPCX", "side": "buy", "qty": 1}`

```json
{"error": "market closed", "status": 409}
```

**Market is closed** — this is June 21, 2026 (Saturday). The exchange only accepts orders Mon–Fri 09:30–16:00 ET. This is the expected behavior — the agent must handle this gracefully by checking market hours and reporting the rejection instead of retrying.

---

### 6. get_portfolio (AFTER order attempt)

```json
{
  "team_name": "Ron-Beiden",
  "cash_cents": 10000000,
  "positions": [],
  "open_orders": []
}
```

**No change** — order was rejected due to market hours. Cash remains $100,000.00.

---

## Observations

1. **Prices are in cents** (`last_cents`, `prev_close_cents`, `cash_cents`) — the agent must divide by 100 to display dollars.

2. **Market hours enforcement** — orders outside Mon–Fri 09:30–16:00 ET return `{"error": "market closed", "status": 409}`. The agent must handle this without retrying in a tight loop.

3. **Rate limit** — 12 seconds between trading calls. Testing confirmed: calling twice within 12s returns `{"error": "rate limited: min 12 seconds between calls", "status": 429}`.

4. **Tool schema** — `get_symbols` returns `{"items": [...]}` wrapper (list-returning tools use this pattern). `get_portfolio` returns flat object with `cash_cents` + `positions` array.

5. **Instant fills** — there is no order book. `place_order` fills immediately at the current live price when market is open.

6. **8 tickers available**: AAPL, AMZN, GOOGL, IBM, META, MSFT, NVDA, SPCX. Price range: $185 (SPCX) to $577 (META).

