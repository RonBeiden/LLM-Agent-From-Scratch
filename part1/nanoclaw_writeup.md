# NanoClaw — Technical Write-up
**Assignment 3, Part 1 | Ron Beiden 206628505**

---

## 1. Agent & Version

- **Repo**: `https://github.com/nanocoai/nanoclaw`
- **Commit read**: `625264b` (main branch, June 19 2026)
- **Language**: TypeScript (Node.js 22, Bun inside containers)
- **Key files examined**:
  - `container/agent-runner/src/poll-loop.ts` — the reasoning loop
  - `container/agent-runner/src/mcp-tools/core.ts` — tool definitions
  - `container/agent-runner/src/providers/claude.ts` — model provider
  - `src/modules/agent-to-agent/agent-route.ts` — multi-agent routing
  - `groups/global/CLAUDE.md` — global system prompt

---

## 2. Architecture (one diagram)

```
┌─────────────────────────────────────────────────────────────┐
│                   HOST PROCESS (Node.js)                    │
│  ┌──────────────┐   inbound.db   ┌────────────────────────┐ │
│  │  Channel     │ ─────────────► │   container/           │ │
│  │  Adapters    │                │   agent-runner (Bun)   │ │
│  │  (Slack,     │ ◄────────────  │                        │ │
│  │  Discord,..) │  outbound.db   │   ┌──────────────────┐ │ │
│  └──────────────┘                │   │ runPollLoop()    │ │ │
│                                  │   │  OBSERVE         │ │ │
│  ┌──────────────┐                │   │  (getPendingMsgs)│ │ │
│  │  src/router  │                │   │  THINK           │ │ │
│  │  (entity     │                │   │  (provider.query)│ │ │
│  │  routing)    │                │   │  ACT             │ │ │
│  └──────────────┘                │   │  (writeMsg out)  │ │ │
│                                  │   └──────────────────┘ │ │
│  ┌──────────────┐                │   ┌──────────────────┐ │ │
│  │  src/host-   │                │   │  MCP Server      │ │ │
│  │  sweep.ts    │                │   │  (send_message,  │ │ │
│  │  (watchdog)  │                │   │  send_file, ..)  │ │ │
│  └──────────────┘                │   └──────────────────┘ │ │
│                                  └──────────────┬─────────┘ │
└─────────────────────────────────────────────────┼───────────┘
                                                  │ Claude API
                                        ┌─────────▼──────────┐
                                        │  Anthropic Claude  │
                                        │  (Agent SDK via    │
                                        │  providers/claude) │
                                        └────────────────────┘

Memory:
  Short-term:  continuation id in session-state.ts (SQLite)
  Long-term:   groups/<name>/CLAUDE.md  (semantic, persistent)
               src/modules/self-mod/    (procedural, writable)
```

---

## 3. The Loop

### Where it lives

**File**: `container/agent-runner/src/poll-loop.ts`  
**Functions**: `runPollLoop()` (outer), `processQuery()` (inner per-turn)

### Core 10 lines (observe → think → act)

```typescript
// container/agent-runner/src/poll-loop.ts  — runPollLoop(), lines ~100-160
while (true) {
  const messages = getPendingMessages(isFirstPoll)        // OBSERVE
    .filter((m) => m.kind !== 'system');
  if (messages.length === 0) { await sleep(POLL_INTERVAL_MS); continue; }

  markProcessing(ids);
  const prompt = formatMessagesWithCommands(keep, ...);

  const query = config.provider.query({                   // THINK
    prompt, continuation, cwd, systemContext,
  });

  const result = await processQuery(query, routing, ...); // ACT (inside processQuery)
  // processQuery streams events; on result event → dispatchResultText() → writeMessageOut()
}
```

### Termination

The loop runs `while (true)` — it **never terminates by design**: the container is killed by the host. There is no `MAX_STEPS` counter. However, at the *per-turn* level, the Claude Agent SDK's session ends when Claude stops issuing tool calls (its own internal stopping criterion). A `continuation` string is persisted in SQLite (`session-state.ts`) so the next wake can resume the Claude `.jsonl` transcript.

For cold-start protection, `maybeRotateContinuation()` (in `providers/claude.ts`) checks if the stored transcript is too large, and clears it so the next run starts fresh — avoiding infinite context growth.

### Single-loop or multi-agent?

