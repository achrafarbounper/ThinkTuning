# RFC 0002 — Optional outbound MCP Host

ThinkTuning may act as an MCP client using JSON-RPC 2.0, MCP `2025-06-18`,
and one-request-per-line stdio. The host is disabled by default
(`AGENT_MCP_HOST` must be explicitly enabled) and never invents `npx`/`uvx`
commands.

`backend/configs/mcp_host.yaml` is declarative: each backend supplies an
explicit command/args, environment values, an optional token environment
reference, timeout, and bounded restart count. Missing commands, tokens, or
configuration skip only that backend. Sessions are stopped on shutdown.

Only stable local names are exposed (`git_branch`, `git_commit`,
`github_list_issues`, `github_get_pr`, `github_list_prs`, and
`github_get_workflow_run`). Remote names are never exposed as `mcp_*`.
Every outbound call is policy checked before transport and audited with
redacted arguments. Mutations use the existing approval flow and are never
auto-approved.
