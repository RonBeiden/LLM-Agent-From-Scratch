"""
Part 3 — Wire Your Loop to a LIVE MCP Exchange
===============================================
Same loop as Part 2. Tools live on a remote MCP server (the stock exchange)
plus one local Python helper (pct_change).

MCP endpoint: https://agent-stocks.vercel.app/api/mcp  (Streamable HTTP)
Auth: X-API-Key header with your ax_... key.

Setup:
  1. Register at https://agent-stocks.vercel.app  (password: bgu2026)
  2. Put your key in part3/.env:   AGENTS_EXCHANGE_API_KEY=ax_your_key_here
  3. Run:
       python part3/agent.py            # run every goal
       python part3/agent.py --only 3   # re-run just goal 3 (1-based)
"""

import argparse
import asyncio
import json
import os
import sys
import time
from contextlib import AsyncExitStack
from pathlib import Path

# ---------------------------------------------------------------------------
# Model provider — HP Azure OpenAI with Ollama fallback
# ---------------------------------------------------------------------------
_a2_path = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', 'assignment2'))
if os.path.isdir(_a2_path):
    sys.path.insert(0, _a2_path)

try:
    from settings import API_BASE, API_KEY, API_VERSION, DEPLOYMENT_MODEL, get_secret, sso_secret_name  # noqa
    import json as _json
    _secret = _json.loads(get_secret(sso_secret_name))
    _access_token = _secret['access_token']
    from openai import AzureOpenAI
    _client = AzureOpenAI(
        azure_endpoint=API_BASE,
        api_version=API_VERSION,
        api_key=API_KEY,
        default_headers={"Authorization": f"Bearer {_access_token}"},
    )
    MODEL = DEPLOYMENT_MODEL
    print(f"[agent] HP Azure OpenAI: {MODEL}")
except Exception as _hp_err:
    print(f"[agent] HP Azure unavailable ({_hp_err!r}), falling back to Ollama")
    from openai import OpenAI
    _client = OpenAI(base_url="http://localhost:11434/v1", api_key="ollama")
    MODEL = "granite4:micro"

import ssl
import httpx
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

MAX_STEPS = 12            # hard stop so a confused model can't loop forever
LIVE_URL   = "https://agent-stocks.vercel.app/api/mcp"
SECONDS_BETWEEN_CYCLES = 15

TRACES_DIR = Path(__file__).parent / "traces"

# ---------------------------------------------------------------------------
# Trading system prompt
# ---------------------------------------------------------------------------
SYSTEM_PROMPT = """\
You are an autonomous trading agent on a live stock exchange. You are given one \
goal at a time. Inspect the market and your portfolio with the provided tools \
as needed, then act on the goal, placing AT MOST ONE order unless explicitly told otherwise.

MONEY IS IN CENTS. All price and cash fields end in _cents (e.g. last_cents, cash_cents). \
To display as dollars, divide by 100 and show 2 decimals. \
For example: cash_cents=10000000 = $100,000.00; last_cents=29801 = $298.01. \
NEVER show a cents number with a dollar sign without converting first.

Quantities are whole shares. Every trade pays a 0.05% fee on the trade value. \
Cost of a buy = price_cents * qty * 1.0005 cents. Leave headroom for the fee.

US market hours only: orders are rejected outside Mon-Fri 09:30-16:00 ET. \
If rejected for market hours, say so clearly and do not retry.

Safety rules you MUST follow:
- Never overdraw: check get_portfolio() cash_cents before buying.
- Only sell shares you actually hold (no short selling).
- If place_order returns {"error": ...}, READ it and adapt. Never claim a \
trade succeeded when it did not.
- For read-only goals, do NOT place any order.

Use pct_change(old, new) when the goal asks you to compare or judge a price move.

When the goal is satisfied, reply with a concise plain-text summary of what \
you did and why, and make no further tool call."""

# ---------------------------------------------------------------------------
# Tracing
# ---------------------------------------------------------------------------
_TRACE: list[dict] = []


def trace(step: str, payload) -> None:
    """Emit one structured trace line and record it."""
    print(f"  [{step}] {json.dumps(payload, default=str)[:300]}")
    _TRACE.append({"step": step, "payload": payload})


# ---------------------------------------------------------------------------
# Local tool(s)
# ---------------------------------------------------------------------------
def pct_change(old_cents: int, new_cents: int) -> dict:
    """
    Percent change from old_cents to new_cents (e.g. 29000 -> 29801 = +2.76%).
    Read-only math the model can use to judge a price move.
    """
    if not old_cents:
        return {"error": "old_cents must be non-zero"}
    return {"pct": round((new_cents - old_cents) / old_cents * 100, 2)}


TOOL_FN: dict = {
    "pct_change": pct_change,
}

