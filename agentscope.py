#!/usr/bin/env python3
"""agentscope — effective-instruction resolver: what instructions does harness X
actually load for directory Y?

Different coding-agent harnesses discover instruction files (AGENTS.md,
CLAUDE.md, GEMINI.md, .cursor/rules, ...) with genuinely different semantics:
Codex concatenates root-to-cwd with a silent 32 KiB cap, Claude Code merges
upward with @-imports, Cursor scopes rules by glob, Copilot takes the nearest
AGENTS.md, Gemini lets you rename the context file. When "the agent ignored my
rule", the answer is usually in the loading rules, not the rule text — and it
is currently answered only by prose guides. agentscope resolves the effective
loaded set per harness for a directory, from the files on disk.

    agentscope                        # all harnesses, current dir
    agentscope --harness codex apps/web
    agentscope --harness claude --harness gemini .
    agentscope --outline               # structure only: just the file lists
    agentscope --full                  # also print the merged content
    agentscope --json
    agentscope --diff-harness codex claude apps/web
    agentscope --hooks                          # effective hook sets
    agentscope --hooks --harness claude .
    agentscope --permissions                    # effective allow/deny rule sets
    agentscope --skills                         # discoverable skills per harness
    agentscope --skills --harness opencode .

Exit codes: 0 = at least one contributing item resolved; 1 = nothing resolved
for the selected harness(es) (the harness would enforce nothing); 2 = usage
error. In --permissions mode, "contributing item" = a rule or a context
setting (sandbox_mode, approval_policy, ...).

Stdlib only. Strictly READ-ONLY: never writes, never executes anything, never
touches the network. Safe to run anywhere.

HONESTY MODEL: agentscope is a model of DOCUMENTED harness behavior, not
runtime truth. Every rule applied is tagged [documented] or [inferred] and the
banner carries the date the rules were last verified against public docs.
Harness behavior drifts with releases; when in doubt, check the harness's own
docs. Truncation is reported as total-bytes vs cap with an ESTIMATED cutoff —
never a claimed exact drop line, because the true truncation point depends on
the harness's internal encoding.

v1.1.0 adds --mcp: resolve the EFFECTIVE MCP server set a harness loads for
a directory (which servers, from which config scope, stdio vs http, env var
NAMES only). Same honesty model; secret-safety is structural — env/header
values and URL query strings are NEVER printed, and --full is refused in
--mcp mode.
"""
import argparse
import fnmatch
import glob
import json
import os
import re
import sys

VERSION = "1.8.0"
RULES_VERIFIED = "2026-10-23"  # date harness rules were last checked vs docs

DOC = "documented"
INF = "inferred"

BANNER = (
    "agentscope v%s — a MODEL of documented harness behavior, not runtime truth.\n"
    "Rules last verified against public docs: %s. [documented] = from vendor docs\n"
    "or current guides; [inferred] = best-effort interpretation, may be wrong."
) % (VERSION, RULES_VERIFIED)

MAX_IMPORT_DEPTH = 5          # [inferred] default recursion cap for @-imports
CODEX_CAP_DEFAULT = 32 * 1024  # [documented] project_doc_max_bytes default
CLAUDE_IMPORT_HOPS = 4        # [documented] per Claude Code memory docs (via guides)

HARNESSES = ("codex", "claude", "cursor", "copilot", "gemini", "opencode",
             "antigravity", "cline")
ALIASES = {"claude-code": "claude"}


# --------------------------------------------------------------------------
# helpers (all read-only)
# --------------------------------------------------------------------------

def home():
    return os.path.expanduser("~")


def upward(path):
    """Yield absolute dirs from path up to the filesystem root."""
    cur = os.path.abspath(path)
    while True:
        yield cur
        parent = os.path.dirname(cur)
        if parent == cur:
            return
        cur = parent


def git_root(path):
    """Nearest ancestor containing .git (dir or worktree file), else None."""
    for d in upward(path):
        g = os.path.join(d, ".git")
        if os.path.isdir(g) or os.path.isfile(g):
            return d
    return None


def read_bytes(path):
    try:
        if os.path.isfile(path):
            with open(path, "rb") as f:
                return f.read()
    except OSError:
        pass
    return None


def non_empty(path):
    data = read_bytes(path)
    return data if data else None


def parse_simple_toml_int(path, key):
    """Naive top-level `key = 1234` scan of a TOML file. [inferred] best-effort."""
    data = read_bytes(path)
    if not data:
        return None
    try:
        text = data.decode("utf-8", "replace")
    except Exception:
        return None
    m = re.search(r"(?m)^\s*" + re.escape(key) + r"\s*=\s*(\d+)", text)
    return int(m.group(1)) if m else None


def parse_simple_toml_strlist(path, key):
    data = read_bytes(path)
    if not data:
        return None
    try:
        text = data.decode("utf-8", "replace")
    except Exception:
        return None
    m = re.search(r"(?m)^\s*" + re.escape(key) + r"\s*=\s*\[([^\]]*)\]", text)
    if not m:
        return None
    return re.findall(r'"([^"]*)"', m.group(1))


def read_json_obj(path):
    data = read_bytes(path)
    if not data:
        return None
    try:
        obj = json.loads(data.decode("utf-8", "replace"))
    except Exception:
        return None
    return obj if isinstance(obj, dict) else None


def parse_frontmatter(text):
    """Return dict of a few known frontmatter keys, raw. Heuristic parse."""
    out = {}
    if not text.startswith("---"):
        return out
    end = text.find("\n---", 3)
    if end == -1:
        return out
    head = text[3:end]
    for m in re.finditer(r"(?m)^\s*(globs|applyTo|paths|alwaysApply|description)\s*:\s*(.+?)\s*$", head):
        out[m.group(1)] = m.group(2).strip().strip('"\'')
    return out


def glob_hits(patterns, relpath):
    """True if any comma/space-separated glob pattern matches relpath. Heuristic."""
    if not patterns:
        return None
    pats = [p.strip().strip('"\'') for p in re.split(r"[,\s]+", patterns) if p.strip()]
    return any(fnmatch.fnmatch(relpath, p) or fnmatch.fnmatch(os.path.basename(relpath), p)
               for p in pats)


def scope_matches(root, rel, patterns):
    """Heuristic: does a glob-scoped rule apply when working in DIR?

    1. The DIR path itself matches (directory-scoped globs like `src/**`).
    2. Otherwise, any file under DIR matches (file-scoped globs like
       `**/*.py` — the rule fires when you touch matching files, so the
       presence of such files under DIR is the signal).
    Returns True / False / None (undecided: file budget exhausted).
    """
    if glob_hits(patterns, rel):
        return True
    base = os.path.join(root, rel) if rel != "." else root
    budget = 5000
    try:
        for dp, _, fns in os.walk(base):
            for f in fns:
                budget -= 1
                if budget <= 0:
                    return None
                rp = os.path.relpath(os.path.join(dp, f), root)
                if rp.split(os.sep)[0] == ".git":
                    continue
                if glob_hits(patterns, rp) or glob_hits(patterns, os.path.basename(rp)):
                    return True
    except OSError:
        return None
    return False


AT_TOKEN_RE = re.compile(r"(?:^|\s)@([^\s`'\"()\[\]{}<>|;]+)")


def find_imports(text, owner_path):
    """Candidate @path import tokens. Only tokens that resolve to a real file
    relative to the owner (or absolute) are returned — anything else (emails,
    mentions) is skipped, never warned about."""
    found = []
    base = os.path.dirname(owner_path)
    for m in AT_TOKEN_RE.finditer(text):
        tok = m.group(1).rstrip(".,:!?")
        cand = tok if os.path.isabs(tok) else os.path.normpath(os.path.join(base, tok))
        if os.path.isfile(cand):
            found.append(cand)
    # de-dup, preserve order
    seen, uniq = set(), []
    for c in found:
        if c not in seen:
            seen.add(c)
            uniq.append(c)
    return uniq


def expand_imports(contrib, max_depth, seen):
    """Recursively inline @-import contributors. seen = set of realpaths on the
    current chain (cycle detection). Returns (children, warnings)."""
    children, warnings = [], []
    text = contrib.get("text") or ""
    if contrib["depth"] >= max_depth:
        if find_imports(text, contrib["path"]):
            warnings.append("import depth cap reached at %s; deeper @-imports not followed"
                            % contrib["path"])
        return children, warnings
    for imp in find_imports(text, contrib["path"]):
        rp = os.path.realpath(imp)
        if rp in seen:
            warnings.append("import cycle skipped: %s" % imp)
            continue
        data = read_bytes(imp)
        if data is None:
            warnings.append("import target unreadable: %s" % imp)
            continue
        try:
            itext = data.decode("utf-8", "replace")
        except Exception:
            warnings.append("import target not decodable: %s" % imp)
            continue
        child = {"path": imp, "bytes": len(data), "text": itext,
                 "role": "import", "depth": contrib["depth"] + 1,
                 "via": contrib["via_import_tag"],
                 "via_import_tag": contrib["via_import_tag"]}
        grandchildren, w2 = expand_imports(child, max_depth, seen | {rp})
        child["imports"] = grandchildren
        children.append(child)
        warnings.extend(w2)
    return children, warnings


def new_resolution(harness):
    return {"harness": harness, "contributors": [], "warnings": [], "notes": [],
            "total_bytes": 0, "cap_bytes": None, "estimated_truncated": False}


def add_file(res, path, role, via, depth=0):
    data = read_bytes(path)
    if data is None:
        return None
    contrib = {"path": os.path.abspath(path), "bytes": len(data),
               "role": role, "depth": depth, "via": via, "imports": []}
    try:
        contrib["text"] = data.decode("utf-8", "replace")
    except Exception:
        contrib["text"] = ""
    res["contributors"].append(contrib)
    res["total_bytes"] += len(data)
    return contrib


# --------------------------------------------------------------------------
# harness resolvers — each returns a resolution dict
# --------------------------------------------------------------------------

def resolve_codex(d):
    res = new_resolution("codex")
    h = home()
    # Global: AGENTS.override.md else AGENTS.md, first non-empty. [documented]
    for name in ("AGENTS.override.md", "AGENTS.md"):
        p = os.path.join(h, ".codex", name)
        if non_empty(p):
            add_file(res, p, "global",
                     "[documented] ~/.codex: first non-empty of AGENTS.override.md, AGENTS.md")
            break
    base = git_root(d) or os.path.abspath(d)
    # Project: root -> cwd, at most one file per dir:
    # AGENTS.override.md > AGENTS.md > project_doc_fallback_filenames. [documented]
    cfg = os.path.join(h, ".codex", "config.toml")
    fallbacks = parse_simple_toml_strlist(cfg, "project_doc_fallback_filenames") or []
    chain = []
    cur, target = os.path.abspath(base), os.path.abspath(d)
    while True:
        chain.append(cur)
        if cur == target:
            break
        nxt = os.path.join(cur, os.path.relpath(target, cur).split(os.sep)[0])
        if nxt == cur:  # target not under base (shouldn't happen); bail
            break
        cur = nxt
    for level in chain:
        names = ["AGENTS.override.md", "AGENTS.md"] + fallbacks
        for i, name in enumerate(names):
            p = os.path.join(level, name)
            if non_empty(p):
                via = ("[documented] per-directory pick: AGENTS.override.md replaces "
                       "AGENTS.md here" if name == "AGENTS.override.md" and i == 0
                       else "[documented] root->cwd concat, closer-to-cwd later = wins")
                if i >= 2:
                    via = "[inferred] config project_doc_fallback_filenames (naive TOML scan)"
                add_file(res, p, "project", via)
                break
    cap = parse_simple_toml_int(cfg, "project_doc_max_bytes")
    res["cap_bytes"] = cap if cap else CODEX_CAP_DEFAULT
    if res["total_bytes"] > res["cap_bytes"]:
        res["estimated_truncated"] = True
        res["warnings"].append(
            "[documented] combined %d bytes exceeds project_doc_max_bytes=%d; Codex "
            "truncates SILENTLY (no error, no warning) — content past the estimated "
            "cutoff may never reach the model. Estimated cutoff, not the exact drop "
            "line." % (res["total_bytes"], res["cap_bytes"]))
    res["notes"].append(
        "[documented] since v0.150.0, untrusted projects skip project-level discovery "
        "(user-level ~/.codex file still loads) — agentscope cannot tell from disk "
        "whether Codex trusts this checkout.")
    return res


def resolve_claude(d):
    res = new_resolution("claude")
    h = home()
    res["notes"].append(
        "[inferred] enterprise managed policy is not resolvable from disk; it loads "
        "before everything below when present.")
    up = os.path.join(h, ".claude", "CLAUDE.md")
    if non_empty(up):
        add_file(res, up, "user",
                 "[documented] ~/.claude/CLAUDE.md loads before project files")
    # Project: upward walk, CLAUDE.md (+ .claude/CLAUDE.md) at each level,
    # root-first; closest-to-cwd later = wins. [documented]
    levels = list(upward(d))
    found_any = False
    for level in reversed(levels):
        for name in ("CLAUDE.md", os.path.join(".claude", "CLAUDE.md")):
            p = os.path.join(level, name)
            if non_empty(p):
                add_file(res, p, "project",
                         "[documented] upward merge, root first, closest-to-cwd wins")
                found_any = True
    if not found_any:
        # [documented] fallback (importstatic table, current Oct 2026): if no
        # CLAUDE.md exists from cwd up, AGENTS.md / .claude/AGENTS.md load instead.
        for level in reversed(levels):
            for name in ("AGENTS.md", os.path.join(".claude", "AGENTS.md")):
                p = os.path.join(level, name)
                if non_empty(p):
                    add_file(res, p, "project-fallback",
                             "[documented] no CLAUDE.md found upward; AGENTS.md fallback loads")
    # CLAUDE.local.md: personal, gitignored. [documented]
    for p in (os.path.join(os.path.abspath(d), "CLAUDE.local.md"),
              os.path.join(git_root(d) or os.path.abspath(d), "CLAUDE.local.md")):
        if non_empty(p) and not any(c["path"] == os.path.abspath(p)
                                    for c in res["contributors"]):
            add_file(res, p, "local",
                     "[documented] CLAUDE.local.md: personal, not committed; "
                     "[inferred] checked at cwd and git root")
    # Modular rules. [documented] that they exist/are conditional; exact load
    # semantics are path-scoped — structure only here.
    rules_dir = os.path.join(h, ".claude", "rules")
    try:
        names = sorted(os.listdir(rules_dir))
    except OSError:
        names = []
    for n in names:
        if n.endswith(".md"):
            p = os.path.join(rules_dir, n)
            if non_empty(p):
                add_file(res, p, "modular-rule",
                         "[documented] ~/.claude/rules/*.md: conditional, path-scoped "
                         "via frontmatter; structure only")
    # @path imports, up to 4 hops. [documented] cap; inline position [inferred].
    for c in res["contributors"]:
        c["via_import_tag"] = ("[documented] @path import inlined at import point "
                               "(recursion cap 4 hops)")
        children, warns = expand_imports(c, CLAUDE_IMPORT_HOPS, {os.path.realpath(c["path"])})
        c["imports"] = children
        res["warnings"].extend(warns)
        for _ch in flatten(c):
            res["total_bytes"] += _ch["bytes"]
    return res


def flatten(contrib):
    out = []
    for ch in contrib.get("imports", []):
        out.append(ch)
        out.extend(flatten(ch))
    return out


