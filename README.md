# Assignment 3: Build an Agent from Scratch

**Ron Beiden | BGU LLM Models Course**

Building a ReAct-style agent from scratch -- no frameworks, no magic. Just a loop you write, an LLM that does the thinking, and tools the agent can call.

## Structure

```
assignment3/
├── part1/
│   └── nanoclaw_writeup.md       # Technical write-up: NanoClaw source code analysis
├── part2/
│   ├── agent.py                  # Coding agent: ReAct loop + 5 tools
│   └── traces/
│       ├── task_1.json           # Trace: explore (list+read+run hello.py)
│       ├── task_2.json           # Trace: debug (find+fix+run buggy.py)
│       ├── task_3.json           # Trace: generate (write reverse.py)
│       └── task_4.json           # Trace: search (find TODO files)
├── part3/
│   ├── agent.py                  # Stock trading agent: same loop + MCP exchange
│   ├── inspector_worksheet.md    # Part 3A: manual MCP Inspector results
│   ├── .env.example              # Copy to .env with your API key
│   └── traces/
│       └── goal_*.json           # Traces: one per GOALS entry
└── notebooks/
    └── 05_report.ipynb           # Full report notebook
```

## Part 1 — Read a Real Agent

Analyzed **NanoClaw** (`https://github.com/nanocoai/nanoclaw`, commit `625264b`). The write-up covers:
- The reasoning loop (`poll-loop.ts`)
- Tool dispatch via MCP server (`mcp-tools/core.ts`)
- Memory: short-term (SQLite continuation) + long-term (CLAUDE.md)
- Multi-agent routing (`agent-to-agent/`)
- What I'd change: summarize before rotation instead of silent amnesia

## Part 2 — Coding Agent

A ReAct loop that reads, writes, runs, and searches files in a tiny sandbox.

**Model**: HP Azure OpenAI GPT-4.1 (with Ollama granite4:micro fallback)

```bash
python part2/agent.py
```

Tasks:
1. Explore: list files, read hello.py, run it
2. Debug: find+fix bug in buggy.py, confirm output
3. Generate: write reverse.py, run it
4. Search: find all TODO comments, read those files

## Part 3 — MCP Stock Trading Agent

Same loop, tools served over MCP from a live exchange.

**Setup**:
1. Register at https://agent-stocks.vercel.app (password: `bgu2026`)
2. Copy `.env.example` to `.env` and set your key
3. Run:

```bash
python part3/agent.py            # all goals
python part3/agent.py --only 3   # just goal 3
```

**Goals**: portfolio report, market survey, symbol comparison, news summary, news-driven buy, position pruning, leaderboard check, trade history, cheapest-share buy, portfolio valuation.

## Key Insight

> The loop doesn't change -- only where the tools live.  
> Part 2 tools = Python functions in your file.  
> Part 3 tools = remote MCP server + one local Python function.

The same `run_agent()` loop handles both cases by routing each tool call to its right place via `dispatch_tool()`.
