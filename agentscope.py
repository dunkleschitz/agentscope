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

Exit codes: 0 = at least one contributing file resolved; 1 = no instruction
files found for the selected harness(es) (the harness would load nothing);
2 = usage error.

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
import json
import os
import re
import sys

VERSION = "1.1.0"
RULES_VERIFIED = "2026-10-22"  # date harness rules were last checked vs docs

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

HARNESSES = ("codex", "claude", "cursor", "copilot", "gemini")
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


INSTR_RESOLVERS = {"codex": resolve_codex, "claude": resolve_claude,
                   "cursor": resolve_cursor, "copilot": resolve_copilot,
                   "gemini": resolve_gemini}


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


MCP_RESOLVERS = {"codex": resolve_mcp_codex, "claude": resolve_mcp_claude,
                 "cursor": resolve_mcp_cursor, "copilot": resolve_mcp_copilot,
                 "gemini": resolve_mcp_gemini}
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
    mode = "mcp" if args.mcp else "instructions"
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
        sys.stdout.write(render_mcp_json(results, d) if mode == "mcp"
                         else render_json(results, d))
    else:
        sys.stdout.write(render_mcp_text(results, outline=args.outline)
                         if mode == "mcp"
                         else render_text(results, full=args.full,
                                          outline=args.outline))
    if mode == "mcp":
        return 0 if any(r["servers"] for r in results) else 1
    return 0 if any(r["contributors"] for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