def resolve_cursor(d):
    res = new_resolution("cursor")
    root = git_root(d) or os.path.abspath(d)
    rel = os.path.relpath(os.path.abspath(d), root)
    # Project rules. [documented] .mdc; .md accepted leniently [inferred].
    rules_dir = os.path.join(root, ".cursor", "rules")
    try:
        names = sorted(os.listdir(rules_dir))
    except OSError:
        names = []
    for n in names:
        if not (n.endswith(".mdc") or n.endswith(".md")):
            continue
        p = os.path.join(rules_dir, n)
        data = non_empty(p)
        if not data:
            continue
        c = add_file(res, p, "project-rule",
                     "[documented] .cursor/rules/*: frontmatter-scoped project rules; "
                     "[inferred] .md accepted alongside .mdc")
        fm = parse_frontmatter(data.decode("utf-8", "replace"))
        scope_bits = []
        if fm.get("alwaysApply", "").lower() == "true":
            scope_bits.append("alwaysApply=true")
        g = fm.get("globs")
        if g:
            hit = scope_matches(root, rel, g)
            scope_bits.append("globs=%s -> %s DIR (heuristic)" %
                             (g, "MATCHES" if hit else
                              ("undecided" if hit is None else "does not match")))
        elif "globs" not in fm and "alwaysApply" not in fm:
            scope_bits.append("no scope frontmatter [inferred: treated as manual/always]")
        c["scope"] = "; ".join(scope_bits) if scope_bits else ""
    legacy = os.path.join(root, ".cursorrules")
    if non_empty(legacy):
        add_file(res, legacy, "project-rule",
                 "[documented] legacy .cursorrules single-file project rules")
    # Nested AGENTS.md: combined with parents, more specific wins. [documented]
    for level in reversed(list(upward(d))):
        if _under(level, root, os.path.abspath(d)):
            p = os.path.join(level, "AGENTS.md")
            if non_empty(p):
                add_file(res, p, "project",
                         "[documented] nested AGENTS.md combined with parents, "
                         "more specific wins")
    res["notes"].append(
        "[documented] precedence: team, then project, then user rules. "
        "[inferred] user/team rules live in Cursor settings, not resolvable from disk.")
    return res


def _under(level, root, d):
    """True if level is on the path root..d (inclusive)."""
    try:
        rel = os.path.relpath(level, root)
    except ValueError:
        return False
    if rel == ".":
        return True
    drel = os.path.relpath(d, root)
    return drel == rel or drel.startswith(rel + os.sep)


def resolve_copilot(d):
    res = new_resolution("copilot")
    root = git_root(d) or os.path.abspath(d)
    # .github/copilot-instructions.md: nearest up the tree wins. [inferred]
    # (documented that the file is read; nearest-wins is the documented rule
    # for AGENTS.md, applied here the same way)
    for level in upward(d):
        p = os.path.join(level, ".github", "copilot-instructions.md")
        if non_empty(p):
            add_file(res, p, "project",
                     "[documented] .github/copilot-instructions.md; "
                     "[inferred] nearest-up-the-tree wins")
            break
    # .github/instructions/*.instructions.md: path-scoped. [documented]
    for base in (os.path.abspath(d), root):
        idir = os.path.join(base, ".github", "instructions")
        try:
            names = sorted(os.listdir(idir))
        except OSError:
            continue
        for n in names:
            if not n.endswith(".instructions.md"):
                continue
            p = os.path.join(idir, n)
            data = non_empty(p)
            if not data:
                continue
            c = add_file(res, p, "path-scoped",
                         "[documented] .github/instructions/*.instructions.md")
            fm = parse_frontmatter(data.decode("utf-8", "replace"))
            at = fm.get("applyTo", "")
            if at:
                rel = os.path.relpath(os.path.abspath(d), root)
                hit = scope_matches(root, rel, at)
                c["scope"] = ("applyTo=%s -> %s DIR (heuristic)" %
                              (at, "MATCHES" if hit else
                               ("undecided" if hit is None else "does not match")))
    # AGENTS.md: nearest in the directory tree takes precedence. [documented]
    for level in upward(d):
        p = os.path.join(level, "AGENTS.md")
        if non_empty(p):
            add_file(res, p, "project",
                     "[documented] nearest AGENTS.md in the tree takes precedence")
            break
    # CLAUDE.md / GEMINI.md at repo root accepted as alternatives. [documented]
    for name in ("CLAUDE.md", "GEMINI.md"):
        p = os.path.join(root, name)
        if non_empty(p) and not any(c["path"] == os.path.abspath(p)
                                    for c in res["contributors"]):
            add_file(res, p, "project-alt",
                     "[documented] Copilot accepts one root CLAUDE.md/GEMINI.md "
                     "as an AGENTS.md alternative")
    res["notes"].append(
        "[inferred] VS Code extras (chat.useClaudeMdFile, useNestedAgentsMdFiles) "
        "are editor settings, not resolvable from disk.")
    return res


def resolve_gemini(d):
    res = new_resolution("gemini")
    h = home()
    # context.fileName: default GEMINI.md, configurable (string or list) in
    # project then user settings. [documented] key; [inferred] merge choice.
    names = ["GEMINI.md"]
    used_cfg = None
    for cfg_path in (os.path.join(os.path.abspath(d), ".gemini", "settings.json"),
                     os.path.join(h, ".gemini", "settings.json")):
        cfg = read_json_obj(cfg_path)
        if cfg:
            ctx = cfg.get("context") or {}
            fn = ctx.get("fileName") or ctx.get("fileNames")
            if isinstance(fn, str):
                names, used_cfg = [fn], cfg_path
                break
            if isinstance(fn, list) and fn:
                names, used_cfg = [str(x) for x in fn], cfg_path
                break
    if used_cfg:
        res["notes"].append("[documented] context.fileName from %s -> %s"
                            % (used_cfg, ", ".join(names)))
    gp = os.path.join(h, ".gemini", "GEMINI.md")
    if non_empty(gp):
        add_file(res, gp, "global", "[documented] ~/.gemini/GEMINI.md base layer")
    # Upward walk: each configured name at each level, concatenated
    # general -> specific. [documented]
    for level in reversed(list(upward(d))):
        for name in names:
            for variant, tag in ((name, DOC),
                                 (os.path.join(".gemini", name), INF)):
                p = os.path.join(level, variant)
                if non_empty(p):
                    add_file(res, p, "project",
                             "[%s] upward walk, concatenated general->specific"
                             % tag)
    # @file.md imports: relative + absolute paths. [documented]; cap [inferred].
    for c in res["contributors"]:
        c["via_import_tag"] = ("[documented] @file import inlined at import point; "
                               "[inferred] recursion cap %d" % MAX_IMPORT_DEPTH)
        children, warns = expand_imports(c, MAX_IMPORT_DEPTH, {os.path.realpath(c["path"])})
        c["imports"] = children
        res["warnings"].extend(warns)
        for _ch in flatten(c):
            res["total_bytes"] += _ch["bytes"]
    return res


def resolve_opencode(d):
    """OpenCode instruction resolution.

    Rules verified 2026-10-22 against the vendor docs
    (opencode.ai/docs/rules/, page itself updated ~1 day before the check).
    Semantics differ genuinely from the other five harnesses:
      - FIRST-MATCH-WINS per directory (AGENTS.md shadows CLAUDE.md), not
        the concatenation every other harness does [documented];
      - global ~/.config/opencode/AGENTS.md, then a Claude Code compat
        fallback (~/.claude/CLAUDE.md) gated by OPENCODE_DISABLE_CLAUDE_CODE*
        env vars [documented];
      - opencode.json `instructions` array (paths, globs, remote URLs) whose
        entries are combined with the AGENTS.md files [documented];
      - opencode does NOT parse @file references in AGENTS.md (no import
        expansion) [documented];
      - the harness can attach a nearer instruction file as a system
        reminder when its read tool opens a file — dynamic per-read
        behavior, not statically capturable [documented, noted only].
    The order matches are combined in is [inferred]: docs give the lookup
    order (local, then global, then Claude-compat), not the merge order.
    Strictly read-only.
    """
    res = new_resolution("opencode")
    h = home()
    res["notes"].append(
        "[documented] precedence: (1) local files traversing up from the cwd "
        "(first match wins per directory: AGENTS.md over CLAUDE.md), "
        "(2) ~/.config/opencode/AGENTS.md, (3) ~/.claude/CLAUDE.md compat "
        "fallback unless disabled by OPENCODE_DISABLE_CLAUDE_CODE or "
        "OPENCODE_DISABLE_CLAUDE_CODE_PROMPT")
    # 1. local traversal, cwd -> up; first filename family with a match wins
    # per directory [documented]. Empty files do not count (same non_empty
    # convention as the other resolvers) [inferred].
    for level in upward(d):
        agents_p = os.path.join(level, "AGENTS.md")
        claude_p = os.path.join(level, "CLAUDE.md")
        if non_empty(agents_p):
            via = ("[documented] first match wins at this level"
                   if not non_empty(claude_p) else
                   "[documented] AGENTS.md shadows CLAUDE.md here "
                   "(first matching file wins)")
            add_file(res, agents_p, "project", via)
        elif non_empty(claude_p):
            add_file(res, claude_p, "project",
                     "[documented] CLAUDE.md used (no AGENTS.md at this "
                     "level)")
    # 2. global file, then Claude compat fallback
    gp = os.path.join(h, ".config", "opencode", "AGENTS.md")
    claude_global = os.path.join(h, ".claude", "CLAUDE.md")
    if non_empty(gp):
        add_file(res, gp, "global",
                 "[documented] ~/.config/opencode/AGENTS.md")
        if non_empty(claude_global):
            res["notes"].append(
                "[documented] ~/.claude/CLAUDE.md present but unused: "
                "global AGENTS.md takes precedence")
    else:
        disabled = (os.environ.get("OPENCODE_DISABLE_CLAUDE_CODE") == "1" or
                    os.environ.get("OPENCODE_DISABLE_CLAUDE_CODE_PROMPT")
                    == "1")
        if non_empty(claude_global):
            if disabled:
                res["notes"].append(
                    "[documented] ~/.claude/CLAUDE.md present but the compat "
                    "fallback is disabled via env (OPENCODE_DISABLE_"
                    "CLAUDE_CODE*)")
            else:
                add_file(res, claude_global, "global",
                         "[documented] Claude Code compat fallback (no "
                         "~/.config/opencode/AGENTS.md)")
        elif not disabled:
            res["notes"].append(
                "[inferred] Claude compat fallback checked "
                "(~/.claude/CLAUDE.md absent)")
    # 3. opencode.json `instructions` — project config, then global config
    # [documented]. Entries are paths, globs, or remote URLs. The project
    # config is found by searching upward (the harness loads it from the
    # project directory; the search-upward is [inferred]).
    cfgs = []
    proj_cfg = None
    for level in upward(d):
        cand = os.path.join(level, "opencode.json")
        if os.path.isfile(cand):
            proj_cfg = cand
            break
    for cfg_path in ([proj_cfg] if proj_cfg else []) + [
            os.path.join(h, ".config", "opencode", "opencode.json")]:
        cfg = read_json_obj(cfg_path)
        if not cfg:
            continue
        cfgs.append(cfg_path)
        cfg_dir = os.path.dirname(cfg_path)
        entries = cfg.get("instructions") or []
        if not isinstance(entries, list):
            res["warnings"].append(
                "opencode.json `instructions` is not a list: %s" % cfg_path)
            continue
        for e in entries:
            if not isinstance(e, str) or not e:
                continue
            if re.match(r"^https?://", e):
                res["warnings"].append(
                    "[documented] remote `instructions` entry NOT fetched "
                    "(read-only; the harness fetches remotes with a 5s "
                    "timeout): %s" % e)
                continue
            pat = e if os.path.isabs(e) else os.path.join(cfg_dir, e)
            hits = sorted(x for x in glob.glob(pat, recursive=True)
                          if os.path.isfile(x))
            if not hits:
                res["warnings"].append(
                    "opencode.json `instructions` entry matched nothing: "
                    "%r (from %s)" % (e, cfg_path))
                continue
            for hit in hits:
                add_file(res, hit, "instructions",
                         "[documented] opencode.json `instructions` entry "
                         "%r (%s)" % (e, cfg_path))
    if cfgs:
        res["notes"].append(
            "[documented] `instructions` read from: %s" % ", ".join(cfgs))
    else:
        res["notes"].append(
            "[inferred] no opencode.json found at project or global level")
    res["notes"].append(
        "[documented] opencode does NOT parse @file references in AGENTS.md "
        "(no import expansion — use `instructions` instead)")
    res["notes"].append(
        "[documented] the harness can attach a nearer instruction file as a "
        "system reminder when its read tool opens a file — dynamic per-read "
        "behavior, not captured by a static resolution")
    return res


# --------------------------------------------------------------------------
# Antigravity (v1.6.0) — Google's agent IDE/CLI/App.
#
# Rules verified 2026-10-23 against the OFFICIAL docs:
#   instructions: antigravity.google/docs/rules/
#   hooks:        antigravity.google/docs/hooks/
#   permissions:  antigravity.google/docs/permissions/
# MCP global config: ~/.gemini/config/mcp_config.json [documented in
# Google Cloud docs + multiple guides]; project-level
# .agents/mcp_config.json is community-reported, tagged [inferred].

ANTIGRAVITY_RULE_FILE_LIMIT = 24000  # bytes; harness truncates above it
ANTIGRAVITY_HOOK_EVENTS = ("SessionStart", "PreToolUse", "PostToolUse",
                           "PreInvocation", "PostInvocation", "Stop")


