"""
Part 2 — Build a Coding Agent from Scratch
============================================
No frameworks. No MCP. No magic.

You will implement a ReAct-style agent loop that uses an LLM (via HP Azure OpenAI
or Ollama fallback) to accomplish coding tasks by calling hardcoded Python functions
as tools.

The architecture in three lines:
    Model provider = the brain  (HP Azure OpenAI / Ollama, text-in / text-out)
    tools          = the hands  (plain Python functions the agent can call)
    YOU            = the loop   (observe → think → act → observe → …)

Your job is to fill in every section marked  # TODO.

Run with:
    python part2/agent.py
"""

import json
import os
import sys

# ---------------------------------------------------------------------------
# Model provider — HP Azure OpenAI with Ollama fallback
# ---------------------------------------------------------------------------
_assignment2_path = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', 'assignment2'))
if os.path.isdir(_assignment2_path):
    sys.path.insert(0, _assignment2_path)

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

MAX_STEPS = 15  # hard stop — a confused model must not loop forever

SYSTEM_PROMPT = (
    "You are a programming assistant working in an isolated code workspace. "
    "Use the provided tools to inspect code files, fix bugs, write new code, "
    "and execute scripts. When a tool returns an 'error' key, adapt and try again. "
    "When the task is accomplished, give a concise summary and stop calling tools."
)

# ---------------------------------------------------------------------------
# The tools: plain Python functions. No server. No protocol. Just functions.
# ---------------------------------------------------------------------------
# This is a small sandbox filesystem the agent can read and write.
_FILES: dict[str, str] = {
    "hello.py": 'print("hello world")\n',
    "buggy.py": (
        "def add(a, b):\n"
        "    return a - b  # bug: should be a + b\n"
        "\n"
        "print(add(2, 3))\n"
    ),
}
_SHELL_LOG: list[str] = []


def list_files() -> list[str]:
    """List all files available in the sandbox."""
    return list(_FILES.keys())


def read_file(path: str) -> dict:
    """Read the contents of a file. Returns {path, content} or {error}."""
    if path not in _FILES:
        return {"error": f"file not found: {path!r}"}
    return {"path": path, "content": _FILES[path]}


def write_file(path: str, content: str) -> dict:
    """Write (create or overwrite) a file with the given content. Returns {path, written}."""
    _FILES[path] = content
    return {"path": path, "written": True}


def run_python(path: str) -> dict:
    """
    Execute a Python file from the sandbox and capture its output.
    Returns {path, stdout} or {path, error}.
    This is a toy interpreter — it runs exec() on the file contents.
    """
    if path not in _FILES:
        return {"error": f"file not found: {path!r}"}
    source = _FILES[path]
    import io, sys as _sys
    old_stdout = _sys.stdout
    _sys.stdout = io.StringIO()
    try:
        exec(compile(source, path, "exec"), {})  # noqa: S102
        captured = _sys.stdout.getvalue()
    except Exception as e:
        _sys.stdout = old_stdout
        result = {"path": path, "error": str(e)}
        _SHELL_LOG.append(f"run_python({path!r}) -> error: {e}")
        return result
    finally:
        _sys.stdout = old_stdout
    _SHELL_LOG.append(f"run_python({path!r}) -> {captured!r}")
    return {"path": path, "stdout": captured}


def search_files(pattern: str) -> dict:
    """
    Search for `pattern` (case-insensitive substring) in the contents of every
    sandbox file.  Returns {"matches": [{"file": ..., "line": ..., "text": ...}, ...]}.
    """
    matches = []
    needle = pattern.lower()
    for filename, content in _FILES.items():
        for lineno, line in enumerate(content.splitlines(), 1):
            if needle in line.lower():
                matches.append({"file": filename, "line": lineno, "text": line.rstrip()})
    return {"matches": matches}


# ---------------------------------------------------------------------------
# Tool registry — the "billboard" the model reads to know what it can do.
# Each entry follows the JSON Schema shape the chat completions API expects.
# ---------------------------------------------------------------------------
TOOLS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "List all files available in the sandbox.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read the contents of a file. Returns {path, content} or {error}.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Filename to read, e.g. 'hello.py'"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Write (create or overwrite) a file with the given content.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Filename to write"},
                    "content": {"type": "string", "description": "Full file content"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_python",
            "description": "Execute a Python file and return its stdout output.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Filename to run, e.g. 'hello.py'"},
                },
                "required": ["path"],
            },
        },
    },
    # TODO 3 — search_files added below
    {
        "type": "function",
        "function": {
            "name": "search_files",
            "description": (
                "Search for a pattern (case-insensitive substring) across all sandbox files. "
                "Returns matching lines with file name, line number, and text."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": "Substring to search for in all file contents",
                    },
                },
                "required": ["pattern"],
            },
        },
    },
]

# ---------------------------------------------------------------------------
# Tool dispatch — maps a tool name to its Python function.
# ---------------------------------------------------------------------------
TOOL_FN: dict = {
    "list_files": list_files,
    "read_file": read_file,
    "write_file": write_file,
    "run_python": run_python,
    "search_files": search_files,   # TODO 3 (cont.) — added
}


# ---------------------------------------------------------------------------
# Tracing — so you can SEE what the agent does at each step.
# ---------------------------------------------------------------------------
_TRACE: list[dict] = []


