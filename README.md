# agentscope — effective-instruction and effective-MCP resolver

"What instructions does harness X actually load for directory Y?" —
and, since v1.1.0, "which **MCP servers** does it actually load?"

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

## Supported harnesses

| Harness | What agentscope resolves |
|---|---|
| `codex` | `~/.codex/AGENTS.override.md` else `AGENTS.md` (first non-empty); then root→cwd, one file per dir (`AGENTS.override.md` > `AGENTS.md` > `project_doc_fallback_filenames`); combined-size vs `project_doc_max_bytes` (default 32 KiB) with a silent-truncation warning |
| `claude` | upward `CLAUDE.md` (+ `.claude/CLAUDE.md`) merge, root first; `AGENTS.md` fallback when no `CLAUDE.md` exists upward; `CLAUDE.local.md`; `~/.claude/CLAUDE.md`; `~/.claude/rules/*.md` (structure only); `@path` imports inlined (recursion cap 4, cycle-safe) |
| `cursor` | `.cursor/rules/*.{mdc,md}` with frontmatter scope (`globs`/`alwaysApply`) matched against the directory (heuristic); legacy `.cursorrules`; nested `AGENTS.md` combined with parents |
| `copilot` | nearest `.github/copilot-instructions.md`; `.github/instructions/*.instructions.md` with `applyTo` matching (heuristic); nearest `AGENTS.md` wins; root `CLAUDE.md`/`GEMINI.md` alternatives |
| `gemini` | `context.fileName` from `.gemini/settings.json` else `~/.gemini/settings.json` (default `GEMINI.md`); global `~/.gemini/GEMINI.md`; upward walk concatenated general→specific; `@file` imports inlined |

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

## Testing

```bash
python3 -m unittest test_agentscope -v   # 45 tests
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
