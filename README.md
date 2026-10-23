# agentscope — effective-instruction, effective-MCP, effective-hook, and effective-permissions resolver

"What instructions does harness X actually load for directory Y?" —
and, since v1.1.0, "which **MCP servers** does it actually load?" —
and, since v1.2.0, "which **hooks** does it actually fire?"

Every coding-agent harness discovers instruction files with different
semantics. Codex concatenates root-to-cwd with a silent 32 KiB cap. Claude
Code merges upward with `@path` imports. Cursor scopes `.cursor/rules` by
glob frontmatter. Copilot takes the nearest `AGENTS.md`. Gemini CLI lets you
rename the context file in settings. When "the agent ignored my rule", the
answer is usually in the *loading rules*, not the rule text — and today it is
answered only by prose guides.

`agentscope` resolves the effective loaded set per harness for a directory,
from the files on disk: ordered contributors with byte counts, merge order,
import inlining, cap warnings, and glob-scope matches.

```bash
agentscope                             # all harnesses, current dir
agentscope --harness codex apps/web
agentscope --harness claude --harness gemini .
agentscope --outline                    # structure only
agentscope --full                       # also print the merged content
agentscope --json
agentscope --diff-harness codex claude apps/web
agentscope --list-harnesses               # list supported harnesses
agentscope --version
```

Exit codes: **0** = at least one contributing file resolved; **1** = the
selected harness(es) would load nothing (itself a useful answer);
**2** = usage error.

## `--mcp`: effective MCP server resolution (v1.1.0)

```bash
agentscope --mcp                          # all harnesses, current dir
agentscope --mcp --harness claude .
agentscope --mcp --json
agentscope --mcp --outline
agentscope --mcp --diff-harness claude codex .
agentscope --hooks                          # effective hook sets
agentscope --hooks --harness claude .
agentscope --hooks --diff-harness claude cursor .
```

Reports the effective MCP server set per harness for the directory —
name, config scope (project/user/local/global), source file, transport
(`stdio` with command+args, or `http` for remote servers — listed as
REMOTE, never probed), and env/header **names** only.

**Secret safety is structural, not documentary:**

- env var and header *values* are never printed — only `NAME=<set>`
- URL query strings and fragments are stripped (they may carry tokens)
- `--full` is refused in `--mcp` mode (exit 2)
- the tool never connects to any server it lists

Per-harness config locations (verified against the Microsoft APM
per-harness table, 2026-10-22):

| Harness | Project scope | User/global scope |
|---|---|---|
| `claude` | `./.mcp.json` (project-scoped servers prompt for interactive approval) | `~/.claude.json` `mcpServers` (user); per-project `projects[]` keys (local) |
| `codex` | `./.codex/config.toml` (only when `.codex/` exists) | `~/.codex/config.toml`, `[mcp_servers.*]` |
| `cursor` | `<git-root>/.cursor/mcp.json` | `~/.cursor/mcp.json` |
| `copilot` | `./.github/mcp.json` (nearest up the tree, inferred) | `$COPILOT_HOME/mcp-config.json` else `~/.copilot/mcp-config.json` |
| `gemini` | `./.gemini/settings.json` `mcpServers` | `~/.gemini/settings.json` `mcpServers` |

Same-name servers defined in multiple scopes are surfaced as warnings
(the merge order is not documented per scope); rules are tagged
`[documented]`/`[inferred]` as in instruction mode.

## `--hooks`: effective hook resolution (v1.2.0)

"Which hooks does harness X actually fire for directory Y?" — the same
effective-resolution question, for the lifecycle hooks that silently
shape agent behavior (guards, formatters, context injectors, audit
loggers). Today it is answered only by running the harness (`/hooks`,
`claude --debug`) or reading prose guides.

```bash
agentscope --hooks                          # all harnesses, current dir
agentscope --hooks --harness claude .
agentscope --hooks --outline                # events + matchers only
agentscope --hooks --json
agentscope --hooks --diff-harness claude cursor .
```

Reports, per harness: event, matcher, config scope (user/project/local/
global/repo), source file, the hook command, and timeout where set.
`--mcp` and `--hooks` are mutually exclusive; `--full` is accepted but
has no additional effect in `--hooks` mode (the text output already
shows complete hook definitions).

**The execution hazard is handled structurally, not documentarily:**
hooks are arbitrary shell commands, so `agentscope` **never executes
anything** in `--hooks` mode — it only parses config files. Hook
command text is shown verbatim for the operator to inspect; it is never
passed to a shell, a subprocess, or any other execution path. (There is
a test that plants a `touch`-the-marker hook command and asserts the
marker is never created.)

Per-harness hook sources (verified via web search, 2026-10-22):