def _ag_frontmatter(path):
    """Parse the tiny frontmatter of a rules/*.md file -> dict.

    Only the documented keys (trigger, description, globs/glob); a real
    YAML parser is overkill and this never executes anything.
    """
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            head = fh.read(4096)
    except OSError:
        return {}
    m = re.match(r"\s*---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(\r?\n|$)",
                 head, re.S)
    if not m:
        return {}
    fm = {}
    for line in m.group(1).splitlines():
        mm = re.match(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*:\s*(.*?)\s*$", line)
        if mm:
            v = mm.group(2)
            if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                v = v[1:-1]
            fm[mm.group(1)] = v
    return fm


def _ag_rules_json_files(agents_dir, res, _depth=0):
    """Expand .agents/rules.json inherits/entries -> [(path, via-note)].

    [documented] shape; entry paths are resolved relative to the .agents
    dir [inferred]. Recursion capped at 3.
    """
    if _depth > 3:
        res["warnings"].append(
            ".agents/rules.json inherits nesting deeper than 3 at %s; "
            "stopped [inferred]" % agents_dir)
        return []
    cfg_path = os.path.join(agents_dir, "rules.json")
    obj = read_json_obj(cfg_path)
    if obj is None:
        return []
    out = []
    for inh in obj.get("inherits") or []:
        ip = inh.get("path") if isinstance(inh, dict) else None
        if not ip:
            continue
        target = os.path.normpath(os.path.join(agents_dir, ip))
        parent = os.path.dirname(target)
        if os.path.basename(target) == "rules.json" and os.path.isdir(parent):
            out.extend(_ag_rules_json_files(parent, res, _depth + 1))
        else:
            res["warnings"].append(
                "rules.json inherits entry %r does not resolve to a "
                "rules.json file; skipped [inferred]" % ip)
    for entry in obj.get("entries") or []:
        if not isinstance(entry, dict) or not entry.get("path"):
            continue
        base = os.path.normpath(os.path.join(agents_dir, entry["path"]))
        if not os.path.isdir(base):
            res["warnings"].append(
                "rules.json entry path %r matched nothing; skipped "
                "[inferred]" % entry["path"])
            continue
        exclude = set(entry.get("exclude") or [])
        only = entry.get("include_only")
        for root, _dirs, files in os.walk(base):
            for fn in sorted(files):
                if not fn.endswith(".md"):
                    continue
                rel = os.path.relpath(os.path.join(root, fn), base)
                if rel in exclude or fn in exclude:
                    continue
                if only and rel not in only and fn not in only:
                    continue
                out.append((os.path.join(root, fn),
                            "[documented] .agents/rules.json entry %r "
                            "([inferred] paths relative to the .agents "
                            "dir)" % entry["path"]))
    return out


def _ag_add_rule_file(res, path, scope, via_extra=""):
    if not non_empty(path):
        return
    try:
        size = os.path.getsize(path)
    except OSError:
        size = 0
    if size > ANTIGRAVITY_RULE_FILE_LIMIT:
        res["warnings"].append(
            "%s is %d bytes > the 24,000-byte per-file limit: the harness "
            "truncates it (cutoff estimated, never claimed exact) "
            "[documented]" % (path, size))
    fm = _ag_frontmatter(path)
    via = "[documented] Antigravity rule file"
    if fm.get("trigger"):
        via += "; trigger=%s" % fm["trigger"]
        if fm.get("description"):
            via += "; description=%r" % fm["description"][:60]
        globs = fm.get("globs") or fm.get("glob")
        if globs:
            via += "; globs=%r" % globs
    elif os.path.basename(os.path.dirname(path)) == "rules":
        via += ("; NO frontmatter trigger — the harness requires one for "
                "rules/ files, this file may be ignored [documented]")
    if via_extra:
        via += "; " + via_extra
    add_file(res, path, scope, via)
    # @[label](path) includes are inlined by the harness (relative to the
    # rule file; frontmatter stripped) [documented]. Listed, not expanded.
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            text = fh.read(200000)
    except OSError:
        return
    incs = sorted(set(re.findall(r"@\[[^\]]*\]\(([^)]+)\)", text)))
    if incs:
        res["notes"].append(
            "[documented] %s references %d @[label](path) include(s), "
            "inlined by the harness relative to the rule file: %s"
            % (path, len(incs), ", ".join(incs)))


def resolve_antigravity(d):
    """Antigravity instruction resolution.

    Verified 2026-10-23 vs antigravity.google/docs/rules/ (official).
    Cumulative, not replacement-based; precedence highest to lowest:
      1. directory-scoped rules, nearest directory winning on conflict:
         <dir>/AGENTS.md | <dir>/GEMINI.md, <dir>/.agents/AGENTS.md |
         <dir>/.agents/GEMINI.md, <dir>/.agents/rules/*.md (plus legacy
         <dir>/.agent/rules/*.md) [documented];
      2. global rules: ~/.gemini/AGENTS.md, ~/.gemini/GEMINI.md,
         ~/.gemini/config/AGENTS.md, ~/.gemini/config/GEMINI.md
         (standalone, no frontmatter, always_on) and
         ~/.gemini/config/rules/*.md (frontmatter trigger required)
         [documented].
    Triggers: always_on | model_decision | glob | manual [documented].
    24,000-byte per-file limit (truncated) and a 20,000-token aggregate
    budget after which the largest rules demote to pointers [documented].
    @[label](path) includes are inlined relative to the rule file
    [documented]. .agents/rules.json inherits/entries share rule dirs
    [documented shape; relative-root inferred]. Strictly read-only.
    """
    res = new_resolution("antigravity")
    h = home()
    res["notes"].append(
        "[documented] precedence (highest to lowest): directory-scoped "
        "rules (nearest directory wins on conflict), then global rules; "
        "cumulative, not replacement-based. Rules verified 2026-10-23 vs "
        "antigravity.google/docs/rules/")
    # 1. directory-scoped: walk from DIR upward
    for level in upward(d):
        for name in ("AGENTS.md", "GEMINI.md"):
            _ag_add_rule_file(
                res, os.path.join(level, name), "project",
                "no frontmatter; always_on for this scope")
        for adir_name in (".agents", ".agent"):  # .agent = legacy
            adir = os.path.join(level, adir_name)
            for name in ("AGENTS.md", "GEMINI.md"):
                _ag_add_rule_file(
                    res, os.path.join(adir, name), "project",
                    "%s/%s; always_on" % (adir_name, name))
            rules_dir = os.path.join(adir, "rules")
            if os.path.isdir(rules_dir):
                for fn in sorted(os.listdir(rules_dir)):
                    if fn.endswith(".md"):
                        _ag_add_rule_file(
                            res, os.path.join(rules_dir, fn), "rule",
                            "%s/rules/%s" % (adir_name, fn))
            for rp, rvia in _ag_rules_json_files(adir, res):
                _ag_add_rule_file(res, rp, "rule", rvia)
    # 2. global
    for gp in (os.path.join(h, ".gemini", "AGENTS.md"),
               os.path.join(h, ".gemini", "GEMINI.md"),
               os.path.join(h, ".gemini", "config", "AGENTS.md"),
               os.path.join(h, ".gemini", "config", "GEMINI.md")):
        _ag_add_rule_file(res, gp, "global",
                          "standalone global file; always_on")
    grules = os.path.join(h, ".gemini", "config", "rules")
    if os.path.isdir(grules):
        for fn in sorted(os.listdir(grules)):
            if fn.endswith(".md"):
                _ag_add_rule_file(
                    res, os.path.join(grules, fn), "rule",
                    "modular global rule (frontmatter trigger required)")
    res["notes"].append(
        "[documented] 20,000-token aggregate rules budget: when exceeded, "
        "the harness demotes the largest rules to pointers "
        "(`- <path>: <description>`); agentscope cannot compute token "
        "usage, so the cutoff is not predicted here")
    res["notes"].append(
        "[documented] model_decision rules inject only path+description "
        "upfront (progressive disclosure); manual rules load only on "
        "explicit @-mention; glob rules activate on file-pattern match — "
        "all dynamic, reported structurally above")
    res["notes"].append(
        "[inferred] the harness walks from the touched file's folder to "
        "the WORKSPACE root; agentscope walks to the filesystem root, so "
        "ancestors above the real workspace root may over-report")
    return res


def hooks_from_antigravity_shape(res, obj, source, scope, via):
    """Parse an Antigravity hooks.json: name -> {enabled?, Event: [...]}."""
    if not isinstance(obj, dict):
        res["warnings"].append(
            "malformed hooks.json in %s (expected an object); skipped" % source)
        return
    for hook_name, definition in sorted(obj.items()):
        if not isinstance(definition, dict):
            res["warnings"].append(
                "hook %r in %s is not an object; skipped" % (hook_name, source))
            continue
        if definition.get("enabled") is False:
            res["notes"].append(
                "hook %r in %s disabled (enabled: false); skipped "
                "[documented]" % (hook_name, source))
            continue
        for event, handlers in definition.items():
            if event == "enabled":
                continue
            if event not in ANTIGRAVITY_HOOK_EVENTS:
                res["warnings"].append(
                    "[inferred] %r in %s is not a documented Antigravity "
                    "hook event; the harness may ignore it"
                    % (event, source))
            if not isinstance(handlers, list):
                res["warnings"].append(
                    "hook %r event %r in %s is not a list; skipped"
                    % (hook_name, event, source))
                continue
            for hdef in handlers:
                if not isinstance(hdef, dict):
                    continue
                matcher = hdef.get("matcher", "")
                entries = hdef.get("hooks")
                if not isinstance(entries, list):
                    res["warnings"].append(
                        "hook %r event %r in %s has no hooks list; skipped"
                        % (hook_name, event, source))
                    continue
                for entry in entries:
                    if not isinstance(entry, dict):
                        continue
                    etype = entry.get("type", "command")
                    if etype != "command":
                        res["warnings"].append(
                            "hook %r event %r in %s: type %r is not "
                            "'command' (the only documented type); skipped "
                            "[documented]" % (hook_name, event, source,
                                              etype))
                        continue
                    cmd = entry.get("command")
                    if not cmd:
                        res["warnings"].append(
                            "hook %r event %r in %s: entry has no command; "
                            "skipped" % (hook_name, event, source))
                        continue
                    add_hook(res, event, matcher, source, scope, cmd, via,
                             timeout=entry.get("timeout"),
                             note="hook %r [documented]" % hook_name)


def resolve_hooks_antigravity(d):
    """Antigravity hook resolution.

    Verified 2026-10-23 vs the OFFICIAL hooks reference
    (antigravity.google/docs/hooks/). Config: `.agents/hooks.json` at the
    workspace root and `~/.gemini/config/hooks.json` globally
    [documented]; the workspace file is found by nearest-upward search
    [inferred]. Six events (SessionStart, PreToolUse, PostToolUse,
    PreInvocation, PostInvocation, Stop); matcher is a regex over tool
    names for Pre/PostToolUse ("" or "*" = all) and ignored for the
    other four [documented]. Hook commands are shown verbatim and NEVER
    executed (agentscope's strict read-only guarantee).
    """
    res = new_hook_resolution("antigravity")
    h = home()
    ad = os.path.abspath(d)
    files = []
    for level in upward(ad):
        cand = os.path.join(level, ".agents", "hooks.json")
        if os.path.isfile(cand):
            files.append((cand, "workspace (nearest upward)",
                          "[documented] .agents/hooks.json = workspace "
                          "scope; [inferred] found by nearest-upward search"))
            break
    files.append((os.path.join(h, ".gemini", "config", "hooks.json"),
                  "global",
                  "[documented] ~/.gemini/config/hooks.json = global scope"))
    for path, scope, via in files:
        obj = read_json_obj(path)
        if obj is None:
            if os.path.exists(path):
                res["warnings"].append(
                    "%s exists but is unreadable/not JSON" % path)
            continue
        hooks_from_antigravity_shape(res, obj, path, scope, via)
    res["notes"].append(
        "[documented] hooks.json maps hook NAME -> {enabled?, Event: "
        "[{matcher, hooks: [{type, command, timeout}]}]}; commands are "
        "shown verbatim and NEVER executed by agentscope")
    res["notes"].append(
        "[documented] matcher is a regex over tool names for PreToolUse/"
        "PostToolUse (\"\" or \"*\" = all tools); ignored for SessionStart, "
        "PreInvocation, PostInvocation, Stop")
    res["notes"].append(
        "[documented] stdin JSON (camelCase) / stdout JSON contract; "
        "PreToolUse stdout REQUIRES a `decision` (allow | auto_approve | "
        "deny | ask | force_ask | deny_unless_prior_grant). Exit-code "
        "semantics are undocumented — hooktest --harness antigravity "
        "models the payloads and refuses to interpret non-zero exits")
    res["notes"].append(
        "[inferred] merge order across workspace/global scopes is "
        "undocumented; both scopes are listed above")
    return res


def resolve_mcp_antigravity(d):
    """Antigravity MCP servers.

    Global config ~/.gemini/config/mcp_config.json (`mcpServers` map)
    [documented in Google Cloud docs + guides]. Project-level
    .agents/mcp_config.json (nearest upward) is community-reported
    (medium.com 2026-10-22: the harness walks up looking for .agents/
    and loads MCP config from it) — tagged [inferred]. Same structural
    secret safety as the other MCP resolvers: env/header names only,
    URL query/fragment stripped, remotes never probed.
    """
    res = new_mcp_resolution("antigravity")
    h = home()
    gpath = os.path.join(h, ".gemini", "config", "mcp_config.json")
    gobj = read_json_obj(gpath)
    if gobj is not None:
        mcp_from_json_block(res, gobj.get("mcpServers"), gpath, "global",
                            "[documented] ~/.gemini/config/mcp_config.json")
    elif os.path.exists(gpath):
        res["warnings"].append("%s exists but is unreadable/not JSON" % gpath)
    for level in upward(os.path.abspath(d)):
        p = os.path.join(level, ".agents", "mcp_config.json")
        if os.path.isfile(p):
            pobj = read_json_obj(p)
            if pobj is not None:
                mcp_from_json_block(
                    res, pobj.get("mcpServers"), p, "project (nearest upward)",
                    "[inferred] .agents/mcp_config.json — community-"
                    "reported, not in the official docs checked 2026-10-23")
            else:
                res["warnings"].append(
                    "%s exists but is unreadable/not JSON" % p)
            break
    res["notes"].append(
        "[documented] env/header VALUES are never shown (names only); "
        "remote servers are never probed")
    res["notes"].append(
        "[documented] refer to secrets as ${VAR_NAME} in the config; "
        "agentscope reports the names, never the values")
    return res


def resolve_permissions_antigravity(d):
    """Antigravity permission rules: UI-managed, nothing static to resolve.

    The official permissions reference (antigravity.google/docs/
    permissions/, verified 2026-10-23) documents a Deny/Ask/Allow model
    over action(target) resources (read_file, write_file, read_url,
    execute_url, command, mcp) layered over presets (Request Review /
    Default / Auto (Preview) / Turbo) — all managed in Settings >
    General > Global Permissions / Settings > Projects. No static
    config-file format is documented, and permission grants are
    explicitly not repo-shippable, so an empty resolution with honest
    notes is the correct model (cf. the rejected effective-env
    resolver: don't print fake precision).
    """
    res = new_perm_resolution("antigravity")
    res["notes"].append(
        "[documented] permission rules (Deny/Ask/Allow lists of "
        "action(target) resources + presets) are UI-managed in Settings; "
        "no static config-file format is documented, so there is nothing "
        "to resolve from disk — this empty resolution is the honest model")
    res["notes"].append(
        "[documented] evaluation: Deny blocks immediately, Ask prompts, "
        "Allow skips prompting; configured rules layer over the preset "
        "(Request Review / Default / Auto (Preview) / Turbo) and always "
        "take precedence")
    res["notes"].append(
        "[documented] actions: read_file, write_file (implies read_file), "
        "read_url, execute_url, command (prefix or regex:), mcp "
        "(server/tool); write_file implies read_file, deny read implies "
        "deny write")
    res["notes"].append(
        "[documented] PreToolUse hooks CAN enforce permission decisions "
        "programmatically (decision + permissionOverrides) — see "
        "agentscope --harness antigravity --hooks and "
        "hooktest --harness antigravity")
    return res


# --------------------------------------------------------------------------
# Cline (v1.8.0) — instructions mode.
# Verified 2026-10-23 vs docs.cline.bot/customization/cline-rules (official;
# page updated 2026-10-07).
# --------------------------------------------------------------------------

def _cline_project_root(d):
    """Nearest ancestor of d holding workspace rule markers, else d.

    Workspace rules live at the project (workspace) root [documented]; when
    DIR is a subdirectory the upward walk finds that root [inferred]. The
    walk never ascends past $HOME, so a stray ~/AGENTS.md is never treated
    as workspace scope (Cline's workspace AGENTS.md is project-root only;
    the global one is ~/.agents/AGENTS.md).
    """
    d = os.path.abspath(d)
    h = os.path.abspath(home())
    markers = (".clinerules", os.path.join(".cline", "rules"),
               ".cursorrules", ".windsurfrules", "AGENTS.md")
    cur = d
    while True:
        for marker in markers:
            if os.path.exists(os.path.join(cur, marker)):
                return cur
        parent = os.path.dirname(cur)
        if parent == cur or cur == h:
            return d
        cur = parent


def _cline_paths(text):
    """Classify a Cline rule file's conditional frontmatter.

    Returns ("always",) | ("never",) | ("conditional", raw) | ("unknown",).
    Only the documented `paths` conditional is recognized. Regex parse of
    the frontmatter block only — YAML is never evaluated, nothing executed.
    """
    if not text.startswith("---"):
        return ("always",)
    end = text.find("\n---", 3)
    if end == -1:
        return ("always",)
    head = text[3:end]
    # NOTE: [ \t]*, not \s*, around the value — \s would swallow the
    # newline and merge a multi-line list into the single-line branch.
    m = re.search(r"(?m)^\s*paths\s*:[ \t]*(.*?)[ \t]*$", head)
    if not m:
        return ("always",)
    val = m.group(1)
    if val == "":
        items = []
        for line in head[m.end():].splitlines():
            lm = re.match(r"^\s*-\s*(.+?)\s*$", line)
            if lm:
                items.append(lm.group(1))
            elif line.strip() == "" or re.match(r"^\s*#", line):
                continue
            else:
                break
        if items:
            return ("conditional", "[%s]" % ", ".join(items))
        return ("unknown",)
    v = val.strip().strip("\"'")
    if v == "[]":
        return ("never",)
    return ("conditional", v)


def _cline_add_rule_file(res, path):
    data = non_empty(path)
    if not data:
        return
    text = data.decode("utf-8", "replace")
    kind = _cline_paths(text)
    if kind[0] == "never":
        via = ("[documented] conditional rule with empty paths: [] — the "
               "harness never activates it")
    elif kind[0] == "conditional":
        via = ("[documented] conditional rule (paths=%s): activates "
               "dynamically when the current context (open tabs, mentioned "
               "paths, edited files) matches — reported structurally, "
               "activation never predicted" % kind[1])
    elif kind[0] == "unknown":
        via = ("[documented] conditional frontmatter present but the paths "
               "value form was not parsed — activation dynamic, not "
               "predicted")
    else:
        via = ("[documented] workspace rule file, always active (no "
               "conditional frontmatter)")
    add_file(res, path, "project-rule", via)


def resolve_cline(d):
    """Cline instruction resolution.

    Verified 2026-10-23 vs docs.cline.bot/customization/cline-rules.
    Scopes (documented):
      - global: ~/Documents/Cline/Rules/*.md (macOS/Linux/WSL; Windows is
        Documents\\Cline\\Rules) plus ~/.agents/AGENTS.md — loaded first,
        the baseline;
      - workspace: .clinerules/ and .cline/rules/ at the project root
        (both searched when present, .md and .txt files processed,
        combined), plus auto-detected .cursorrules, .windsurfrules and
        AGENTS.md at the project root.
    Merge (documented): global + workspace combined; workspace rules take
    precedence on conflict. The relative order of the workspace formats
    (.clinerules/ vs .cursorrules vs .windsurfrules vs AGENTS.md) is
    undocumented — the order below is [inferred].
    Conditional rules (documented): a YAML frontmatter `paths` conditional
    scopes a rule to matching files; activation depends on the live editor
    context, so it is reported structurally and never predicted.
    Rule toggles are UI state in the Rules panel, not files — not modeled;
    a toggled-off rule still appears here. Multi-root workspaces: the
    .clinerules/ dir only works in the primary workspace folder.
    Strictly read-only.
    """
    res = new_resolution("cline")
    h = home()
    root = _cline_project_root(d)
    # 1. global scope — the documented baseline
    try:
        gnames = sorted(os.listdir(os.path.join(h, "Documents", "Cline",
                                                "Rules")))
    except OSError:
        gnames = []
    for n in gnames:
        if n.endswith(".md"):
            p = os.path.join(h, "Documents", "Cline", "Rules", n)
            if non_empty(p):
                add_file(res, p, "global",
                         "[documented] global rule "
                         "(~/Documents/Cline/Rules); loaded first, baseline")
    gp = os.path.join(h, ".agents", "AGENTS.md")
    if non_empty(gp):
        add_file(res, gp, "global",
                 "[documented] cross-tool global AGENTS.md "
                 "(~/.agents/AGENTS.md)")
    # 2. workspace scope — native dirs first, then auto-detected formats
    for rd in (os.path.join(root, ".clinerules"),
               os.path.join(root, ".cline", "rules")):
        try:
            names = sorted(os.listdir(rd))
        except OSError:
            continue
        for n in names:
            if n.endswith(".md") or n.endswith(".txt"):
                _cline_add_rule_file(res, os.path.join(rd, n))
    for name in (".cursorrules", ".windsurfrules"):
        p = os.path.join(root, name)
        if non_empty(p):
            add_file(res, p, "project-rule",
                     "[documented] %s auto-detected by Cline (cross-tool "
                     "compat)" % name)
    ap = os.path.join(root, "AGENTS.md")
    if non_empty(ap):
        add_file(res, ap, "project",
                 "[documented] AGENTS.md auto-detected by Cline "
                 "(cross-tool compat)")
    if root != os.path.abspath(d):
        res["notes"].append(
            "[inferred] DIR is a subdirectory; workspace rules were taken "
            "from the nearest ancestor holding rule markers (%s) — the "
            "docs define workspace rules at the project (workspace) root"
            % root)
    res["notes"].append(
        "[documented] merge: global + workspace rules are combined; "
        "workspace rules take precedence on conflict. The relative order "
        "of the workspace formats above is undocumented [inferred].")
    res["notes"].append(
        "[documented] conditional rules activate on live editor context "
        "(message paths, open/visible tabs, edited files) — dynamic, "
        "never predicted here.")
    res["notes"].append(
        "[documented] every rule has a toggle in the Rules panel (UI "
        "state, not files): agentscope assumes all discovered rules are "
        "enabled — a toggled-off rule still appears above.")
    res["notes"].append(
        "[documented] multi-root workspaces: .clinerules/ only works in "
        "the primary workspace folder; rules in other folders are "
        "ignored by the harness.")
    res["notes"].append(
        "[documented] Windows global rules dir is Documents\\Cline\\Rules; "
        "this resolver models the macOS/Linux/WSL location "
        "(~/Documents/Cline/Rules).")
    return res


INSTR_RESOLVERS = {"codex": resolve_codex, "claude": resolve_claude,
                   "cursor": resolve_cursor, "copilot": resolve_copilot,
                   "gemini": resolve_gemini, "opencode": resolve_opencode,
                   "antigravity": resolve_antigravity,
                   "cline": resolve_cline}


# --------------------------------------------------------------------------
# MCP server resolution (v1.1.0) — "which MCP servers does harness X
# actually load for directory Y?"
# --------------------------------------------------------------------------
# Same honesty model: a model of DOCUMENTED harness behavior. Config layouts
# verified against the Microsoft APM "Install MCP servers" per-harness table
# (2026-10-22). Secret-safety is STRUCTURAL, not documentary:
#   - env var VALUES and header values are NEVER printed (names only, <set>)
#   - URL query strings and fragments are stripped (they may carry tokens)
#   - --full is refused in --mcp mode
#   - remote (http/sse) servers are listed as REMOTE and never probed
# Strictly READ-ONLY: never writes, never executes anything, never touches
# the network.

import tomllib  # stdlib 3.11+; agentscope requires 3.11


def mask_url(url):
    """Strip query string and fragment from a URL (may carry tokens)."""
    if not isinstance(url, str):
        return ""
    return re.sub(r"[?#].*$", "", url)


def env_names(mapping):
    """{NAME: <set>} — names only; values never leave the config file."""
    if not isinstance(mapping, dict):
        return {}
    return {str(k): "<set>" for k in mapping}


def read_toml_obj(path):
    data = read_bytes(path)
    if data is None:
        return None
    try:
        obj = tomllib.loads(data.decode("utf-8", "replace"))
    except Exception:
        return None
    return obj if isinstance(obj, dict) else None


def new_mcp_resolution(harness):
    return {"harness": harness, "servers": [], "warnings": [], "notes": []}


def add_mcp_server(res, name, source, scope, transport, via, command="",
                   args=None, url="", env=None, headers=None, note=""):
    srv = {"name": str(name), "source": source, "scope": scope,
           "transport": transport,  # stdio | http | unknown
           "command": command or "",
           "args": [str(a) for a in args] if args else [],
           "url": mask_url(url) if url else "",
           "env": env_names(env), "headers": env_names(headers),
           "via": via, "note": note or ""}
    res["servers"].append(srv)
    return srv


def mcp_from_json_block(res, block, source, scope, via):
    """Parse a {name: {...}} server map (mcpServers or servers key)."""
    if block is None:
        return
    if not isinstance(block, dict):
        res["warnings"].append(
            "malformed server map in %s (expected an object); skipped" % source)
        return
    for name, entry in block.items():
        if not isinstance(entry, dict):
            res["warnings"].append(
                "server %r in %s is not an object; skipped" % (name, source))
            continue
        url = entry.get("url") or entry.get("serverUrl")
        command = entry.get("command")
        transport = "http" if url else ("stdio" if command else "unknown")
        srv = add_mcp_server(
            res, name, source, scope, transport, via, command=command,
            args=entry.get("args"), url=url, env=entry.get("env"),
            headers=entry.get("headers"))
        if transport == "unknown":
            res["warnings"].append(
                "server %r in %s has neither url nor command; "
                "listed as unknown" % (name, source))


def mcp_from_toml_block(res, block, source, scope, via):
    """Parse Codex [mcp_servers.*] tables. [documented] shape; url [inferred]."""
    if block is None:
        return
    if not isinstance(block, dict):
        res["warnings"].append(
            "malformed [mcp_servers] in %s (expected a table); skipped" % source)
        return
    for name, entry in block.items():
        if not isinstance(entry, dict):
            res["warnings"].append(
                "server %r in %s is not a table; skipped" % (name, source))
            continue
        url = entry.get("url")
        command = entry.get("command")
        transport = "http" if url else ("stdio" if command else "unknown")
        add_mcp_server(
            res, name, source, scope, transport, via, command=command,
            args=entry.get("args"), url=url, env=entry.get("env"))
        if transport == "unknown":
            res["warnings"].append(
                "server %r in %s has neither url nor command; "
                "listed as unknown" % (name, source))


def mcp_collisions(res, tag):
    """Same server name defined in >1 scope: surface it, don't silently pick."""
    seen = {}
    for s in res["servers"]:
        seen.setdefault(s["name"], []).append(s["scope"])
    for name, scopes in seen.items():
        uniq = sorted(set(scopes))
        if len(uniq) > 1:
            res["warnings"].append(
                "%s server %r is defined in multiple scopes (%s); the "
                "harness merges them at runtime" % (tag, name, ", ".join(uniq)))


def resolve_mcp_claude(d):
    res = new_mcp_resolution("claude")
    h = home()
    ad = os.path.abspath(d)
    # Project scope. [documented]
    p = os.path.join(ad, ".mcp.json")
    obj = read_json_obj(p)
    if obj is None and os.path.exists(p):
        res["warnings"].append("%s exists but is unreadable/not JSON" % p)
    else:
        mcp_from_json_block(res, obj.get("mcpServers") if obj else None, p,
                            "project", "[documented] .mcp.json = project scope")
        for s in res["servers"]:
            if s["scope"] == "project":
                s["note"] = ("[documented] project-scoped servers prompt for "
                             "user approval in interactive sessions")
    # User + local scopes from ~/.claude.json. [documented] locations.
    cfg_path = os.path.join(h, ".claude.json")
    cfg = read_json_obj(cfg_path)
    if cfg is None and os.path.exists(cfg_path):
        res["warnings"].append("%s exists but is unreadable/not JSON" % cfg_path)
    if cfg:
        mcp_from_json_block(res, cfg.get("mcpServers"), cfg_path, "user",
                            "[documented] ~/.claude.json mcpServers = user scope")
        projs = cfg.get("projects")
        matched = False
        if isinstance(projs, dict):
            for cand in (ad, git_root(ad)):
                if cand and cand in projs and isinstance(projs[cand], dict):
                    mcp_from_json_block(
                        res, projs[cand].get("mcpServers"),
                        "%s [projects %s]" % (cfg_path, cand), "local",
                        "[documented] per-project local scope lives in "
                        "~/.claude.json; [inferred] matched by exact "
                        "project-path key")
                    matched = True
                    break
        if not matched:
            res["notes"].append(
                "[inferred] no ~/.claude.json projects[] key matched this "
                "directory; local-scope servers (if any) are not shown")
    mcp_collisions(res, "[inferred]")
    return res


def resolve_mcp_codex(d):
    res = new_mcp_resolution("codex")
    h = home()
    ad = os.path.abspath(d)
    files = [(os.path.join(h, ".codex", "config.toml"), "global",
              "[documented] ~/.codex/config.toml = global scope")]
    proj_dir = os.path.join(ad, ".codex")
    if os.path.isdir(proj_dir):  # [documented] project config only then
        files.append((os.path.join(proj_dir, "config.toml"), "project",
                      "[documented] .codex/config.toml = project scope"))
    else:
        res["notes"].append(
            "[documented] no .codex/ dir here, so no project-scope config")
    for path, scope, via in files:
        obj = read_toml_obj(path)
        if obj is None:
            if os.path.exists(path):
                res["warnings"].append(
                    "%s exists but is unreadable/not TOML" % path)
            continue
        mcp_from_toml_block(res, obj.get("mcp_servers"), path, scope, via)
    mcp_collisions(res, "[inferred]")
    return res


def _resolve_mcp_jsonpair(res, d, proj_rel, glob_rel):
    """Shared pattern: project file (git root) + global file (HOME)."""
    h = home()
    root = git_root(os.path.abspath(d)) or os.path.abspath(d)
    proj_path, proj_scope, proj_via = proj_rel
    glob_path, glob_scope, glob_via = glob_rel
    pp = os.path.join(root, proj_path)
    gp = os.path.join(h, glob_path)
    for path, scope, via in ((pp, proj_scope, proj_via), (gp, glob_scope, glob_via)):
        obj = read_json_obj(path)
        if obj is None:
            if os.path.exists(path):
                res["warnings"].append(
                    "%s exists but is unreadable/not JSON" % path)
            continue
        block = obj.get("mcpServers")
        used_key = "mcpServers"
        if block is None and obj.get("servers") is not None:
            block = obj.get("servers")
            used_key = "servers"
        key_note = "" if used_key == "mcpServers" else (
            " ([inferred] accepted 'servers' key here)")
        mcp_from_json_block(res, block, path, scope, via + key_note)
    mcp_collisions(res, "[inferred]")


def resolve_mcp_cursor(d):
    res = new_mcp_resolution("cursor")
    _resolve_mcp_jsonpair(
        res, d,
        (os.path.join(".cursor", "mcp.json"), "project",
         "[documented] .cursor/mcp.json = project scope"),
        (os.path.join(".cursor", "mcp.json"), "global",
         "[documented] ~/.cursor/mcp.json = global scope"))
    return res


def resolve_mcp_copilot(d):
    res = new_mcp_resolution("copilot")
    h = home()
    ad = os.path.abspath(d)
    root = git_root(ad) or ad
    # Project scope. [documented] file; nearest-up-the-tree [inferred] (mirrors
    # the copilot-instructions discovery pattern).
    for base in (ad, root):
        p = os.path.join(base, ".github", "mcp.json")
        obj = read_json_obj(p)
        if obj is not None:
            tag = "project" if base == ad else "project [inferred: found at git root]"
            mcp_from_json_block(res, obj.get("mcpServers") or obj.get("servers"),
                                p, tag,
                                "[documented] .github/mcp.json = project scope")
            break
    else:
        p = os.path.join(ad, ".github", "mcp.json")
        if os.path.exists(p):
            res["warnings"].append("%s exists but is unreadable/not JSON" % p)
    # Global scope. [documented] locations.
    cph = os.environ.get("COPILOT_HOME", "")
    gp = os.path.join(cph, "mcp-config.json") if cph else None
    if not gp or not os.path.exists(gp):
        gp = os.path.join(h, ".copilot", "mcp-config.json")
    obj = read_json_obj(gp)
    if obj is None:
        if os.path.exists(gp):
            res["warnings"].append("%s exists but is unreadable/not JSON" % gp)
    else:
        mcp_from_json_block(res, obj.get("mcpServers") or obj.get("servers"),
                            gp, "global",
                            "[documented] mcp-config.json = global scope")
    mcp_collisions(res, "[inferred]")
    return res


def resolve_mcp_gemini(d):
    res = new_mcp_resolution("gemini")
    _resolve_mcp_jsonpair(
        res, d,
        (os.path.join(".gemini", "settings.json"), "project",
         "[documented] .gemini/settings.json mcpServers = project scope"),
        (os.path.join(".gemini", "settings.json"), "user",
         "[documented] ~/.gemini/settings.json mcpServers = user scope"))
    return res


def _opencode_configs(d):
    """(path, scope) for the project opencode.json (nearest upward search)
    and the global one. Scopes [documented]; the upward project-config
    search is [inferred]."""
    out = []
    for level in upward(d):
        cand = os.path.join(level, "opencode.json")
        if os.path.isfile(cand):
            out.append((cand, "project"))
            break
    gp = os.path.join(home(), ".config", "opencode", "opencode.json")
    if os.path.isfile(gp):
        out.append((gp, "global"))
    return out


def resolve_mcp_opencode(d):
    """OpenCode MCP servers from opencode.json (v2 layout: `mcp.servers`).

    Verified 2026-10-22 against opencode.ai/v2/docs/mcp-servers: local
    servers (command/cwd/environment/disabled), remote servers (url,
    headers, oauth, disabled), project config REPLACES the entire server
    object with the same name from the global config [all documented].
    Secret safety is structural: env/header VALUES are never printed
    (names + <set>), URL query/fragment is stripped, remote servers are
    never probed. Strictly read-only.
    """
    res = new_mcp_resolution("opencode")
    origins = {}  # name -> (scope, source) for override notes
    for cfg_path, scope in _opencode_configs(d):
        cfg = read_json_obj(cfg_path)
        if not cfg:
            continue
        mcp = cfg.get("mcp")
        if not isinstance(mcp, dict):
            continue
        servers = mcp.get("servers")
        if servers is None:
            legacy = {k: v for k, v in mcp.items()
                      if isinstance(v, dict)
                      and ("type" in v or "command" in v or "url" in v)}
            if legacy:
                res["warnings"].append(
                    "[documented] pre-v2 `mcp.<name>` layout (server names "
                    "directly under `mcp`) found in %s — v2 groups servers "
                    "under `mcp.servers`; legacy entries not modeled: %s"
                    % (cfg_path, ", ".join(sorted(legacy))))
            continue
        if not isinstance(servers, dict):
            res["warnings"].append(
                "`mcp.servers` is not an object in %s; skipped" % cfg_path)
            continue
        for name, entry in servers.items():
            if not isinstance(entry, dict):
                res["warnings"].append(
                    "server %r is not an object in %s; skipped"
                    % (name, cfg_path))
                continue
            stype = entry.get("type")
            transport = {"local": "stdio", "remote": "http"}.get(stype,
                                                               "unknown")
            dis = entry.get("disabled") is True
            dnote = ("[documented] disabled: true — configured but not "
                     "connected") if dis else ""
            if stype == "local":
                cmd = entry.get("command") or []
                cmd = [str(c) for c in cmd] if isinstance(cmd, list) else []
                add_mcp_server(res, name, cfg_path, scope, transport,
                               "[documented] opencode.json `mcp.servers` "
                               "(v2 layout)",
                               command=cmd[0] if cmd else "",
                               args=cmd[1:] if len(cmd) > 1 else [],
                               env=entry.get("environment"), note=dnote)
            elif stype == "remote":
                add_mcp_server(
                    res, name, cfg_path, scope, transport,
                    "[documented] opencode.json `mcp.servers` (v2 layout)",
                    url=entry.get("url") or "",
                    headers=entry.get("headers"),
                    note=((dnote + "; ") if dnote else "") +
                         "[documented] OAuth enabled by default unless "
                         "`oauth: false`; remote servers are listed as "
                         "REMOTE and never probed")
            else:
                res["warnings"].append(
                    "server %r: unknown/absent `type` %r in %s; skipped"
                    % (name, stype, cfg_path))
                continue
            if name in origins and origins[name] != (scope, cfg_path):
                res["notes"].append(
                    "[documented] server %r defined in %s and %s: the "
                    "higher-precedence project config replaces the entire "
                    "server object" % (name, origins[name][1], cfg_path))
            origins[name] = (scope, cfg_path)
    if not _opencode_configs(d):
        res["notes"].append(
            "[inferred] no opencode.json found (project or global); "
            "nothing to resolve")
    return res


def resolve_mcp_cline(d):
    res = new_mcp_resolution("cline")
    res["notes"].append(
        "[inferred] Cline's MCP servers are configured in the extension "
        "settings (cline_mcp_settings.json in VS Code globalStorage), not "
        "per-directory — no per-directory effective set to resolve")
    return res


MCP_RESOLVERS = {"codex": resolve_mcp_codex, "claude": resolve_mcp_claude,
                 "cursor": resolve_mcp_cursor, "copilot": resolve_mcp_copilot,
                 "gemini": resolve_mcp_gemini, "opencode": resolve_mcp_opencode,
                 "antigravity": resolve_mcp_antigravity,
                 "cline": resolve_mcp_cline}
RESOLVERS = {"instructions": INSTR_RESOLVERS, "mcp": MCP_RESOLVERS}


def mcp_server_lines(s):
    lines = ["  - %s  [%s scope, %s]" % (s["name"], s["scope"], s["source"])]
    if s["transport"] == "http":
        lines.append("    transport: http (REMOTE — never probed), url: %s" % s["url"])
    elif s["transport"] == "stdio":
        cmd = s["command"] + (" " + " ".join(s["args"]) if s["args"] else "")
        lines.append("    transport: stdio, command: %s" % cmd.strip())
    else:
        lines.append("    transport: unknown")
    if s["env"]:
        lines.append("    env: %s" % ", ".join(
            "%s=<set>" % k for k in sorted(s["env"])))
    if s["headers"]:
        lines.append("    headers: %s" % ", ".join(
            "%s=<set>" % k for k in sorted(s["headers"])))
    lines.append("    via: %s" % s["via"])
    if s["note"]:
        lines.append("    note: %s" % s["note"])
    return lines


def render_mcp_text(results, outline=False):
    lines = [BANNER, "",
             "SECRET SAFETY: env/header VALUES and URL query strings are never "
             "printed — only names and masked URLs.", ""]
    for r in results:
        lines.append("== %s (MCP servers) ==" % r["harness"])
        if outline:
            for s in r["servers"]:
                lines.append("  %s  [%s]" % (s["name"], s["scope"]))
        elif not r["servers"]:
            lines.append("  (no MCP servers resolve for this directory)")
        else:
            for s in r["servers"]:
                lines.extend(mcp_server_lines(s))
        for w in r["warnings"]:
            lines.append("  WARNING: %s" % w)
        for n in r["notes"]:
            lines.append("  note: %s" % n)
        lines.append("")
    return "\n".join(lines).rstrip("\n") + "\n"


def render_mcp_json(results, directory):
    return json.dumps({
        "tool": "agentscope", "version": VERSION, "mode": "mcp",
        "rules_verified": RULES_VERIFIED,
        "directory": os.path.abspath(directory),
        "disclaimer": "model of documented harness behavior, not runtime "
                      "truth; secret values never included",
        "harnesses": [{"harness": r["harness"], "servers": r["servers"],
                       "warnings": r["warnings"], "notes": r["notes"]}
                      for r in results],
    }, indent=2) + "\n"

def hook_lines(hk):
    line = "  - %s  [matcher %r, %s scope, %s]" % (
        hk["event"], hk["matcher"], hk["scope"], hk["source"])
    out = [line, "    command: %s" % hk["command"]]
    if hk["timeout"] is not None:
        out.append("    timeout: %s" % hk["timeout"])
    out.append("    via: %s" % hk["via"])
    if hk["note"]:
        out.append("    note: %s" % hk["note"])
    return out


def render_hooks_text(results, outline=False):
    lines = [BANNER, "",
             "NEVER EXECUTED: agentscope only parses hook config; it never "
             "runs hook commands.", ""]
    for r in results:
        lines.append("== %s (hooks) ==" % r["harness"])
        if outline:
            for hk in r["hooks"]:
                lines.append("  %s  [%r, %s]" % (
                    hk["event"], hk["matcher"], hk["scope"]))
        elif not r["hooks"]:
            lines.append("  (no hooks resolve for this directory)")
        else:
            for hk in r["hooks"]:
                lines.extend(hook_lines(hk))
        for w in r["warnings"]:
            lines.append("  WARNING: %s" % w)
        for n in r["notes"]:
            lines.append("  note: %s" % n)
        lines.append("")
    return "\n".join(lines).rstrip("\n") + "\n"


def render_hooks_json(results, directory):
    return json.dumps({
        "tool": "agentscope", "version": VERSION, "mode": "hooks",
        "rules_verified": RULES_VERIFIED,
        "directory": os.path.abspath(directory),
        "disclaimer": "model of documented harness behavior, not runtime "
                      "truth; hook commands are never executed",
        "harnesses": [{"harness": r["harness"],
                       "hooks": [dict(h) for h in r["hooks"]],
                       "warnings": r["warnings"], "notes": r["notes"]}
                      for r in results],
    }, indent=2) + "\n"


# --------------------------------------------------------------------------
# Hook resolution (v1.2.0) — "which hooks does harness X actually fire for
# directory Y?"
# --------------------------------------------------------------------------
# Same honesty model: a model of DOCUMENTED harness behavior, not runtime
# truth. Hook-config layouts verified via web search (2026-10-22).
#
# EXECUTION SAFETY IS STRUCTURAL, not documentary:
#   - hooks are arbitrary shell commands; agentscope NEVER executes anything.
#     --hooks mode is pure config parsing. Hook command text is shown verbatim
#     for the operator to inspect, but it is never passed to a shell, a
#     subprocess, or any other execution path.
#   - hook commands may embed credentials (e.g. Authorization headers); the
#     operator reading this output owns the machine and the config. See
#     README Limitations.
# Every harness rule is tagged [documented]/[inferred] with the
# RULES_VERIFIED date; output is bannered as a model, never runtime truth.
# Strictly READ-ONLY: never writes, never executes, never touches the network.

CLAUDE_HOOK_EVENTS = (  # [documented] Claude Code event names
    "PreToolUse", "PostToolUse", "PostToolUseFailure", "PostToolBatch",
    "PermissionRequest", "UserPromptSubmit", "UserPromptExpansion",
    "SessionStart", "SessionEnd", "Stop", "SubagentStop", "ConfigChange",
    "CwdChanged", "FileChanged", "TaskCreated", "TaskCompleted",
    "Notification", "PreCompact",
)


def new_hook_resolution(harness):
    return {"harness": harness, "hooks": [], "warnings": [], "notes": []}


def add_hook(res, event, matcher, source, scope, command, via,
             timeout=None, note=""):
    hk = {"event": str(event), "matcher": matcher or "",
          "source": source, "scope": scope,
          "command": command or "", "timeout": timeout,
          "via": via, "note": note or ""}
    res["hooks"].append(hk)
    return hk


def hooks_from_claude_shape(res, obj, source, scope, via):
    """Parse the Claude-style hooks map:

    {"hooks": {Event: [{"matcher": "...",
                        "hooks": [{"type": "command", "command": "...",
                                   "timeout": N}]}]}}

    [documented] shape for claude; [documented] as the shape Codex and
    Cursor borrow (matcher-name mapping differs per harness, noted by
    callers). Entries without a command string are surfaced as warnings,
    not silently dropped.
    """
    if obj is None:
        return
    if not isinstance(obj, dict):
        res["warnings"].append(
            "%s is not a JSON object; skipped" % source)
        return
    block = obj.get("hooks")
    if block is None:
        return
    if not isinstance(block, dict):
        res["warnings"].append(
            "%s 'hooks' is not an object; skipped" % source)
        return
    for event, groups in block.items():
        if not isinstance(groups, list):
            res["warnings"].append(
                "%s event %r is not a list; skipped" % (source, event))
            continue
        for grp in groups:
            if not isinstance(grp, dict):
                res["warnings"].append(
                    "%s event %r has a non-object matcher group; skipped"
                    % (source, event))
                continue
            matcher = grp.get("matcher", "")
            entries = grp.get("hooks")
            if not isinstance(entries, list):
                res["warnings"].append(
                    "%s event %r matcher %r has no 'hooks' list; skipped"
                    % (source, event, matcher))
                continue
            for entry in entries:
                if not isinstance(entry, dict):
                    res["warnings"].append(
                        "%s event %r has a non-object hook entry; skipped"
                        % (source, event))
                    continue
                cmd = entry.get("command")
                if not cmd:
                    res["warnings"].append(
                        "%s event %r matcher %r: hook entry has no "
                        "'command' (type=%r); skipped" %
                        (source, event, matcher, entry.get("type")))
                    continue
                add_hook(res, event, matcher, source, scope, cmd, via,
                         timeout=entry.get("timeout"))


def hooks_from_copilot_shape(res, obj, source, scope, via,
                             expect_version=True):
    """Parse Copilot hook files: {"version": 1, "hooks": {event: [...]}}.
    [documented] per the official GitHub Copilot hooks reference
    (docs.github.com/en/copilot/reference/hooks-reference, verified
    2026-10-22). Command key may be "bash", "command", or "exec"+args
    (direct exec, no shell); timeout key "timeoutSec" ("timeout" is an
    alias, timeoutSec wins); disableAllHooks skips the file. expect_version
    is False for inline "hooks" blocks in settings.json, which carry no
    version field."""
    if obj is None:
        return
    if not isinstance(obj, dict):
        res["warnings"].append("%s is not a JSON object; skipped" % source)
        return
    if obj.get("disableAllHooks") is True:
        res["notes"].append(
            "[documented] %s sets disableAllHooks=true — the CLI skips the "
            "hooks declared in this file" % source)
        return
    if expect_version and obj.get("version") != 1:
        res["warnings"].append(
            "%s: missing or unexpected 'version' (expected 1); parsed "
            "anyway" % source)
    block = obj.get("hooks")
    if block is None:
        return
    if not isinstance(block, dict):
        res["warnings"].append("%s 'hooks' is not an object; skipped" % source)
        return
    for event, entries in block.items():
        if not isinstance(entries, list):
            res["warnings"].append(
                "%s event %r is not a list; skipped" % (source, event))
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                res["warnings"].append(
                    "%s event %r has a non-object hook entry; skipped"
                    % (source, event))
                continue
            etype = entry.get("type", "command")
            if etype != "command":
                res["warnings"].append(
                    "%s event %r: hook type %r is not a shell command "
                    "(agentscope resolves shell hooks only); skipped "
                    "[documented]" % (source, event, etype))
                continue
            if entry.get("exec"):
                cmd = " ".join([str(entry["exec"])] +
                               [str(a) for a in entry.get("args", [])])
                note = ("direct exec, no shell interpretation "
                        "[documented]")
            else:
                cmd = (entry.get("bash") or entry.get("command"))
                note = ""
                if entry.get("powershell") and not cmd:
                    res["warnings"].append(
                        "%s event %r: powershell-only entry is ignored on "
                        "this host; skipped [documented]" % (source, event))
                    continue
            if not cmd:
                res["warnings"].append(
                    "%s event %r: hook entry has no 'bash'/'command'/'exec'; "
                    "skipped" % (source, event))
                continue
            add_hook(res, event, entry.get("matcher", ""), source, scope,
                     cmd, via, timeout=(entry.get("timeoutSec") if
                                       entry.get("timeoutSec") is not None
                                       else entry.get("timeout")),
                     note=note)


def hook_files_from_dir(res, dirpath, scope, via, parser, glob="*.json"):
    """Read every JSON file in a hooks directory. [inferred] ordering."""
    if not os.path.isdir(dirpath):
        return
    for name in sorted(os.listdir(dirpath)):
        if not name.endswith(".json"):
            continue
        p = os.path.join(dirpath, name)
        obj = read_json_obj(p)
        if obj is None:
            res["warnings"].append("%s exists but is unreadable/not JSON" % p)
            continue
        parser(res, obj, p, scope, via)


def resolve_hooks_claude(d):
    res = new_hook_resolution("claude")
    h = home()
    ad = os.path.abspath(d)
    files = [
        (os.path.join(h, ".claude", "settings.json"), "user",
         "[documented] ~/.claude/settings.json = user scope"),
        (os.path.join(ad, ".claude", "settings.json"), "project",
         "[documented] .claude/settings.json = project scope"),
        (os.path.join(ad, ".claude", "settings.local.json"), "local",
         "[documented] .claude/settings.local.json = personal (gitignored) "
         "scope"),
    ]
    for path, scope, via in files:
        obj = read_json_obj(path)
        if obj is None:
            if os.path.exists(path):
                res["warnings"].append(
                    "%s exists but is unreadable/not JSON" % path)
            continue
        hooks_from_claude_shape(res, obj, path, scope, via)
    for hk in res["hooks"]:
        if hk["event"] not in CLAUDE_HOOK_EVENTS:
            res["warnings"].append(
                "[inferred] event %r in %s is not a documented Claude Code "
                "event; the harness may ignore it" % (hk["event"],
                                                      hk["source"]))
    res["notes"].append(
        "[documented] hooks from all scopes fire; hooks load at session "
        "start (config changes need a restart); matchers are regexes "
        "matched against the tool name")
    res["notes"].append(
        "[inferred] plugin-managed hooks (.claude/plugins/) are not "
        "enumerated here")
    return res


def resolve_hooks_codex(d):
    res = new_hook_resolution("codex")
    h = home()
    ad = os.path.abspath(d)
    files = [
        (os.path.join(h, ".codex", "hooks.json"), "global",
         "[documented] ~/.codex/hooks.json = global scope"),
    ]
    proj_dir = os.path.join(ad, ".codex")
    if os.path.isdir(proj_dir):
        files.append((os.path.join(proj_dir, "hooks.json"), "project",
                      "[documented] .codex/hooks.json = project scope"))
        files.append((os.path.join(proj_dir, "config.toml"), "project",
                      "[documented] [hooks] in .codex/config.toml = project "
                      "scope"))
    else:
        res["notes"].append(
            "[documented] no .codex/ dir here, so no project-scope hooks")
    for path, scope, via in files:
        if path.endswith(".toml"):
            obj = read_toml_obj(path)
            if obj is not None:
                obj = {"hooks": obj.get("hooks")}
            hook_via = via
        else:
            obj = read_json_obj(path)
            hook_via = via
        if obj is None:
            if os.path.exists(path):
                res["warnings"].append(
                    "%s exists but is unreadable" % path)
            continue
        hooks_from_claude_shape(res, obj, path, scope,
                                hook_via + "; [inferred] Codex borrows the "
                                "Claude hook shape")
    if any(hk["scope"] == "project" for hk in res["hooks"]):
        res["notes"].append(
            "[documented] Codex loads project hooks only in trusted "
            "projects; a changed hook is skipped until re-trusted")
    res["notes"].append(
        "[inferred] blocking = exit 2 or permissionDecision deny; matching "
        "hooks start at once, so one cannot stop another; tool-name mapping "
        "differs from Claude (e.g. Edit|Write matchers may need "
        "apply_patch)")
    return res


CURSOR_HOOK_NAME_MAP = {  # [documented] Cursor's tool-name mapping
    "Bash": "Shell", "Edit": "Write", "UserPromptSubmit": "beforeSubmitPrompt",
}


def resolve_hooks_cursor(d):
    res = new_hook_resolution("cursor")
    h = home()
    ad = os.path.abspath(d)
    files = [
        (os.path.join(h, ".cursor", "hooks.json"), "global",
         "[documented] ~/.cursor/hooks.json = global scope"),
        (os.path.join(ad, ".cursor", "hooks.json"), "project",
         "[documented] .cursor/hooks.json = project scope"),
    ]
    for path, scope, via in files:
        obj = read_json_obj(path)
        if obj is None:
            if os.path.exists(path):
                res["warnings"].append(
                    "%s exists but is unreadable/not JSON" % path)
            continue
        hooks_from_claude_shape(res, obj, path, scope, via)
    # Cursor also reads Claude's hook configs by default. [documented]
    claude_files = [
        (os.path.join(h, ".claude", "settings.json"), "user (via claude)",
         "[documented] Cursor reads Claude's settings by default"),
        (os.path.join(ad, ".claude", "settings.json"), "project (via claude)",
         "[documented] Cursor reads Claude's settings by default"),
        (os.path.join(ad, ".claude", "settings.local.json"),
         "local (via claude)",
         "[documented] Cursor reads Claude's settings by default"),
    ]
    for path, scope, via in claude_files:
        obj = read_json_obj(path)
        if obj is None:
            continue
        before = len(res["hooks"])
        hooks_from_claude_shape(res, obj, path, scope, via)
        for hk in res["hooks"][before:]:
            hk["note"] = ("[documented] Cursor maps Claude tool names: "
                          + ", ".join("%s->%s" % kv
                                      for kv in sorted(
                                          CURSOR_HOOK_NAME_MAP.items())))
    if res["hooks"]:
        res["notes"].append(
            "[documented] Cursor does NOT map Notification or "
            "PermissionRequest events; it fails open when a hook crashes, "
            "so guard scripts should exit 2 on unparseable input")
    return res


def hooks_from_copilot_settings(res, path, scope, via):
    """Read an inline "hooks" block from a Copilot settings.json file.
    [documented] the CLI reads top-level "hooks" in repo
    .github/copilot/settings.json and settings.local.json, in
    .claude/settings.json and settings.local.json (cross-tool read), and
    in user ~/.copilot/settings.json. disableAllHooks at this level skips
    every hook from every source for sessions here."""
    if not os.path.isfile(path):
        return
    try:
        with open(path, "r", encoding="utf-8") as f:
            settings = json.load(f)
    except (OSError, ValueError) as e:
        res["warnings"].append("%s is not valid JSON (%s); skipped"
                              % (path, e))
        return
    if not isinstance(settings, dict):
        res["warnings"].append("%s is not a JSON object; skipped" % path)
        return
    if settings.get("disableAllHooks") is True:
        res["notes"].append(
            "[documented] %s sets disableAllHooks=true — the CLI skips "
            "every hook from every source for sessions here (policy "
            "hooks are not affected)" % path)
        return
    hooks_from_copilot_shape(res, {"hooks": settings.get("hooks")}, path,
                             scope, via, expect_version=False)


def resolve_hooks_copilot(d):
    res = new_hook_resolution("copilot")
    h = home()
    ad = os.path.abspath(d)
    root = git_root(ad) or ad
    via_repo = ("[documented] .github/hooks/*.json = repo scope; [inferred] "
                "nearest-up-the-tree discovery mirrors the copilot-"
                "instructions pattern")
    found = False
    for base in (ad, root):
        dirpath = os.path.join(base, ".github", "hooks")
        if os.path.isdir(dirpath):
            hook_files_from_dir(res, dirpath, "repo", via_repo,
                                hooks_from_copilot_shape)
            found = True
            break
    if not found:
        res["notes"].append(
            "[documented] no .github/hooks/ dir here, so no repo-scope hooks")
    # inline "hooks" blocks in settings files [documented]
    copilot_home = os.environ.get("COPILOT_HOME")
    user_hooks_dir = (os.path.join(copilot_home, "hooks")
                      if copilot_home else os.path.join(h, ".copilot", "hooks"))
    via_inline = ("[documented] inline 'hooks' block in %s; event entries "
                  "use the same shape as hook files (no version field)")
    for rel in (os.path.join(".github", "copilot", "settings.json"),
                os.path.join(".github", "copilot", "settings.local.json")):
        hooks_from_copilot_settings(res, os.path.join(root, rel), "repo",
                                    via_inline % rel)
    for rel in (os.path.join(".claude", "settings.json"),
                os.path.join(".claude", "settings.local.json")):
        hooks_from_copilot_settings(res, os.path.join(root, rel),
                                    "repo-inline",
                                    "[documented] the CLI cross-reads "
                                    ".claude/settings*.json for inline "
                                    "'hooks' blocks, same as its own "
                                    "settings.json")
    hooks_from_copilot_settings(res, os.path.join(h, ".copilot",
                                                 "settings.json"),
                                "user-inline",
                                via_inline % "~/.copilot/settings.json")
    hook_files_from_dir(
        res, user_hooks_dir, "user",
        "[documented] user hooks dir (%s)" %
        ("$COPILOT_HOME/hooks when COPILOT_HOME is set, else "
         "~/.copilot/hooks/*.json = user scope"),
        hooks_from_copilot_shape)
    res["notes"].append(
        "[documented] hook sources merge in order policy > user > repo > "
        "plugin; when the same event appears in multiple sources, ALL "
        "entries run. Policy hooks are machine-wide and not enumerable "
        "from here — Linux/macOS: /etc/github-copilot/policy.d/*.json "
        "(root-owned, not group/world-writable), Windows: "
        "HKLM\\Software\\Policies\\GitHub\\Copilot. Plugin-declared hooks "
        "(each plugin's own hooks.json) and custom-agent frontmatter "
        "scoped hooks are not enumerated here; end users cannot disable "
        "policy hooks")
    return res


def resolve_hooks_opencode(d):
    res = new_hook_resolution("opencode")
    res["notes"].append(
        "[inferred] OpenCode hooks are not modeled in this version; "
        "instructions resolution only")
    return res


def resolve_hooks_gemini(d):
    res = new_hook_resolution("gemini")
    res["notes"].append(
        "[documented] the Gemini CLI HAS a documented hook mechanism (11 "
        "events; geminicli.com/docs/hooks/, verified 2026-10-22), but "
        "agentscope --hooks does not model it yet — see hooktest "
        "--harness gemini for a synthetic-payload tester instead")
    return res


def resolve_hooks_cline(d):
    res = new_hook_resolution("cline")
    res["notes"].append(
        "[inferred] no documented shell-hook mechanism found for Cline — "
        "not modeled")
    return res


HOOK_RESOLVERS = {"codex": resolve_hooks_codex, "claude": resolve_hooks_claude,
                  "cursor": resolve_hooks_cursor,
                  "copilot": resolve_hooks_copilot,
                  "gemini": resolve_hooks_gemini,
                  "opencode": resolve_hooks_opencode,
                  "antigravity": resolve_hooks_antigravity,
                  "cline": resolve_hooks_cline}
RESOLVERS["hooks"] = HOOK_RESOLVERS

# --------------------------------------------------------------------------
# permissions mode (--permissions, v1.3.0)
# --------------------------------------------------------------------------
# Which allow/deny/ask rules a harness actually enforces for a directory.
# Pure config parsing: rule text is shown verbatim but NEVER executed or
# evaluated against real tool calls — this is the merged rule set on disk,
# not the harness's runtime decision. Same honesty model as the other modes.


def new_perm_resolution(harness):
    return {"harness": harness, "rules": [], "context": [], "warnings": [],
            "notes": []}


def add_perm_rule(res, kind, rule, source, scope, via, tag=DOC, note=""):
    res["rules"].append({"kind": kind, "rule": str(rule), "source": source,
                         "scope": scope, "via": via, "tag": tag,
                         "note": note})


def add_perm_context(res, name, value, source, scope, via, tag=DOC, note=""):
    res["context"].append({"name": name, "value": str(value), "source": source,
                           "scope": scope, "via": via, "tag": tag,
                           "note": note})


def _perm_claude_scope(res, path, scope, via):
    obj = read_json_obj(path)
    if obj is None:
        return
    perms = obj.get("permissions")
    if not isinstance(perms, dict):
        return
    for kind in ("deny", "ask", "allow"):
        entries = perms.get(kind)
        if not isinstance(entries, list):
            continue
        for e in entries:
            add_perm_rule(res, kind, e, path, scope, via)


def resolve_permissions_claude(d):
    res = new_perm_resolution("claude")
    root = git_root(d) or d
    _perm_claude_scope(
        res, os.path.join(home(), ".claude", "settings.json"), "user",
        "[documented] user settings merge with project and local scopes")
    _perm_claude_scope(
        res, os.path.join(root, ".claude", "settings.json"), "project",
        "[documented] project settings apply inside this repo")
    _perm_claude_scope(
        res, os.path.join(root, ".claude", "settings.local.json"), "local",
        "[documented] local settings (gitignored) apply inside this repo")
    res["notes"].append(
        "[documented] evaluation order: deny rules first, then ask, then "
        "allow — the FIRST match decides; specificity and file origin do "
        "not change the outcome (verified 2026-10-22)")
    return res


def _toml_compact(value):
    if isinstance(value, dict):
        return "{" + ", ".join("%s=%s" % (k, _toml_compact(v))
                               for k, v in value.items()) + "}"
    if isinstance(value, list):
        return "[" + ", ".join(_toml_compact(v) for v in value) + "]"
    return str(value)


def _perm_codex_scope(res, path, scope, via):
    obj = read_toml_obj(path)
    if obj is None:
        return
    default = obj.get("default_permissions")
    if isinstance(default, str):
        add_perm_context(res, "default_permissions", default, path, scope,
                         via, note="selects the active permission profile")
    sandbox = obj.get("sandbox_mode")
    if sandbox is None:
        sandbox = obj.get("sandbox")  # [inferred] older key
    if isinstance(sandbox, str):
        add_perm_context(res, "sandbox_mode", sandbox, path, scope, via)
    approval = obj.get("approval_policy")
    if approval is None:
        approval = obj.get("ask_for_approval")  # [inferred] older key
    if approval is not None:
        add_perm_context(res, "approval_policy", _toml_compact(approval),
                         path, scope, via,
                         tag=DOC if isinstance(approval, str) else INF,
                         note="[inferred] granular table shown verbatim; "
                         "merge semantics undocumented" if isinstance(
                             approval, dict) else "")
    profiles = obj.get("permissions")
    if not isinstance(profiles, dict):
        return
    for pname, pbody in profiles.items():
        if not isinstance(pbody, dict):
            res["warnings"].append(
                "profile %r in %s is not a table; skipped" % (pname, path))
            continue
        fs = pbody.get("filesystem")
        if not isinstance(fs, dict):
            continue
        # [documented] shape: [permissions.<name>.filesystem] with
        # "<token-or-path>" = "<level>" or a sub-table of pattern = level
        for pat, level in fs.items():
            if isinstance(level, dict):
                for sub, lvl in level.items():
                    add_perm_rule(res, "fs", "%s %s => %s" % (pat, sub, lvl),
                                  path, "profile:%s" % pname, via,
                                  note="filesystem access level")
            else:
                add_perm_rule(res, "fs", "%s => %s" % (pat, level),
                              path, "profile:%s" % pname, via,
                              note="filesystem access level")


def resolve_permissions_codex(d):
    res = new_perm_resolution("codex")
    root = git_root(d) or d
    _perm_codex_scope(
        res, os.path.join(home(), ".codex", "config.toml"), "global",
        "[documented] global config applies to all projects")
    _perm_codex_scope(
        res, os.path.join(root, ".codex", "config.toml"), "project",
        "[documented] project config overrides global, but ONLY loads in "
        "trusted projects [inferred] untrusted projects see global only")
    res["notes"].append(
        "[documented] resolution order: CLI flags > active profile > "
        "config.toml defaults > built-in defaults; --flags are not visible "
        "to a static resolver (verified 2026-10-22)")
    return res


def _perm_gemini_scope(res, path, scope, via):
    obj = read_json_obj(path)
    if obj is None:
        return
    tools = obj.get("tools")
    core = None
    if isinstance(tools, dict):
        core = tools.get("core")  # [documented] enterprise shape
        sandbox = tools.get("sandbox")
        if sandbox is not None:
            add_perm_context(res, "tools.sandbox", sandbox, path, scope, via)
    if core is None:
        core = obj.get("coreTools")  # [documented] older/CI shape
    if isinstance(core, list):
        for e in core:
            add_perm_rule(res, "allow", e, path, scope, via)


def resolve_permissions_gemini(d):
    res = new_perm_resolution("gemini")
    root = git_root(d) or d
    _perm_gemini_scope(
        res, os.path.join(home(), ".gemini", "settings.json"), "global",
        "[documented] global settings")
    _perm_gemini_scope(
        res, os.path.join(root, ".gemini", "settings.json"), "project",
        "[documented] project settings")
    res["notes"].append(
        "[documented] hierarchy: env vars > project > global > defaults; env "
        "overrides are not visible to a static resolver (verified "
        "2026-10-22)")
    return res


def resolve_permissions_cursor(d):
    res = new_perm_resolution("cursor")
    res["notes"].append(
        "[inferred] no documented static allow/deny rule format found for "
        "Cursor (verified 2026-10-22); nothing to resolve")
    return res


def resolve_permissions_copilot(d):
    res = new_perm_resolution("copilot")
    res["notes"].append(
        "[inferred] no documented static allow/deny rule format found for "
        "GitHub Copilot CLI (verified 2026-10-22); nothing to resolve")
    return res


def resolve_permissions_opencode(d):
    """OpenCode permission rules from the top-level `permissions` array in
    opencode.json: [{action, resource, effect}].

    Verified 2026-10-22 against opencode.ai/v2/docs/mcp-servers: `action`
    matches the normalized `<server>_<tool>` name (or built-in actions
    like opencode_read_mcp_resource); `resource` is a pattern; `effect` is
    allow/deny in the documented examples. Rule evaluation ORDER across
    the array is not documented, so no merge order is assumed
    [inferred]. Rules shown verbatim; strictly read-only.
    """
    res = new_perm_resolution("opencode")
    cfgs = _opencode_configs(d)
    for cfg_path, scope in cfgs:
        cfg = read_json_obj(cfg_path)
        if not cfg:
            continue
        perms = cfg.get("permissions")
        if perms is None:
            continue
        if not isinstance(perms, list):
            res["warnings"].append(
                "`permissions` is not a list in %s; skipped" % cfg_path)
            continue
        for p in perms:
            if not isinstance(p, dict):
                res["warnings"].append(
                    "non-object `permissions` entry in %s; skipped"
                    % cfg_path)
                continue
            rule = ("action=%s resource=%s effect=%s"
                    % (p.get("action", ""), p.get("resource", ""),
                       p.get("effect", "")))
            add_perm_rule(
                res, "opencode", rule, cfg_path, scope,
                "[documented] opencode.json `permissions` array "
                "(opencode.ai/v2/docs/mcp-servers)",
                note="[documented] fields action/resource/effect; "
                     "[inferred] evaluation order across the array is "
                     "undocumented — no merge order assumed; rules are "
                     "shown verbatim")
    if not res["rules"]:
        res["notes"].append(
            "[inferred] no `permissions` array found in %s"
            % (", ".join(p for p, _ in cfgs) or "any opencode.json"))
    return res


def resolve_permissions_cline(d):
    res = new_perm_resolution("cline")
    res["notes"].append(
        "[inferred] no documented static allow/deny rule format found for "
        "Cline — not modeled")
    return res


PERM_RESOLVERS = {"codex": resolve_permissions_codex,
                  "claude": resolve_permissions_claude,
                  "cursor": resolve_permissions_cursor,
                  "copilot": resolve_permissions_copilot,
                  "gemini": resolve_permissions_gemini,
                  "opencode": resolve_permissions_opencode,
                  "antigravity": resolve_permissions_antigravity,
                  "cline": resolve_permissions_cline}
RESOLVERS["permissions"] = PERM_RESOLVERS


# --------------------------------------------------------------------------
# skills mode (--skills, v1.7.0)
# --------------------------------------------------------------------------
# "Which skills will harness X actually SEE for directory Y?"
#
# Discovery is on-disk resolution only: which <name>/SKILL.md folders exist
# in each harness's documented skill locations for the target directory.
# Loading is NOT modeled: harnesses load skill bodies on-demand by
# description match (progressive disclosure — name+description listed at
# startup, full SKILL.md read on match), so "visible" != "will be used".
# The Oct 22 rejection of this angle ("an 'effective set' would overstate
# loading semantics") is kept visible in the README Limitations note and
# in every text report. Per-harness locations verified 2026-10-23
# (toolsmith/scratch/agentscope-skills-verification.md).
#
# SAFETY IS STRUCTURAL: agentscope NEVER descends into a skill's scripts/
# or references/ folders, never executes anything, and never touches the
# network (opencode's configured skills.urls remote catalogs are listed
# but never fetched). It reads SKILL.md frontmatter only.

SKILL_NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def new_skill_resolution(harness):
    return {"harness": harness, "skills": [], "warnings": [], "notes": []}


def add_skill(res, skill_id, folder, name, description, path, scope, via,
              tag=DOC, verdict="VALID", verdict_note=""):
    res["skills"].append({
        "id": skill_id, "folder": folder, "name": name or "",
        "description": (description or "")[:300],
        "path": path, "scope": scope, "via": via, "tag": tag,
        "verdict": verdict, "verdict_note": verdict_note or ""})


def parse_skill_frontmatter(path):
    """Heuristic parse of a SKILL.md frontmatter block.

    Returns (fields, error). fields maps top-level keys to scalar values;
    description continuation lines (indented) are folded with spaces. Full
    YAML is out of scope: anything ambiguous yields an error, never a
    guessed value.
    """
    data = read_bytes(path)
    if data is None:
        return {}, "unreadable"
    try:
        text = data.decode("utf-8")
    except Exception:
        return {}, "not decodable as UTF-8"
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, "missing frontmatter (first line is not ---)"
    end = None
    for i, ln in enumerate(lines[1:], start=1):
        if ln.strip() in ("---", "..."):
            end = i
            break
    if end is None:
        return {}, "unterminated frontmatter"
    fields = {}
    cur_key = None
    for ln in lines[1:end]:
        if re.match(r"^\s", ln) and cur_key == "description":
            val = ln.strip()
            if val:
                fields[cur_key] = (fields.get(cur_key, "") + " " + val).strip()
            continue
        m = re.match(r"^([A-Za-z0-9_./-]+)\s*:\s*(.*)$", ln)
        if m:
            cur_key = m.group(1)
            fields[cur_key] = m.group(2).strip().strip("\"'")
        else:
            cur_key = None
    return fields, None


def validate_skill_fields(name, desc, folder):
    """Spec-constraint checks (agentskills.io). Returns a list of errors."""
    errors = []
    if not name:
        errors.append("missing required frontmatter field 'name'")
    else:
        if len(name) > 64:
            errors.append("name %r exceeds 64 chars" % name)
        if not SKILL_NAME_RE.match(name):
            errors.append(
                "name %r violates spec charset "
                "(lowercase letters/digits/hyphens, no leading/trailing/doubled "
                "hyphens)" % name)
        if name != folder:
            errors.append("name %r does not match folder name %r" % (name, folder))
    if not desc:
        errors.append("missing/empty required frontmatter field 'description'")
    elif len(desc) > 1024:
        errors.append("description exceeds 1024 chars")
    return errors


def _skill_dirs(basedir):
    """(folder, SKILL.md path) for every immediate subdir containing one."""
    found = []
    if not basedir or not os.path.isdir(basedir):
        return found
    try:
        entries = sorted(os.listdir(basedir))
    except OSError:
        return found
    for e in entries:
        p = os.path.join(basedir, e)
        if os.path.isdir(p) and os.path.isfile(os.path.join(p, "SKILL.md")):
            found.append((e, os.path.join(p, "SKILL.md")))
    return found


def _add_skills_from(res, basedir, scope, via, tag=DOC, id_from_path=False,
                     namespace=None):
    for folder, spath in _skill_dirs(basedir):
        fields, err = parse_skill_frontmatter(spath)
        if err:
            add_skill(res, folder, folder, "", "", spath, scope,
                      "%s [frontmatter: %s]" % (via, err), tag, "INVALID",
                      "frontmatter unreadable: %s" % err)
            continue
        name = fields.get("name", "")
        desc = fields.get("description", "")
        errors = validate_skill_fields(name, desc, folder)
        skill_id = folder if id_from_path else (name or folder)
        if namespace:
            skill_id = "%s:%s" % (namespace, skill_id)
        vnote = "; ".join(errors)
        if not errors:
            extras = sorted(k for k in fields
                            if k not in ("name", "description"))
            if extras:
                vnote = ("extra frontmatter fields present (%s) — "
                         "harness-specific, listed not judged"
                         % ", ".join(extras))
            if "disable-model-invocation" in fields:
                vnote = (vnote + "; " if vnote else "") + \
                    "present but harness-gated (disable-model-invocation)"
        add_skill(res, skill_id, folder, name, desc, spath, scope, via, tag,
                  "VALID" if not errors else "INVALID", vnote)


def _skills_dedup(res, precedence, prec_tag=INF):
    """Same-id claims from multiple scopes -> DUPLICATE-NAME for the losers.
    precedence lists scopes highest-priority first."""
    def rank(sk):
        try:
            return precedence.index(sk["scope"])
        except ValueError:
            return len(precedence)
    seen = {}
    for s in res["skills"]:
        key = s["id"]
        if key not in seen:
            seen[key] = s
            continue
        cur, new = seen[key], s
        if rank(new) < rank(cur):
            winner, loser = new, cur
            seen[key] = new
        else:
            winner, loser = cur, new
        loser["verdict"] = "DUPLICATE-NAME"
        loser["verdict_note"] = (
            "same-name skill also present at %s; %s scope wins "
            "[%s: precedence %s]"
            % (winner["path"], winner["scope"], prec_tag,
               ">".join(precedence)))


SKILLS_LOADING_NOTE = (
    "[documented] loading model: harnesses LIST name+description at session "
    "start and load the SKILL.md body on description match (progressive "
    "disclosure). This report is the DISCOVERABLE set per directory — "
    "visible, not 'will be used'.")


def resolve_skills_claude(d):
    res = new_skill_resolution("claude")
    root = git_root(d) or d
    _add_skills_from(res, os.path.join(home(), ".claude", "skills"), "personal",
                     "[documented] personal skills load in every project")
    for anc in upward(d):
        _add_skills_from(res, os.path.join(anc, ".claude", "skills"), "project",
                         "[documented] nested project skills load when working "
                         "in that package")
        if anc == root:
            break
    plugbase = os.path.join(home(), ".claude", "plugins")
    if os.path.isdir(plugbase):
        try:
            plugins = sorted(os.listdir(plugbase))
        except OSError:
            plugins = []
        for p in plugins:
            _add_skills_from(res, os.path.join(plugbase, p, "skills"), "plugin",
                             "[documented] plugin-bundled skills",
                             namespace=p)
    res["notes"].append(
        "[documented-via-guides] collision precedence: enterprise > personal "
        "> project; enterprise-managed directory is not enumerable "
        "statically")
    res["notes"].append(SKILLS_LOADING_NOTE)
    _skills_dedup(res, ["enterprise", "personal", "project", "plugin"], DOC)
    return res


def resolve_skills_codex(d):
    res = new_skill_resolution("codex")
    root = git_root(d) or d
    for anc in upward(d):
        _add_skills_from(res, os.path.join(anc, ".agents", "skills"), "project",
                         "[documented] .agents/skills in every folder from "
                         "the working directory up to the repository root")
        if anc == root:
            break
    _add_skills_from(res, os.path.join(home(), ".agents", "skills"), "personal",
                     "[documented] personal skills")
    if res["skills"]:
        res["notes"].append(
            "[documented] the skill list is limited to ~2% of the context "
            "window — a large discovered set may be truncated at runtime")
    res["notes"].append(SKILLS_LOADING_NOTE)
    _skills_dedup(res, ["personal", "project"])  # [inferred]
    return res


def resolve_skills_cursor(d):
    res = new_skill_resolution("cursor")
    root = git_root(d) or d
    proj_dirs = ((".agents/skills", "[documented]"),
                 (".cursor/skills", "[documented]"),
                 (".claude/skills", "[documented] compatibility dir"),
                 (".codex/skills", "[documented] compatibility dir"))
    for anc in upward(d):
        for rel, tag in proj_dirs:
            _add_skills_from(res, os.path.join(anc, rel), "project",
                             "%s project skill dir" % tag)
        if anc == root:
            break
    _add_skills_from(res, os.path.join(home(), ".agents", "skills"), "personal",
                     "[documented] personal skill dir")
    _add_skills_from(res, os.path.join(home(), ".cursor", "skills"), "personal",
                     "[documented] personal skill dir")
    res["notes"].append(SKILLS_LOADING_NOTE)
    _skills_dedup(res, ["personal", "project"])  # [inferred]
    return res


def resolve_skills_copilot(d):
    res = new_skill_resolution("copilot")
    root = git_root(d) or d
    for anc in upward(d):
        for rel, tag in ((".github/skills", "[documented]"),
                         (".claude/skills", "[documented]"),
                         (".agents/skills", "[documented]")):
            _add_skills_from(res, os.path.join(anc, rel), "project",
                             "%s project skill dir" % tag)
        if anc == root:
            break
    _add_skills_from(res, os.path.join(home(), ".copilot", "skills"), "personal",
                     "[documented] personal skill dir")
    _add_skills_from(res, os.path.join(home(), ".agents", "skills"), "personal",
                     "[documented] personal skill dir")
    res["notes"].append(
        "[documented] in VS Code, ~/.claude/skills is also read as personal")
    res["notes"].append(SKILLS_LOADING_NOTE)
    _skills_dedup(res, ["personal", "project"])  # [inferred]
    return res


def resolve_skills_gemini(d):
    res = new_skill_resolution("gemini")
    root = git_root(d) or d
    for anc in upward(d):
        for rel in (".gemini/skills", ".agents/skills"):
            _add_skills_from(res, os.path.join(anc, rel), "project",
                             "[documented] project skill dir")
        if anc == root:
            break
    _add_skills_from(res, os.path.join(home(), ".gemini", "skills"), "personal",
                     "[documented] personal skill dir")
    _add_skills_from(res, os.path.join(home(), ".agents", "skills"), "personal",
                     "[documented] personal skill dir")
    res["notes"].append(
        "[documented] loading is INTERACTIVE: the model calls activate_skill "
        "and the user confirms the skill name + folder before the body loads "
        "— presence here does not imply the body is ever read")
    _skills_dedup(res, ["personal", "project"])  # [inferred]
    return res


def _opencode_is_v2(d):
    """v2 config signal: mcp.servers layout [documented, v1.4.0 note]."""
    for cfg, _scope in _opencode_configs(d):
        obj = read_json_obj(cfg)
        if isinstance(obj, dict) and isinstance(obj.get("mcp"), dict) \
                and "servers" in obj["mcp"]:
            return True
    return False


def resolve_skills_opencode(d):
    res = new_skill_resolution("opencode")
    root = git_root(d) or d
    v2 = _opencode_is_v2(d)
    pairs = ((".opencode/skills", os.path.join(home(), ".config", "opencode", "skills")),
             (".claude/skills", os.path.join(home(), ".claude", "skills")),
             (".agents/skills", os.path.join(home(), ".agents", "skills")))
    for anc in upward(d):
        for proj_rel, _ in pairs:
            _add_skills_from(res, os.path.join(anc, proj_rel), "project",
                             "[documented] project skill dir (walks up to the "
                             "git worktree)", id_from_path=v2)
        if anc == root:
            break
    for _, glob_dir in pairs:
        _add_skills_from(res, glob_dir, "global",
                         "[documented] global skill dir", id_from_path=v2)
    if v2:
        res["notes"].append(
            "[documented] opencode v2 config detected: skill IDs derive from "
            "the PATH, not frontmatter name; v2 publishes its own source "
            "precedence — verify the order against the docs")
    else:
        res["notes"].append(
            "[documented] opencode v1 semantics: skill ID = frontmatter name; "
            "v2 derives IDs from the path — re-verify if you run v2")
    for cfg, _scope in _opencode_configs(d):
        obj = read_json_obj(cfg)
        urls = obj.get("skills", {}).get("urls") \
            if isinstance(obj.get("skills"), dict) else None
        if urls:
            res["notes"].append(
                "[documented] configured remote skill catalog(s) in %s: "
                "LISTED ONLY — agentscope never fetches them" % cfg)
    res["notes"].append(SKILLS_LOADING_NOTE)
    _skills_dedup(res, ["global", "project"])  # [inferred]
    return res


def resolve_skills_antigravity(d):
    res = new_skill_resolution("antigravity")
    root = git_root(d) or d  # workspace root [inferred]
    _add_skills_from(res, os.path.join(root, ".agents", "skills"), "workspace",
                     "[documented] workspace skill dir")
    _add_skills_from(res, os.path.join(root, ".agent", "skills"), "workspace",
                     "[documented] legacy workspace skill dir (backward compat)")
    _add_skills_from(res, os.path.join(home(), ".gemini", "config", "skills"),
                     "global",
                     "[documented] global candidate (most recent source, "
                     "citing official docs)")
    _add_skills_from(res, os.path.join(home(), ".gemini", "antigravity", "skills"),
                     "global",
                     "[inferred] global candidate (older sources)")
    res["notes"].append(
        "global skill path UNRESOLVED: ~/.gemini/config/skills (recent, cites "
        "official docs) vs ~/.gemini/antigravity/skills (older) — verify "
        "against the docs; BOTH are modeled above")
    res["notes"].append(
        "[documented] same-name collision: workspace version takes "
        "precedence over global")
    res["notes"].append(SKILLS_LOADING_NOTE)
    _skills_dedup(res, ["workspace", "global"], DOC)
    return res


def resolve_skills_cline(d):
    res = new_skill_resolution("cline")
    res["notes"].append(
        "[inferred] no documented Agent Skills directory found for Cline — "
        "not modeled")
    return res


SKILLS_RESOLVERS = {"codex": resolve_skills_codex,
                    "claude": resolve_skills_claude,
                    "cursor": resolve_skills_cursor,
                    "copilot": resolve_skills_copilot,
                    "gemini": resolve_skills_gemini,
                    "opencode": resolve_skills_opencode,
                    "antigravity": resolve_skills_antigravity,
                    "cline": resolve_skills_cline}
RESOLVERS["skills"] = SKILLS_RESOLVERS


def skill_lines(sk):
    line = "  %-13s %-30s [%s scope, %s]" % (
        sk["verdict"], sk["id"], sk["scope"], sk["path"])
    out = [line]
    if sk["description"]:
        out.append("    description: %s" % sk["description"])
    out.append("    via: %s" % sk["via"])
    if sk["verdict_note"]:
        out.append("    note: %s" % sk["verdict_note"])
    return out


def render_skills_text(results, outline=False):
    lines = [BANNER, "",
             "SKILLS ARE DISCOVERY, NOT LOADING: this is the set of skills "
             "each harness", "can SEE from this directory. Harnesses load "
             "skill bodies on-demand by", "description match (progressive "
             "disclosure); visible != will be used. (The Oct 22 rejection of "
             "this", "angle — \"an effective set would overstate loading "
             "semantics\" — is kept visible", "in the README Limitations.)",
             ""]
    for r in results:
        lines.append("== %s (skills) ==" % r["harness"])
        if outline:
            for sk in r["skills"]:
                lines.append("  %-13s %-30s [%s]" % (
                    sk["verdict"], sk["id"], sk["scope"]))
        elif not r["skills"]:
            lines.append("  (no skills resolve for this directory)")
        else:
            for sk in r["skills"]:
                lines.extend(skill_lines(sk))
        for w in r["warnings"]:
            lines.append("  WARNING: %s" % w)
        for n in r["notes"]:
            lines.append("  note: %s" % n)
        lines.append("")
    return "\n".join(lines).rstrip("\n") + "\n"


def render_skills_json(results, directory):
    return json.dumps({
        "tool": "agentscope", "version": VERSION, "mode": "skills",
        "rules_verified": RULES_VERIFIED,
        "directory": os.path.abspath(directory),
        "disclaimer": "model of documented harness behavior, not runtime "
                      "truth; this is the DISCOVERABLE skill set, not the "
                      "loaded set — skills load on-demand by description "
                      "match",
        "harnesses": [{"harness": r["harness"],
                       "skills": [dict(s) for s in r["skills"]],
                       "warnings": r["warnings"], "notes": r["notes"]}
                      for r in results],
    }, indent=2) + "\n"


def render_permissions_text(results, outline=False):
    lines = [BANNER, ""]
    for r in results:
        lines.append("== %s ==" % r["harness"])
        if outline:
            lines.append("  %d rule(s), %d context setting(s)" % (
                len(r["rules"]), len(r["context"])))
        elif not r["rules"] and not r["context"]:
            lines.append("  (no permission rules found)")
        else:
            for c in r["context"]:
                lines.append("  [%s|%s] %s = %s" % (
                    c["scope"], c["tag"], c["name"], c["value"]))
                if c["note"]:
                    lines.append("      note: %s" % c["note"])
            for p in r["rules"]:
                lines.append("  [%s|%s] %s: %s" % (
                    p["scope"], p["tag"], p["kind"], p["rule"]))
                if p["note"]:
                    lines.append("      note: %s" % p["note"])
        for w in r["warnings"]:
            lines.append("  WARNING: %s" % w)
        for n in r["notes"]:
            lines.append("  note: %s" % n)
        lines.append("")
    return "\n".join(lines).rstrip("\n") + "\n"


def render_permissions_json(results, directory):
    return json.dumps({
        "tool": "agentscope", "version": VERSION,
        "rules_verified": RULES_VERIFIED,
        "directory": os.path.abspath(directory), "mode": "permissions",
        "disclaimer": "model of documented harness behavior, not runtime truth",
        "harnesses": [{
            "harness": r["harness"], "rules": r["rules"],
            "context": r["context"], "warnings": r["warnings"],
            "notes": r["notes"],
        } for r in results],
    }, indent=2) + "\n"

# --------------------------------------------------------------------------
# output
# --------------------------------------------------------------------------

def contrib_lines(c, indent=0):
    pad = "  " * indent
    scope = ("  [%s]" % c["scope"]) if c.get("scope") else ""
    line = "%s- %s  (%d bytes, %s)%s\n%s    via: %s" % (
        pad, c["path"], c["bytes"], c["role"], scope, pad, c["via"])
    out = [line]
    for ch in c.get("imports", []):
        out.extend(contrib_lines(ch, indent + 1))
    return out


def render_text(results, full=False, outline=False):
    lines = [BANNER, ""]
    for r in results:
        lines.append("== %s ==" % r["harness"])
        if outline:
            for c in r["contributors"]:
                lines.append("  %s" % c["path"])
        elif not r["contributors"]:
            lines.append("  (no instruction files found)")
        else:
            for c in r["contributors"]:
                lines.extend(contrib_lines(c))
            lines.append("  total: %d bytes%s" % (
                r["total_bytes"],
                ("  [cap %d bytes — ESTIMATED truncation]" % r["cap_bytes"])
                if r["estimated_truncated"] else
                (("  [cap %d bytes — under cap]" % r["cap_bytes"])
                 if r["cap_bytes"] else "")))
        for w in r["warnings"]:
            lines.append("  WARNING: %s" % w)
        for n in r["notes"]:
            lines.append("  note: %s" % n)
        if full and r["contributors"] and not outline:
            lines.append("  --- merged content (load order) ---")
            for c in r["contributors"]:
                lines.append("  >>> %s" % c["path"])
                for ch in [c] + flatten(c):
                    if ch is not c:
                        lines.append("  >>> (import) %s" % ch["path"])
                    lines.append(ch.get("text", "").rstrip("\n"))
        lines.append("")
    return "\n".join(lines).rstrip("\n") + "\n"


def serializable(c):
    d = {k: v for k, v in c.items() if k != "text"}
    d["imports"] = [serializable(ch) for ch in c.get("imports", [])]
    return d


def render_json(results, directory):
    return json.dumps({
        "tool": "agentscope", "version": VERSION,
        "rules_verified": RULES_VERIFIED,
        "directory": os.path.abspath(directory),
        "disclaimer": "model of documented harness behavior, not runtime truth",
        "harnesses": [{
            "harness": r["harness"],
            "contributors": [serializable(c) for c in r["contributors"]],
            "warnings": r["warnings"], "notes": r["notes"],
            "total_bytes": r["total_bytes"], "cap_bytes": r["cap_bytes"],
            "estimated_truncated": r["estimated_truncated"],
        } for r in results],
    }, indent=2) + "\n"


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def build_parser():
    ap = argparse.ArgumentParser(
        prog="agentscope",
        description="Resolve the effective instruction set a coding-agent harness "
                    "loads for a directory. Read-only; a model of documented "
                    "behavior, not runtime truth.")
    ap.add_argument("directory", nargs="?", default=".",
                    help="directory to resolve for (default: cwd)")
    ap.add_argument("--harness", action="append", default=[],
                    help="harness to resolve (%s); repeatable; default: all"
                    % ", ".join(HARNESSES))
    ap.add_argument("--list-harnesses", action="store_true",
                    help="list supported harnesses and exit")
    ap.add_argument("--outline", action="store_true",
                    help="structure only: contributing file paths per harness")
    ap.add_argument("--full", action="store_true",
                    help="also print merged content in load order")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--diff-harness", nargs=2, metavar=("A", "B"),
                    help="compare the loaded file sets of two harnesses")
    ap.add_argument("--mcp", action="store_true",
                    help="resolve the effective MCP server set per harness "
                         "instead of instruction files (env values and URL "
                         "query strings are never printed; --full refused)")
    ap.add_argument("--hooks", action="store_true",
                    help="resolve the effective hook set per harness "
                         "instead of instruction files (pure config parsing; "
                         "hook commands are never executed; --full ignored)")
    ap.add_argument("--permissions", action="store_true",
                    help="resolve the effective allow/deny/ask rule set per "
                         "harness instead of instruction files (pure config "
                         "parsing; rules are shown verbatim, never executed "
                         "or evaluated against real tool calls; --full "
                         "ignored)")
    ap.add_argument("--skills", action="store_true",
                    help="resolve the discoverable Agent Skills set per "
                         "harness instead of instruction files (SKILL.md "
                         "frontmatter only; scripts/ never read or executed; "
                         "remote catalogs never fetched; --full ignored)")
    ap.add_argument("--version", action="store_true", help="print version and exit")
    return ap