**Single-loop agent** — confirmed by code, not marketing. `runPollLoop()` contains exactly one while-loop that calls one provider. There is no planner that spawns sub-agents. The multi-agent capability is wired *around* the loop at the host level (see section 7), not inside it.

---

## 4. Tools & Dispatch

### How tools are defined

Tools are defined as `McpToolDefinition` objects with a JSON Schema `inputSchema` and an async `handler`.

**File**: `container/agent-runner/src/mcp-tools/core.ts`

```typescript
// core.ts — sendMessage tool definition
export const sendMessage: McpToolDefinition = {
  tool: {
    name: 'send_message',
    description: 'Send a message to a named destination...',
    inputSchema: {
      type: 'object',
      properties: {
        to:   { type: 'string', description: 'Destination name. Optional if only one.' },
        text: { type: 'string', description: 'Message content' },
      },
      required: ['text'],
    },
  },
  async handler(args) {
    const text = args.text as string;
    if (!text) return err('text is required');
    const routing = resolveRouting(args.to as string | undefined);
    if ('error' in routing) return err(routing.error);
    const id = generateId();
    writeMessageOut({ id, kind: 'chat', platform_id: routing.platform_id, ... });
    return ok(`Message sent to ${routing.resolvedName} (id: ${seq})`);
  },
};
```

Tools are registered by calling `registerTools([sendMessage, sendFile, editMessage, addReaction])` at the bottom of `core.ts`, which hooks them into the MCP server started in `mcp-tools/server.ts`.

### Tool dispatch

NanoClaw is an **MCP server** — it does not manually dispatch tool calls from model output. Instead, it delegates entirely to the **Claude Agent SDK** (`providers/claude.ts`). The SDK:
1. Receives the tool definitions from the MCP server at session start
2. When Claude outputs a tool call, the SDK calls the MCP tool handler directly (subprocess/stdio protocol)
3. The result is fed back to Claude automatically by the SDK

NanoClaw never touches `tool_calls` arrays itself — the provider abstraction (`AgentProvider.query()` → streaming `ProviderEvent`) hides this.

### Tool failure modes

**File**: `container/agent-runner/src/mcp-tools/core.ts`

Errors return `{ content: [{ type: 'text', text: 'Error: ...' }], isError: true }` — they surface to the model as a tool observation. The model can read the error and adapt. No retry, no crash. Example:

```typescript
function err(text: string) {
  return { content: [{ type: 'text' as const, text: `Error: ${text}` }], isError: true };
}
```

### MCP (Section C)

NanoClaw acts as an **MCP server** — it does NOT consume external MCP servers by default. The MCP server runs inside the container and exposes tools to the Claude Agent SDK (which is the MCP client). The `.mcp.json` at the repo root and `groups/<name>/CLAUDE.md` can reference additional external MCP servers via the Claude Code client config, but the core nanoclaw code itself is server-only.

---

## 5. Memory

### Short-term / context memory

**File**: `container/agent-runner/src/db/session-state.ts`