LOCAL_TOOLS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "pct_change",
            "description": "Percent change between two cents prices (from old_cents to new_cents). "
                           "Use to judge how far a price has moved; read-only, places no order. "
                           "e.g. old_cents=29000, new_cents=29801 gives pct=+2.76.",
            "parameters": {
                "type": "object",
                "properties": {
                    "old_cents": {"type": "integer", "description": "earlier price in cents"},
                    "new_cents": {"type": "integer", "description": "later price in cents"},
                },
                "required": ["old_cents", "new_cents"],
            },
        },
    },
]


# ---------------------------------------------------------------------------
# TODO 1 — mcp_tools_to_openai
# ---------------------------------------------------------------------------
def mcp_tools_to_openai(mcp_tools) -> list[dict]:
    """
    Translate the tool definitions returned by client.list_tools() into the
    OpenAI-compatible chat-completions tool schema.

    Each MCP tool object has:
        .name        (str)
        .description (str or None)
        .inputSchema (dict — already a valid JSON Schema, or None)

    Each tool entry must look like:
        {
            "type": "function",
            "function": {
                "name": ...,
                "description": ...,
                "parameters": ...,   # the inputSchema, or {"type":"object","properties":{}}
            }
        }
    """
    result = []
    for tool in mcp_tools:
        schema = tool.inputSchema if tool.inputSchema else {"type": "object", "properties": {}}
        result.append({
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description or "",
                "parameters": schema,
            },
        })
    return result


# ---------------------------------------------------------------------------
# TODO 2 — dispatch_tool  (Part 2's dispatch, extended for MCP)
# ---------------------------------------------------------------------------
async def dispatch_tool(name: str, args: dict, client: Client, mcp_names: set):
    """
    Route ONE tool call to where the tool actually lives:
      - if name in TOOL_FN:    plain Python call
      - elif name in mcp_names: await client.call_tool(name, args)
      - else:                  return {"error": ...}
    """
    if name in TOOL_FN:
        try:
            return TOOL_FN[name](**args)
        except Exception as e:
            return {"error": f"local tool {name!r} raised {type(e).__name__}: {e}"}
    elif name in mcp_names:
        try:
            result = await client.call_tool(name, args)
            # fastmcp returns CallToolResult with .content list of TextContent
            if hasattr(result, 'content') and result.content:
                texts = [item.text for item in result.content if hasattr(item, 'text')]
                combined = "\n".join(texts)
                try:
                    return json.loads(combined)
                except (json.JSONDecodeError, TypeError):
                    return combined
            elif hasattr(result, 'data') and result.data is not None:
                return result.data
            return str(result)
        except Exception as e:
            return {"error": f"MCP tool {name!r} raised {type(e).__name__}: {e}"}
    else:
        return {"error": f"unknown tool: {name!r}"}


# ---------------------------------------------------------------------------
# TODO 3 — run_agent (the loop — identical to Part 2 except dispatch)
# ---------------------------------------------------------------------------
async def run_agent(goal: str, client: Client) -> tuple[str, list[dict]]:
    """
    Drive one goal to completion. Returns (final_answer, tool_log).
    """
    # 1. Discover MCP tools
    mcp_tool_list = await client.list_tools()
    mcp_names = {t.name for t in mcp_tool_list}
    tools = LOCAL_TOOLS + mcp_tools_to_openai(mcp_tool_list)

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": goal},
    ]
    tool_log: list[dict] = []

    for step in range(1, MAX_STEPS + 1):
        # Think
        resp = _client.chat.completions.create(
            model=MODEL,
            messages=messages,
            tools=tools,
        )
        msg = resp.choices[0].message

        # Record
        msg_dict: dict = {"role": msg.role, "content": msg.content or ""}
        if msg.tool_calls:
            msg_dict["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments,
                    },
                }
                for tc in msg.tool_calls
            ]
        messages.append(msg_dict)

        # Done?
        if not msg.tool_calls:
            return msg.content or "(no response)", tool_log

        # Act
        for tc in msg.tool_calls:
            name = tc.function.name
            try:
                args = json.loads(tc.function.arguments)
            except json.JSONDecodeError:
                args = {}
            trace("action", {"tool": name, "args": args})
            # Respect exchange rate limit: wait 13s before any tool call
            # to avoid 429 errors during multi-step goals
            await asyncio.sleep(13)
            observation = await dispatch_tool(name, args, client, mcp_names)
            trace("observation", observation)
            tool_log.append({"tool": name, "args": args, "result": observation})
            messages.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": json.dumps(observation, default=str),
            })

    return "(stopped: hit MAX_STEPS without a final answer)", tool_log


