# CHANGELOG — agentscope

## 1.6.0 (2026-10-23)

Seventh harness: `--harness antigravity` (Google's agent IDE/CLI/App),
all four modes, rules verified 2026-10-23 against the official docs
(antigravity.google/docs/rules/, /hooks/, /permissions/):

- **instructions**: cumulative (not replacement-based) resolution —
  directory-scoped rules nearest-first (`AGENTS.md`/`GEMINI.md`,
  `.agents/AGENTS.md`/`GEMINI.md`, `.agents/rules/*.md` with frontmatter
  `trigger: always_on|model_decision|glob|manual`, legacy `.agent/`
  honored), `.agents/rules.json` inherits/entries expansion (relative
  paths resolved against the `.agents` dir [inferred]), then global
  `~/.gemini/AGENTS.md`, `~/.gemini/GEMINI.md`,
  `~/.gemini/config/AGENTS.md`/`GEMINI.md`,
  `~/.gemini/config/rules/*.md`. 24 KB per-file truncation warning;
  `@[label](path)` includes listed (inlined by the harness, not expanded
  here); workspace-root vs filesystem-root over-reporting noted honestly.
- **--hooks**: `.agents/hooks.json` (workspace, nearest-upward search
  [inferred]) + `~/.gemini/config/hooks.json` (global) [documented];
  6 events (SessionStart, PreToolUse, PostToolUse, PreInvocation,
  PostInvocation, Stop); matcher regexes over tool names (ignored for
  non-tool events); `enabled: false` honored; commands shown verbatim,
  never executed (dedicated never-executes test). Pairs with
  hooktest v1.5.0 `--harness antigravity`.
- **--mcp**: `~/.gemini/config/mcp_config.json` [documented] plus
  community-reported `.agents/mcp_config.json` [inferred]; same
  structural secret safety (env/header names only, URLs masked, remotes
  never probed).
- **--permissions**: honestly empty with documented notes — permission
  rules (Deny/Ask/Allow over `action(target)` resources + presets) are
  UI-managed (Settings); no static config format exists. PreToolUse
  hooks as the programmatic enforcement path, cross-linked to hooktest.
- 107 tests (16 new, incl. rules.json expansion, 24 KB warning,
  enabled:false skip, never-executes, MCP secret safety, honest-empty
  permissions, --diff-harness inclusion).

## 1.5.0 (2026-10-22)

Copilot `--hooks` resolution upgraded against the official GitHub Copilot
hooks reference (docs.github.com/en/copilot/reference/hooks-reference,
verified 2026-10-22 — the same reference backing hooktest's new
`--harness copilot` corpus):

- Inline `hooks` blocks in settings files are now enumerated: repo
  `.github/copilot/settings.json` + `settings.local.json` (scope `repo`),
  cross-tool-read `.claude/settings.json` + `settings.local.json`
  (scope `repo-inline`, all `[documented]`), and user
  `~/.copilot/settings.json` (scope `user-inline`). These are hooks the
  previous version silently missed.
- Entry parsing now handles the documented `exec` + `args` form (direct
  exec, no shell — recorded verbatim with a note), the `timeout` alias
  (`timeoutSec` wins when both are present), and non-`command` hook types
  (e.g. `http` hooks are skipped with a warning: agentscope resolves
  shell hooks only).
- `disableAllHooks` is honored: in a hooks-dir file it skips that file's
  hooks (note); at settings.json level it produces the documented
  stronger note — the CLI skips every hook from every source for sessions
  here (policy hooks excepted).
- `COPILOT_HOME` is honored: user hooks load from `$COPILOT_HOME/hooks`
  when set, else `~/.copilot/hooks` `[documented]`.
- Merge note now records the documented source order (policy > user >
  repo > plugin), the policy paths (`/etc/github-copilot/policy.d/*.json`
  on Linux/macOS, `HKLM\Software\Policies\GitHub\Copilot` on Windows —
  machine-wide, not enumerable from here), and the plugin/agent-scoped
  sources that stay unmodeled.
- Also fixed a README gap: the hooks-source table still claimed Gemini
  CLI had no documented hook mechanism (corrected in the tool in v1.4.0,
  but the README row was left behind) — now reads
  documented-but-not-modeled, pointing at `hooktest --harness gemini`.
- 90+ tests passing (5 new copilot `--hooks` tests).

Sixth harness: `--harness opencode` (instructions mode). Resolves the
effective instruction set for OpenCode from vendor docs verified
2026-10-22 (opencode.ai/docs/rules/, the page itself updated the day
before the check). Genuinely different semantics from the other five
harnesses: **first-match-wins per directory** (AGENTS.md shadows
CLAUDE.md; not concatenation); global `~/.config/opencode/AGENTS.md`;
`~/.claude/CLAUDE.md` Claude Code compat fallback gated by the
documented `OPENCODE_DISABLE_CLAUDE_CODE*` env vars (checked for real);
project/global `opencode.json` `instructions` entries — paths, globs,
and remote URLs, with remotes listed-but-never-fetched (read-only) and
missing/glob-miss entries warned; the nearest `opencode.json` upward is
used (the upward search is [inferred]); no `@file` import expansion
(the harness does not parse file references — [documented]). The
read-time "nearer instruction file attached as a system reminder"
behavior is dynamic per-read and reported as a note, never claimed as
static output. `--mcp` covers opencode too (v2 `mcp.servers` layout
verified 2026-10-22 vs opencode.ai/v2/docs/mcp-servers: local/remote
types, command/cwd/environment/disabled, headers, OAuth-on-by-default
note, project-config-replaces-global override semantics [all
documented]; pre-v2 `mcp.<name>` layout warned as unmodeled; env/header
values never printed, URLs masked, remote servers never probed). And
`--permissions` models opencode's documented `permissions` array
(action/resource/effect — evaluation order across the array is
undocumented, so no merge order is assumed, rules verbatim). `--hooks`
for opencode resolves empty with a note. 91 tests (17 opencode: 9
instructions + 6 mcp + 2 permissions, plus the rewritten gemini-hooks
test).

Also fixed in this release: the `--hooks` note for Gemini CLI claimed
(no evidence) that no hook mechanism existed; the official Gemini CLI
hooks reference (geminicli.com/docs/hooks/, 11 events) is real, so the
note now says documented-but-not-modeled and points at
`hooktest --harness gemini` (v1.3.0, built the same day). The stale-model
claim existed since v1.2.0 — a reminder to re-verify, not just label.

## 1.3.0 (2026-10-22)

`--permissions` mode: effective allow/deny rule resolution per harness
for a directory — the fourth mode on agentscope's effective-resolution
axis (instructions, MCP, hooks, now permissions). Answers "which
allow/deny rules does harness X actually enforce for directory Y?"
from config files alone: merged allow/deny/ask/filesystem rules with
scope, source file, and [documented]/[inferred] tags, plus mode-level
context (`sandbox_mode`, `approval_policy`, active permission profile,
Gemini's `tools.sandbox`). Per-harness layouts verified via web search
2026-10-22: Claude's three settings.json scopes (rules emitted in the
documented deny → ask → allow evaluation order), Codex's global+project
config.toml with named `[permissions.<name>.filesystem]` profiles and
the `:project_roots` token shape, Gemini's global+project
settings.json `tools.core`/`tools.sandbox` (plus the older `coreTools`
shape); Cursor and Copilot have no documented static allow/deny format
and resolve empty with a note. Read-only by construction: rules shown
verbatim, never executed or evaluated against real tool calls (a
planted `Bash(touch <marker>)` deny rule asserts the marker is never
created). `--diff-harness` works in `--permissions` mode; `--mcp`,
`--hooks`, `--permissions` mutually exclusive; exit 0 rules-or-context
/ 1 none / 2 usage. 73 tests (13 new), all green. Dogfooded with
doccover (flagged the missing `--permissions` README docs, now fixed;
2 stale refs are prose-example false positives) and clicheck (15/15).

## 1.2.0 (2026-10-22)

`--hooks` mode: effective hook resolution per harness for a directory —
event, matcher, config scope, source file, hook command, timeout.
Hook-config layouts verified via web search 2026-10-22 (Claude's three
settings files; Codex `hooks.json`/`[hooks]` with trusted-project gate;
Cursor's own files plus Claude's settings with tool-name mapping;
Copilot's `.github/hooks/*.json` + `~/.copilot/hooks/*.json`; Gemini has
no documented hook mechanism). The execution hazard is structural:
`--hooks` is pure config parsing — hook commands are shown verbatim but
never executed (covered by a dedicated test asserting a planted
`touch`-marker command never runs); commands may embed credentials, so
the output is treated like the config file itself (README Limitations).
`--diff-harness` works in `--hooks` mode (caught and fixed a real tuple-
formatting bug in the diff path during the build). 60 tests (15 new),
all green. Dogfooded with doccover (flagged the missing README docs,
now fixed) and clicheck (15/15 with correct option placement).

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
