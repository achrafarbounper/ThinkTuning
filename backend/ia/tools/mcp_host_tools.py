"""Stable agent tools backed by the optional outbound MCP host."""

from __future__ import annotations

from typing import Any

_router = None


def _get_router():
    global _router
    if _router is None:
        from app.infrastructure.mcp.host.capability_router import CapabilityRouter
        from app.infrastructure.mcp.host.config import load_server_configs
        from app.infrastructure.mcp.host.connection_manager import ConnectionManager

        _router = CapabilityRouter(ConnectionManager(load_server_configs()))
    return _router


def _call(name: str, **arguments: Any) -> dict[str, Any]:
    return _get_router().call(name, arguments)


def git_branch(action: str = "list", name: str | None = None) -> dict[str, Any]:
    """Use the configured Git MCP backend for branch operations."""
    arguments = {"action": action}
    if name is not None:
        arguments["name"] = name
    return _call("git_branch", **arguments)


def git_commit(message: str, files: list[str] | None = None) -> dict[str, Any]:
    """Create a commit through the configured Git MCP backend."""
    return _call("git_commit", message=message, files=files or [])


def github_list_issues(
    owner: str, repo: str, state: str = "open", limit: int = 20
) -> dict[str, Any]:
    """List issues through the configured GitHub MCP backend."""
    return _call("github_list_issues", owner=owner, repo=repo, state=state, limit=limit)


def github_get_pr(owner: str, repo: str, number: int) -> dict[str, Any]:
    """Get one pull request through the configured GitHub MCP backend."""
    return _call("github_get_pr", owner=owner, repo=repo, number=number)


def github_list_prs(
    owner: str, repo: str, state: str = "open", limit: int = 20
) -> dict[str, Any]:
    """List pull requests through the configured GitHub MCP backend."""
    return _call("github_list_prs", owner=owner, repo=repo, state=state, limit=limit)


def github_get_workflow_run(owner: str, repo: str, run_id: int) -> dict[str, Any]:
    """Get one GitHub Actions run through the configured MCP backend."""
    return _call("github_get_workflow_run", owner=owner, repo=repo, run_id=run_id)
