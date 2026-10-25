"""Row-Bot's tools, listed to Claude Code as an MCP server whose calls do nothing.

Claude Code starts this for one model call (``python -I -S -B claude_plan_tools.py <tools.json>``). It
answers ``initialize`` and ``tools/list`` with the tools in the file and refuses every ``tools/call``:
Row-Bot reads the model's tool calls from Claude Code's output and runs them in its own agent loop,
through its own approval gate. Standard library only; it reads nothing but its tools file.
"""

from __future__ import annotations

import json
import sys

REFUSAL = "Row-Bot runs its tools itself, after its own approval check. Nothing ran here."


def answer(message: object, tools: list) -> dict | None:
    """The JSON-RPC reply to one message; None for a notification, which gets none."""
    if not isinstance(message, dict) or "id" not in message:
        return None
    method = message.get("method")
    params = message.get("params") if isinstance(message.get("params"), dict) else {}
    if method == "initialize":
        result: dict = {"protocolVersion": str(params.get("protocolVersion") or "2024-11-05"),
                        "capabilities": {"tools": {}}, "serverInfo": {"name": "row-bot", "version": "1"}}
    elif method == "tools/list":
        result = {"tools": tools}
    elif method == "tools/call":
        result = {"isError": True, "content": [{"type": "text", "text": REFUSAL}]}
    elif method == "ping":
        result = {}
    else:
        return {"jsonrpc": "2.0", "id": message["id"], "error": {"code": -32601, "message": "Method not found"}}
    return {"jsonrpc": "2.0", "id": message["id"], "result": result}


def main(argv: list[str]) -> int:
    with open(argv[1], encoding="utf-8") as handle:
        tools = json.load(handle)
    for raw in sys.stdin.buffer:
        try:
            message = json.loads(raw.decode("utf-8"))
        except ValueError:
            continue
        reply = answer(message, tools)
        if reply is not None:
            sys.stdout.write(json.dumps(reply) + "\n")
            sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