| Harness | Sources |
|---|---|
| `claude` | `~/.claude/settings.json` (user), `./.claude/settings.json` (project), `./.claude/settings.local.json` (personal); matchers are regexes on tool names; all scopes fire; hooks load at session start |
| `codex` | `~/.codex/hooks.json` (global), `./.codex/hooks.json` or `[hooks]` in `./.codex/config.toml` (project, trusted projects only); borrows Claude's config shape; blocking = exit 2 or `permissionDecision` deny |
| `cursor` | `~/.cursor/hooks.json` (global), `./.cursor/hooks.json` (project); also reads Claude's settings by default, with tool-name mapping (`Bash`→`Shell`, `Edit`→`Write`, `UserPromptSubmit`→`beforeSubmitPrompt`; `Notification`/`PermissionRequest` not mapped) |
| `copilot` | `./.github/hooks/*.json` (repo), inline `hooks` blocks in `./.github/copilot/settings.json` + `settings.local.json` (repo) and `./.claude/settings.json` + `settings.local.json` (cross-read, documented), `~/.copilot/settings.json` (user-inline), user hooks dir (`$COPILOT_HOME/hooks` when `COPILOT_HOME` is set, else `~/.copilot/hooks/*.json`); sources merged; `disableAllHooks` honored (per-file in hook files, everything in settings.json); policy (`/etc/github-copilot/policy.d/*.json`, machine-wide) and plugin/agent-scoped hooks are not enumerable from here |
| `gemini` | documented hook mechanism exists (geminicli.com/docs/hooks/) but is not modeled by agentscope `--hooks` — always empty, with a note pointing at `hooktest --harness gemini` |

Exit codes in `--hooks` mode: **0** = at least one hook resolves;
**1** = none resolve; **2** = usage error.

## `--permissions`: effective allow/deny rule resolution (v1.3.0)

"Which allow/deny/ask rules does harness X actually enforce for
directory Y?" — the same effective-resolution question, for the
permission rules that decide whether an agent's tool call flies,
prompts, or dies. Today it is answered only by prose guides or by
asking the harness itself; config validators (agnix) check hygiene and
policy engines enforce at runtime, but none statically resolves the
merged effective set.

```bash
agentscope --permissions                     # all harnesses, current dir
agentscope --permissions --harness claude .
agentscope --permissions --outline           # rule + context counts only
agentscope --permissions --json
agentscope --permissions --diff-harness claude codex .
```