# ---------------------------------------------------------------------------
# Goals
# ---------------------------------------------------------------------------
GOALS = [
    # 1. Read-only: report the portfolio.
    "Report your current portfolio: your cash in dollars and every position"
    " you hold.",

    # 2. Read-only: survey the market.
    "Survey the market: list the available symbols with their current prices,"
    " and tell me which one is the most expensive and which is the cheapest.",

    # 3. Read-only + local tool: compare two symbols by price.
    "Compare AAPL and MSFT: quote both and use pct_change to say how far"
    " MSFT's price is above or below AAPL's. Do not trade.",

    # 4. Read-only: summarize the latest news.
    "Read the latest news and summarize, in one line each, the three most"
    " recent headlines and which symbol each is about.",

    # 5. Trade: a news-driven buy, sized conservatively.
    "Buy the stock with the most supportive recent news. Spend at most 30% of"
    " your net worth on it and keep at least 20% of your portfolio in cash."
    " Place the order and confirm the fill from the result.",

    # 6. Trade: prune holdings that no longer have supporting news.
    "Review your holdings and sell any position you can no longer justify from"
    " recent news. If every holding is still justified, hold and explain why.",

    # 7. Read-only: leaderboard check.
    "Check the leaderboard and tell me our team's rank and net worth relative"
    " to the other teams. Do not trade.",

    # --- Extra goals added to test more capabilities ---
    # 8. Get trade history.
    "List the last 5 trades made by this account with their symbols, sides,"
    " quantities, and prices in dollars. Do not place any order.",

    # 9. Conservative diversification buy.
    "Get quotes for all available symbols. Pick the cheapest one. Buy 1 share"
    " if we have enough cash. Confirm the fill or explain why not.",

    # 10. Portfolio value summary.
    "Calculate our total portfolio value in dollars: sum cash plus the market"
    " value of all positions at current prices. Use pct_change to show how"
    " each position has moved from its average cost if available. Do not trade.",
]


# ---------------------------------------------------------------------------
# Trace saving
# ---------------------------------------------------------------------------
def save_trace(goal_num: int, goal: str, answer: str) -> Path:
    messages: list[dict] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": goal},
    ]
    for entry in _TRACE:
        if entry["step"] == "action":
            call = entry["payload"]
            messages.append({
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "function": {
                            "name": call.get("tool"),
                            "arguments": json.dumps(call.get("args", {}), default=str),
                        }
                    }
                ],
            })
        elif entry["step"] == "observation":
            messages.append({"role": "tool", "content": json.dumps(entry["payload"], default=str)})
    messages.append({"role": "assistant", "content": answer})

    TRACES_DIR.mkdir(parents=True, exist_ok=True)
    path = TRACES_DIR / f"goal_{goal_num}.json"
    path.write_text(json.dumps(messages, indent=2, default=str), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Goal runner
# ---------------------------------------------------------------------------
async def run_goals(api_key: str, goals: list[tuple[int, str]]) -> None:
    async with AsyncExitStack() as stack:
        exchange = await stack.enter_async_context(make_live_client(api_key))

        for idx, (num, goal) in enumerate(goals):
            print(f"\n{'='*60}\nGOAL {num}: {goal}\n{'='*60}")
            _TRACE.clear()
            try:
                answer, _tool_log = await run_agent(goal, exchange)
            except NotImplementedError:
                raise
            except Exception as e:
                print(f"  [goal-error] {e!r}")
                answer = f"(goal errored: {e})"

            path = save_trace(num, goal, answer)
            print(f"\n--- ANSWER: {answer}")
            print(f"--- trace saved to {path}")

            if idx < len(goals) - 1:
                time.sleep(SECONDS_BETWEEN_CYCLES)


# ---------------------------------------------------------------------------
# Client factory
# ---------------------------------------------------------------------------
def make_live_client(api_key: str) -> Client:
    # HP corporate network uses SSL interception — pass verify=False to bypass
    return Client(
        StreamableHttpTransport(
            url=LIVE_URL,
            headers={"X-API-Key": api_key},
            verify=False,
        )
    )


def load_api_key() -> str:
    api_key = os.environ.get("AGENTS_EXCHANGE_API_KEY", "")
    if not api_key:
        env_file = Path(__file__).parent / ".env"
        if env_file.exists():
            for line in env_file.read_text().splitlines():
                if line.startswith("AGENTS_EXCHANGE_API_KEY="):
                    api_key = line.split("=", 1)[1].strip()
    return api_key


async def main():
    parser = argparse.ArgumentParser(description="Live MCP trading agent")
    parser.add_argument("--only", type=int, default=None, metavar="N",
                        help="run only goal N (1-based) instead of all goals")
    args = parser.parse_args()

    api_key = load_api_key()
    if not api_key:
        print("ERROR: set AGENTS_EXCHANGE_API_KEY in your environment or part3/.env")
        print("       (register your team on https://agent-stocks.vercel.app to get a key)")
        return

    goals = list(enumerate(GOALS, 1))
    if args.only is not None:
        if not 1 <= args.only <= len(GOALS):
            print(f"ERROR: --only must be between 1 and {len(GOALS)}")
            return
        goals = [(args.only, GOALS[args.only - 1])]

    await run_goals(api_key, goals)


if __name__ == "__main__":
    asyncio.run(main())
