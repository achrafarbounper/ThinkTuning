"""Non-destructive smoke test for the deployed MCP SSE endpoint."""

from __future__ import annotations

import argparse
import json
import sys
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def _call(url: str, api_key: str, request_id: int, method: str) -> dict:
    payload = json.dumps(
        {"jsonrpc": "2.0", "id": request_id, "method": method, "params": {}}
    ).encode("utf-8")
    request = Request(
        url,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
            "X-API-Key": api_key,
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=30) as response:
            body = response.read().decode("utf-8")
    except (HTTPError, URLError, TimeoutError) as exc:
        raise RuntimeError(f"MCP smoke request failed: {exc}") from exc
    for line in body.splitlines():
        if line.startswith("data: ") and line != "data: [DONE]":
            return json.loads(line[6:])
    raise RuntimeError("MCP response did not contain a JSON-RPC data event")


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a read-only MCP production smoke test.")
    parser.add_argument("--url", required=True, help="MCP SSE endpoint, e.g. https://host/mcp/sse")
    parser.add_argument("--api-key", required=True)
    args = parser.parse_args()

    initialized = _call(args.url, args.api_key, 1, "initialize")
    if "error" in initialized:
        raise RuntimeError(f"initialize returned an error: {initialized['error']}")
    capabilities = initialized.get("result", {}).get("capabilities", {})
    tools = _call(args.url, args.api_key, 2, "tools/list")
    if "error" in tools:
        raise RuntimeError(f"tools/list returned an error: {tools['error']}")
    tool_count = len(tools.get("result", {}).get("tools", []))
    print(
        json.dumps(
            {
                "status": "ok",
                "sampling": "sampling" in capabilities,
                "tool_count": tool_count,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as exc:
        print(f"smoke failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