Reports, per harness: the merged allow/deny/ask/filesystem rules (kind,
rule text, scope, source file), plus the mode-level context that
frames them (`sandbox_mode`, `approval_policy`, active permission
profile, Gemini's `tools.sandbox`). `--mcp`, `--hooks`, and
`--permissions` are mutually exclusive; `--full` is accepted but has no
additional effect in `--permissions` mode (the text output already
shows complete rules).

**Read-only by construction:** rule text is shown verbatim for the
operator to inspect — a `deny` rule may name a destructive command,
and it is never executed, never passed to a shell, and never evaluated
against real tool calls. (There is a test that plants a
`Bash(touch <marker>)` deny rule and asserts the marker is never
created.) Permission rules are your own config (paths and patterns),
so the output is no more sensitive than the config files themselves.

Per-harness permission sources (verified via web search, 2026-10-22):

| Harness | Sources |
|---|---|
| `claude` | `~/.claude/settings.json` (user), `./.claude/settings.json` (project), `./.claude/settings.local.json` (personal); `permissions.allow` / `.deny` / `.ask` lists; rules emit in evaluation order (deny → ask → allow — the first match decides, specificity and file origin don't matter) |
| `codex` | `~/.codex/config.toml` (global), `./.codex/config.toml` (project, trusted projects only); `default_permissions` selects the active named profile; `[permissions.<name>.filesystem]` path→`write`/`read`/`none` tables (incl. the `:project_roots` token shape); `sandbox_mode`, `approval_policy` (incl. granular tables) as context |
| `gemini` | `~/.gemini/settings.json` (global), `./.gemini/settings.json` (project); `tools.core` allowlist + `tools.sandbox` (also the older top-level `coreTools` shape); env vars outrank both but aren't visible to a static resolver |
| `cursor` | no documented static allow/deny rule format found — always empty, with a note |
| `copilot` | no documented static allow/deny rule format found — always empty, with a note |

Exit codes in `--permissions` mode: **0** = at least one rule or
context setting resolves; **1** = none resolve; **2** = usage error.

## Supported harnesses

| Harness | What agentscope resolves |
|---|---|
| `codex` | `~/.codex/AGENTS.override.md` else `AGENTS.md` (first non-empty); then root→cwd, one file per dir (`AGENTS.override.md` > `AGENTS.md` > `project_doc_fallback_filenames`); combined-size vs `project_doc_max_bytes` (default 32 KiB) with a silent-truncation warning |
| `claude` | upward `CLAUDE.md` (+ `.claude/CLAUDE.md`) merge, root first; `AGENTS.md` fallback when no `CLAUDE.md` exists upward; `CLAUDE.local.md`; `~/.claude/CLAUDE.md`; `~/.claude/rules/*.md` (structure only); `@path` imports inlined (recursion cap 4, cycle-safe) |
| `cursor` | `.cursor/rules/*.{mdc,md}` with frontmatter scope (`globs`/`alwaysApply`) matched against the directory (heuristic); legacy `.cursorrules`; nested `AGENTS.md` combined with parents |
| `copilot` | nearest `.github/copilot-instructions.md`; `.github/instructions/*.instructions.md` with `applyTo` matching (heuristic); nearest `AGENTS.md` wins; root `CLAUDE.md`/`GEMINI.md` alternatives |
| `gemini` | `context.fileName` from `.gemini/settings.json` else `~/.gemini/settings.json` (default `GEMINI.md`); global `~/.gemini/GEMINI.md`; upward walk concatenated general→specific; `@file` imports inlined |
| `opencode` | **first-match-wins per directory** (AGENTS.md shadows CLAUDE.md — not concatenated like the others); global `~/.config/opencode/AGENTS.md`; `~/.claude/CLAUDE.md` compat fallback (disabled by `OPENCODE_DISABLE_CLAUDE_CODE*`); `opencode.json` `instructions` entries (paths, globs, remote URLs — remotes listed, never fetched); **no** `@file` import expansion (the harness doesn't parse them). `--mcp` reads the v2 `mcp.servers` layout; `--permissions` reads the `permissions` array |
| `antigravity` (v1.6.0) | **cumulative** (not replacement-based): directory-scoped rules nearest-first — `<dir>/AGENTS.md`/`GEMINI.md`, `<dir>/.agents/AGENTS.md`/`GEMINI.md`, `<dir>/.agents/rules/*.md` (frontmatter `trigger`: `always_on`/`model_decision`/`glob`/`manual`; legacy `.agent/` honored), `.agents/rules.json` `inherits`/`entries` expansion — then global `~/.gemini/AGENTS.md`, `~/.gemini/GEMINI.md`, `~/.gemini/config/AGENTS.md`/`GEMINI.md`, `~/.gemini/config/rules/*.md`. 24 KB per-file truncation warning; `@[label](path)` includes listed (inlined by the harness, not expanded here). `--hooks` reads `.agents/hooks.json` (workspace, nearest upward) + `~/.gemini/config/hooks.json` (global): 6 events, matcher regexes, `enabled: false` honored, commands shown verbatim and **never executed**. `--mcp` reads `~/.gemini/config/mcp_config.json` (+ community-reported `.agents/mcp_config.json`). `--permissions` is honestly empty — permission rules are UI-managed (Settings), with the Deny/Ask/Allow model and presets documented in notes. Rules verified 2026-10-23 vs the official docs |

## The honesty model

**agentscope is a model of documented harness behavior, not runtime
truth.** Every rule applied is tagged `[documented]` or `[inferred]`, and
the banner carries the date the rules were last verified against public
docs (2026-10-22). Harness behavior drifts with releases; when a debug
session hinges on it, confirm against the harness's own docs.

- Truncation is reported as total-bytes vs cap with an **estimated**
  cutoff — never a claimed exact drop line.
- Glob/`applyTo` scope matches are labeled **heuristic**.
- Structure only, never semantic conflict judgments: agentscope reports
  *which files load in which order*, not whether they agree.

## Limitations

- Enterprise/managed-policy layers and user settings that live in
  editor config (Cursor user rules, Copilot `chat.useClaudeMdFile`) are
  not resolvable from disk; they are listed as notes, not files.
- Codex's "untrusted project" behavior (skips project-level discovery)
  cannot be determined from the filesystem; flagged as a note.
- `@`-import recursion caps and filename fallbacks follow documented
  values where they exist, best-effort defaults where they don't.
- Symlink loops in the directory walk are not followed (`os.walk`
  default); unreadable files are skipped silently per contributor,
  warned per import.
- `--hooks` prints hook commands verbatim; a hook command may embed
  credentials (e.g. an `Authorization` header) — treat the output as
  you would the config file itself.
- `--hooks` does not enumerate plugin-managed hooks (Claude),
  policy hooks or plugin-declared hooks (Copilot); hook merge order
  across scopes is undocumented and reported as `[inferred]` where
  assumed.
- `--permissions` resolves the rule set on disk, not the harness's
  runtime decision: CLI flags (Codex), session-local rules (Claude
  `/permissions` changes), and environment-variable overrides (Gemini)
  are invisible to a static resolver and flagged as notes. Cursor and
  Copilot have no documented static allow/deny format and resolve empty.
  Rule text is shown verbatim; treat the output as you would the config
  file itself.

## Testing

```bash
python3 -m unittest test_agentscope -v   # 91 tests
```

Stdlib only (Python 3.11+, for `tomllib`). No dependencies. Strictly
read-only — the suite asserts the tool never writes to the resolved
directory.

## License

TBD (maintainer approval pending).

## Pre-publish checklist

Per the project charter, nothing ships publicly until the maintainer
approves the publishing account/org and the license. Before
first publication:

1. **Secrets hygiene** — run a secrets scan over the repo and its history
   (`gitleaks detect --source .` or `trufflehog git file://.`), resolve
   every hit, and grep once more for obvious keys/tokens by hand.
2. **History review** — read `git log` and the full diff end-to-end; no
   credentials, private paths, or personal data may be committed.
3. **License** — replace the TBD notice with the maintainer-approved
   LICENSE file.