Conversation history is not carried in-process. Instead, the Claude Agent SDK maintains a `.jsonl` transcript on disk (in the container's workspace). Between container wakes (each new message), the `continuation` string (a session ID) is loaded from SQLite and passed to `provider.query({ continuation })`, which hands it back to the SDK to reload the full transcript. This is effectively context-window memory — nanoclaw does not trim or summarize it (the rotation heuristic in `maybeRotateContinuation` simply discards old sessions, not summarizes).

### Long-term memory

**File**: `groups/global/CLAUDE.md`, `groups/main/CLAUDE.md`

Long-term memory is **semantic** — stored as Markdown files (`CLAUDE.md`) in each agent group's directory. These are injected into every session as system context. They contain: user preferences, recurring facts, and behavioral rules. They are written by Claude via the `self-mod` module.

**Write path**: `src/modules/self-mod/request.ts` → Claude calls `Bash("edit CLAUDE.md ...")` → persists to disk  
**Read path**: `container/agent-runner/src/memory-scaffold.ts` → loads `CLAUDE.md` into the `systemContext.instructions` passed to `provider.query()`

### Skill/learning logic

**File**: `container/agent-runner/src/mcp-tools/self-mod.ts`

The agent can modify its own `CLAUDE.md` (`update_memory` MCP tool) and install new skills (`install_skill` tool). This maps to **procedural memory** — the agent can create new behaviors by writing code into its own workspace.

---

## 6. Prompts, Planning, Eval

### System prompt

The primary system prompt is **not a single string in code** — it is the content of `groups/global/CLAUDE.md` (global) and `groups/<name>/CLAUDE.md` (per-agent-group), concatenated and passed as `systemContext.instructions`.

**Logic hidden in the prompt**:

```markdown
# groups/global/CLAUDE.md (excerpt)
All output must be wrapped in <message to="name">...</message> blocks.
Text outside these blocks is treated as scratchpad and NOT sent.
If you have only one destination, you can omit the to= attribute.
```

This routing protocol **belongs in code** (it's structural dispatch logic), but is enforced via the prompt. The `dispatchResultText()` function in `poll-loop.ts` does parse `<message>` blocks, but the model only knows to emit them because the prompt says so — a prompt failure silently drops all output.

There is a re-send nudge (reflection) in `processQuery()`:
```typescript
if (sent === 0 && scratchpad) {  // model forgot to wrap output
  query.push(`<system>Your response was not delivered — it was not wrapped...`);
}
```
This is a one-shot **self-correction** trigger. No re-planning or multi-step reflection.

### Planning

Planning is **implicit inside Claude** — there is no explicit planner. Claude decides what to do each turn based on its system prompt and conversation history. There are no chain-of-thought scaffolds, no explicit step decomposition, no re-planning triggers beyond the wrapping-nudge.

### Observability

- `log()` → `console.error()` → Docker container logs (viewable via `docker logs`)
- `touchHeartbeat()` → file-based liveness probe watched by `src/host-sweep.ts`
- SQLite `messages_in` / `messages_out` — full audit trail of every message
- `upload-trace.ts` — uploads the full conversation `.jsonl` to Hugging Face on `/upload-trace` command

---

## 7. Multi-agent

### Pattern

NanoClaw implements a **peer-to-peer / flat-swarm** multi-agent pattern, not manager-worker.

**File**: `src/modules/agent-to-agent/agent-route.ts`

Any agent can send a message to any other named agent group via `writeMessageOut({ channel_type: 'agent', platform_id: dest.agentGroupId })`. The host router (`src/router.ts`) delivers it to the target agent's `inbound.db`.

### Communication format

Standard `writeMessageOut` / `messages_in` rows — the same pipeline as human messages. No special A2A protocol. The agent uses the `send_message(to="worker-1", ...)` MCP tool, which resolves the destination via the `agent_destinations` table.

### Cost-explosion risk

There is **no rate limit or depth guard** on A2A messages. Two agents can send messages to each other in a tight loop (each response triggers the other), running up unbounded API tokens. The only backstop is the session heartbeat/sweep (`src/host-sweep.ts`) which kills stale containers after a timeout — but that timeout is 60s+ by default.

---

## 8. What I'd Change

### The design decision: Unbounded continuation without summarization

**File**: `container/agent-runner/src/db/session-state.ts` + `providers/claude.ts`

**The current design**: NanoClaw persists the Claude session `.jsonl` transcript across container wakes via the `continuation` ID. The transcript grows indefinitely until `maybeRotateContinuation()` decides it's too large — at which point it is **discarded entirely** (cleared, fresh start).

**The failure mode**: **Long-horizon drift + silent amnesia**. A user who has had 200 turns of conversation with their agent will one day get a completely fresh agent with no memory of anything. The agent doesn't know this happened. The user doesn't see a warning. Every preference, past decision, and established context is silently lost. This is worse than a "graceful reset" — it's an invisible regression.

**My alternative**: Before rotating the session, trigger a summarization turn:

```typescript
// Before clearContinuation():
await provider.query({
  prompt: "<system>Your session transcript is too long. Summarize the most important facts, preferences, and decisions from this conversation into your memory file (CLAUDE.md). Be concise but complete.</system>",
  continuation: current,
  cwd: config.cwd,
});
// THEN clear the continuation
clearContinuation(providerName);
```

This converts **working context memory** into **durable semantic memory** (CLAUDE.md), preventing the cold-restart amnesia. The infrastructure to write CLAUDE.md already exists (self-mod module). This change requires ~10 lines in `poll-loop.ts` and eliminates the most painful UX failure in long-running agents.