def normalize_harness(name):
    n = name.lower()
    n = ALIASES.get(n, n)
    return n if n in HARNESSES else None


def main(argv=None):
    ap = build_parser()
    args = ap.parse_args(argv)
    if args.version:
        print("agentscope %s" % VERSION)
        return 0
    if args.list_harnesses:
        print("\n".join(HARNESSES))
        return 0
    d = os.path.abspath(args.directory)
    if not os.path.isdir(d):
        print("agentscope: not a directory: %s" % args.directory, file=sys.stderr)
        return 2
    modes = [m for m, on in (("mcp", args.mcp), ("hooks", args.hooks),
                                   ("permissions", args.permissions),
                                   ("skills", args.skills)) if on]
    if len(modes) > 1:
        print("agentscope: --mcp, --hooks, --permissions and --skills are "
              "mutually exclusive", file=sys.stderr)
        return 2
    mode = modes[0] if modes else "instructions"
    if args.mcp and args.full:
        print("agentscope: --full is refused in --mcp mode (config files may "
              "contain secrets)", file=sys.stderr)
        return 2
    if args.diff_harness:
        names = []
        for n in args.diff_harness:
            nn = normalize_harness(n)
            if nn is None:
                print("agentscope: unknown harness: %s" % n, file=sys.stderr)
                return 2
            names.append(nn)
        ra = RESOLVERS[mode][names[0]](d)
        rb = RESOLVERS[mode][names[1]](d)
        if mode == "mcp":
            sa = {s["name"] for s in ra["servers"]}
            sb = {s["name"] for s in rb["servers"]}
            what = "MCP servers"
        elif mode == "hooks":
            def _hk(h):
                return "%s [matcher %r]: %s" % (h["event"], h["matcher"],
                                                h["command"])
            sa = {_hk(h) for h in ra["hooks"]}
            sb = {_hk(h) for h in rb["hooks"]}
            what = "hooks"
        elif mode == "permissions":
            def _pr(p):
                return "%s|%s|%s" % (p["scope"], p["kind"], p["rule"])
            def _pc(c):
                return "context|%s|%s" % (c["name"], c["value"])
            sa = {_pr(p) for p in ra["rules"]} | \
                {_pc(c) for c in ra["context"]}
            sb = {_pr(p) for p in rb["rules"]} | \
                {_pc(c) for c in rb["context"]}
            what = "permission rules"
        elif mode == "skills":
            sa = {(s["id"], s["scope"], s["path"]) for s in ra["skills"]}
            sb = {(s["id"], s["scope"], s["path"]) for s in rb["skills"]}
            what = "discoverable skills"
        else:
            sa = {c["path"] for c in ra["contributors"]}
            sb = {c["path"] for c in rb["contributors"]}
            what = "instruction files"
        if args.json:
            print(json.dumps({"a": names[0], "b": names[1], "mode": mode,
                              "what": what,
                              "only_in_a": sorted(sa - sb),
                              "only_in_b": sorted(sb - sa),
                              "in_both": sorted(sa & sb)}, indent=2))
        else:
            print(BANNER + "\n")
            print("only in %s (%s):" % (names[0], what))
            for p in sorted(sa - sb):
                print("  - %s" % p)
            print("only in %s (%s):" % (names[1], what))
            for p in sorted(sb - sa):
                print("  - %s" % p)
            print("in both (%s):" % what)
            for p in sorted(sa & sb):
                print("  = %s" % p)
        return 0
    if args.harness:
        names = []
        for n in args.harness:
            nn = normalize_harness(n)
            if nn is None:
                print("agentscope: unknown harness: %s" % n, file=sys.stderr)
                return 2
            if nn not in names:
                names.append(nn)
    else:
        names = list(HARNESSES)
    results = [RESOLVERS[mode][n](d) for n in names]
    if args.json:
        if mode == "mcp":
            sys.stdout.write(render_mcp_json(results, d))
        elif mode == "hooks":
            sys.stdout.write(render_hooks_json(results, d))
        elif mode == "permissions":
            sys.stdout.write(render_permissions_json(results, d))
        elif mode == "skills":
            sys.stdout.write(render_skills_json(results, d))
        else:
            sys.stdout.write(render_json(results, d))
    else:
        if mode == "mcp":
            sys.stdout.write(render_mcp_text(results, outline=args.outline))
        elif mode == "hooks":
            sys.stdout.write(render_hooks_text(results, outline=args.outline))
        elif mode == "permissions":
            sys.stdout.write(render_permissions_text(results,
                                                     outline=args.outline))
        elif mode == "skills":
            sys.stdout.write(render_skills_text(results, outline=args.outline))
        else:
            sys.stdout.write(render_text(results, full=args.full,
                                         outline=args.outline))
    if mode == "mcp":
        return 0 if any(r["servers"] for r in results) else 1
    if mode == "hooks":
        return 0 if any(r["hooks"] for r in results) else 1
    if mode == "permissions":
        return 0 if any(r["rules"] or r["context"] for r in results) else 1
    if mode == "skills":
        return 0 if any(r["skills"] for r in results) else 1
    return 0 if any(r["contributors"] for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
