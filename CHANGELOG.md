# CHANGELOG — agentscope

## 1.9.0 (2026-10-23)

Ninth harness: `--harness roo` (Roo Code — VS Code/Desktop/CLI agent),
instructions mode, with a new `--roo-mode <slug>` flag. Rules verified
2026-10-23 vs the official custom-modes docs
(roocodeinc.github.io/Roo-Code/features/custom-modes/). Resolution:
shared rules `~/.roo/rules/` + `{workspace}/.roo/rules/` (global scope
loads first; recursive, alphabetical by filename case-insensitive,
`.md`/`.txt`, `.DS_Store`/`.swp` excluded; legacy `.roorules` fallback
when the workspace dir is missing or empty). Mode rules for the named
slug: `~/.roo/rules-<slug>/` + `{workspace}/.roo/rules-<slug}/`
(preferred), `.roorules-<slug>` then `.clinerules-<slug>` single-file
fallbacks — a non-empty mode dir shadows them. Slugs outside the
documented `/^[a-zA-Z0-9-]+$/` pattern are flagged. DIR is the workspace
root (no upward walk — undocumented, `[inferred]`). The active mode is a
runtime choice (VS Code mode switcher / `switch_mode`), so without
`--roo-mode` only shared rules resolve and the gap is flagged, never
guessed; `--roo-mode` with a non-roo harness is a usage error (exit 2).
Mode tool permissions and `roleDefinition`/`customInstructions` live in
`custom_modes.yaml`/`.roomodes` configs, not rule files — not modeled.
`--mcp`/`--hooks`/`--permissions`/`--skills` resolve honestly empty with
notes (no documented per-directory static mechanism; `.roo/mcp.json`
noted as a future candidate). 14 new hermetic tests (154 total); one
stale `1.8.0` version assertion in existing tests updated (tool correct).
Dogfood: doccover 12/12 + clicheck 15/15 clean.

Eighth harness: `--harness cline` (Cline — VS Code/Desktop/CLI agent),
instructions mode. Rules verified 2026-10-23 vs the official docs
(docs.cline.bot/customization/cline-rules, page updated 2026-10-07).
Resolution: global `~/Documents/Cline/Rules/*.md` + `~/.agents/AGENTS.md`
(baseline, loaded first), then workspace at the project root —
`.clinerules/` **and** `.cline/rules/` (both searched, combined;
`.md`/`.txt` processed), auto-detected `.cursorrules`, `.windsurfrules`,
`AGENTS.md` (cross-tool compat). Combined merge, workspace wins on
conflict; the relative order of the workspace formats is undocumented
(`[inferred]`). Conditional rules: the documented YAML frontmatter
`paths` conditional is parsed (single-line and multi-line list forms)
and reported structurally — activation depends on live editor context
and is never predicted; `paths: []` reported as never-activates.
Rule toggles are UI state (Rules panel), not files — assumed enabled,
always noted. Honest notes: `.clinerules/` works only in the primary
workspace folder (multi-root); project root = nearest ancestor of DIR
holding rule markers (`[inferred]`, walk stops at `$HOME` so a stray
`~/AGENTS.md` is never workspace scope); Windows global dir is
`Documents\Cline\Rules`. `--mcp`/`--hooks`/`--permissions`/`--skills`
resolve honestly empty with notes (no documented per-directory static
mechanism). One real bug caught during the build by smoke test: the
frontmatter `paths` regex used `\s*` around the value, which swallowed
the newline and shredded multi-line lists — now `[ \t]*`. 14 new
hermetic tests (140 total); two stale `1.7.0` version assertions in
existing tests updated (tool correct both times). Dogfood: doccover +
clicheck clean.

## 1.7.0 (2026-10-23)

Fifth mode: `--skills` — "which skills will harness X actually SEE for
directory Y?" (discoverable Agent Skills per harness per directory).
Resolves every `<name>/SKILL.md` in each harness's documented skill
locations (personal + project, with per-harness extras: Claude plugin
skills namespaced `plugin:skill`, Cursor's `.claude/skills/` +
`.codex/skills/` compatibility dirs, Codex's upward `.agents/skills/`
walk, OpenCode's six locations × v1/v2, Antigravity's workspace +
dual global candidates), with verdicts VALID / INVALID (name≠folder,
bad frontmatter, missing description — reported, never silently
skipped) / DUPLICATE-NAME (per-harness precedence winner named:
claude enterprise>personal>project, antigravity workspace>global).
Heuristic SKILL.md frontmatter parser (name/description only); skills'
`scripts/` never read or executed; remote catalogs (opencode
`skills.urls`) listed, never fetched; `--full` ignored.
**Discovery, not loading:** the mode answers the Oct 22 rejection's
objection structurally — harnesses load skill bodies on-demand by
description match, so the report is the visible set, never an
"effective loaded set". Mutually exclusive with --mcp/--hooks/
--permissions; exit 0 found / 1 none / 2 usage; --diff-harness on
(id, scope, path). 19 new hermetic tests (126 total). Design review:
DESIGN-skills.md.

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
