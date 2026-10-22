# CHANGELOG — agentscope

## 1.1.0 (2026-10-22)

`--mcp` mode: effective MCP server resolution per harness for a
directory — name, config scope (project/user/local/global), source
file, transport (stdio with command+args; http for remote, never
probed), env/header names only. Config layouts per harness verified
against the Microsoft APM per-harness table 2026-10-22 (Claude
`.mcp.json` + `~/.claude.json` mcpServers/projects[]; Codex
`config.toml` `[mcp_servers.*]` global+project; Cursor
`.cursor/mcp.json` pair; Copilot `.github/mcp.json` +
`mcp-config.json`; Gemini `settings.json` `mcpServers` pair).
Secret safety is structural: env/header values never printed, URL
query strings/fragments stripped, `--full` refused in `--mcp` mode,
remote servers listed but never probed. Same-name servers in multiple
scopes surface as warnings (merge order undocumented). `--diff-harness`
works in both modes. 45 tests (18 new), all green. Requires Python 3.11+
now (tomllib); README gained an `--mcp` section.

## 1.0.0 (2026-10-22)

Initial release.

- Five harness resolvers (codex, claude, cursor, copilot, gemini) built
  from public docs, verified 2026-10-22: root->cwd concat with 32 KiB
  silent-truncation warning (Codex), upward merge + AGENTS.md fallback +
  CLAUDE.local.md (Claude), frontmatter glob scoping (Cursor), nearest
  AGENTS.md + path-scoped instructions (Copilot), configurable
  context.fileName (Gemini).
- `@path`/`@file` import inlining with recursion caps (Claude 4 hops per
  docs; Gemini 5 inferred), cycle detection, unreadable-target warnings;
  email-shaped tokens never resolved.
- Honesty model: every rule tagged [documented]/[inferred], bannered as a
  model of documented behavior (not runtime truth); truncation reported as
  estimated cutoff only; scope matches labeled heuristic; structure, never
  semantic conflict judgments.
- CLI: `--harness` (repeatable), `--outline`, `--full`, `--json`,
  `--diff-harness A B`, `--list-harnesses`, `--version`; exit codes
  0 loaded / 1 nothing-loads / 2 usage. Strictly read-only.
- 27 tests, including hermetic-HOME fixtures, real-bug catches (import
  recursion KeyError; TestCase.run name collision), and a read-only
  assertion.