def trace(step: str, payload) -> None:
    print(f"  [{step}] {json.dumps(payload, default=str)[:400]}")
    _TRACE.append({"step": step, "payload": payload})


def save_trace(task_num: int, goal: str, answer: str) -> str:
    """
    Write the trajectory recorded in _TRACE for one task to
    part2/traces/task_<N>.json, as a flat list of OpenAI-format chat messages.
    """
    messages: list[dict] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": goal},
    ]
    for entry in _TRACE:
        if entry["step"] == "action":
            call = entry["payload"]  # {"tool": name, "args": {...}}
            messages.append(
                {
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
                }
            )
        elif entry["step"] == "observation":
            messages.append(
                {"role": "tool", "content": json.dumps(entry["payload"], default=str)}
            )
    # the model's final natural-language answer (no tool call)
    messages.append({"role": "assistant", "content": answer})

    traces_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "traces")
    os.makedirs(traces_dir, exist_ok=True)
    path = os.path.join(traces_dir, f"task_{task_num}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(messages, f, indent=2, default=str)
    return path


# ---------------------------------------------------------------------------
# TODO 1 — dispatch_tool
# ---------------------------------------------------------------------------
def dispatch_tool(name: str, args: dict):
    """
    Call the Python function named `name` with keyword arguments `args`.
    Return its result, or {"error": "..."} if the tool doesn't exist.
    """
    if name not in TOOL_FN:
        return {"error": f"unknown tool: {name!r}. Available: {list(TOOL_FN.keys())}"}
    try:
        return TOOL_FN[name](**args)
    except Exception as e:
        return {"error": f"tool {name!r} raised {type(e).__name__}: {e}"}


# ---------------------------------------------------------------------------
# TODO 2 — run_agent  (the loop)
# ---------------------------------------------------------------------------
def run_agent(goal: str) -> str:
    """
    Drive one task to completion using the ReAct loop:

        for each step:
            1. call the chat completions API with the current messages and TOOLS
            2. append the model's reply to messages
            3. if the reply has NO tool_calls → the model is done, return its content
            4. for each tool_call in the reply:
               a. extract name and arguments
               b. trace the action
               c. call dispatch_tool(name, args)
               d. trace the observation
               e. append a tool-role message with the result (as JSON)
        if MAX_STEPS reached → return a "(stopped)" string
    """
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": goal},
    ]

    for step in range(1, MAX_STEPS + 1):
        # 1. Think — call the model
        try:
            resp = _client.chat.completions.create(
                model=MODEL,
                messages=messages,
                tools=TOOLS,
            )
        except Exception as api_err:
            err_str = str(api_err)
            if "content_filter" in err_str or "content management policy" in err_str:
                return (
                    f"(content filter triggered on step {step} — "
                    f"the HP Azure content policy blocked this request. "
                    f"This is a known limitation when the accumulated conversation "
                    f"contains code execution patterns. Error: {err_str[:200]})"
                )
            raise
        msg = resp.choices[0].message

        # 2. Record — append the reply
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

        # 3. Done? — no tool calls means final answer
        if not msg.tool_calls:
            return msg.content or "(agent returned empty response)"

        # 4. Act — execute each tool call and append result
        for tc in msg.tool_calls:
            name = tc.function.name
            try:
                args = json.loads(tc.function.arguments)
            except json.JSONDecodeError:
                args = {}
            trace("action", {"tool": name, "args": args})
            result = dispatch_tool(name, args)
            trace("observation", result)
            messages.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": json.dumps(result, default=str),
            })

    return "(stopped: hit MAX_STEPS without a final answer)"


# ---------------------------------------------------------------------------
# Tasks — run these to verify your implementation
# ---------------------------------------------------------------------------
TASKS = [
    "List all files, read hello.py, run it, and tell me what it prints.",
    "Read buggy.py, identify the bug, fix it, run the fixed version, and confirm the output.",
    "Write a new file called reverse.py that defines reverse(s) and prints reverse('hello'), then run it.",
    "Search all files for the word 'TODO'. For every match, read that file and tell me its full contents.",
]

# The pristine sandbox each task starts from (the grader uses the same seed).
PRISTINE_FILES = {
    "hello.py": 'print("hello world")\n',
    "buggy.py": (
        "def add(a, b):\n"
        "    return a - b  # bug: should be a + b\n"
        "\n"
        "print(add(2, 3))\n"
    ),
    "notes.py": (
        "# TODO: add input validation\n"
        "# TODO: write unit tests\n"
        "\n"
        "def greet(name):\n"
        "    return f'Hello, {name}!'\n"
    ),
}


def reset_sandbox() -> None:
    """Restore the sandbox to its pristine seed and clear the trace buffer."""
    _FILES.clear()
    _FILES.update(PRISTINE_FILES)
    _TRACE.clear()


def run_task(task_num: int, goal: str) -> None:
    """Run one task against a fresh sandbox and save its trace."""
    reset_sandbox()
    print(f"\n{'='*60}\nTASK {task_num}: {goal}\n{'='*60}")
    answer = run_agent(goal)
    print(f"\n--- ANSWER: {answer}")
    print(f"--- trace saved to {save_trace(task_num, goal, answer)}")


if __name__ == "__main__":
    for i, goal in enumerate(TASKS, 1):
        run_task(i, goal)
