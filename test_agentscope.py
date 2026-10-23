#!/usr/bin/env python3
"""Tests for agentscope. Hermetic: every test builds its own fixture tree and
points HOME at an empty dir so no real user files leak in."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

TOOL = os.path.join(os.path.dirname(os.path.abspath(__file__)), "agentscope.py")


def make_tree(struct):
    """struct: {relpath: content}; returns (root, home). Content None = dir."""
    root = tempfile.mkdtemp(prefix="as-root-")
    home = tempfile.mkdtemp(prefix="as-home-")
    for rel, content in struct.items():
        p = os.path.join(root, rel)
        if content is None:
            os.makedirs(p, exist_ok=True)
        else:
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w") as f:
                f.write(content)
    return root, home


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = []

    def tearDown(self):
        for t in self._tmp:
            shutil.rmtree(t, ignore_errors=True)

    def tree(self, struct):
        root, home = make_tree(struct)
        self._tmp += [root, home]
        return root, home

    def tool(self, home, *args, cwd=None):
        env = dict(os.environ, HOME=home)
        return subprocess.run([sys.executable, TOOL, *args], capture_output=True,
                              text=True, env=env, cwd=cwd or os.getcwd())


class CodexTests(Base):
    def test_chain_order_and_override(self):
        root, home = self.tree({
            ".git": None, "AGENTS.md": "# root\n", "sub/AGENTS.md": "# sub\n",
            "sub/AGENTS.override.md": "# sub-override\n",
        })
        r = self.tool(home, "--harness", "codex", "--json", os.path.join(root, "sub"))
        self.assertEqual(r.returncode, 0)
        paths = [c["path"] for c in json.loads(r.stdout)["harnesses"][0]["contributors"]]
        self.assertEqual(paths, [os.path.join(root, "AGENTS.md"),
                                 os.path.join(root, "sub", "AGENTS.override.md")])

    def test_global_first_non_empty(self):
        root, home = self.tree({".git": None, "AGENTS.md": "# root\n"})
        os.makedirs(os.path.join(home, ".codex"))
        open(os.path.join(home, ".codex", "AGENTS.override.md"), "w").write("")  # empty
        open(os.path.join(home, ".codex", "AGENTS.md"), "w").write("# global\n")
        r = self.tool(home, "--harness", "codex", "--json", root)
        paths = [c["path"] for c in json.loads(r.stdout)["harnesses"][0]["contributors"]]
        self.assertIn(os.path.join(home, ".codex", "AGENTS.md"), paths)

    def test_cap_warning_and_estimated_cutoff(self):
        root, home = self.tree({".git": None, "AGENTS.md": "x" * 40000})
        r = self.tool(home, "--harness", "codex", "--json", root)
        h = json.loads(r.stdout)["harnesses"][0]
        self.assertTrue(h["estimated_truncated"])
        self.assertEqual(h["cap_bytes"], 32768)
        self.assertTrue(any("ESTIMATED" in w.upper() or "estimated" in w
                            for w in h["warnings"]))

    def test_custom_cap_from_config(self):
        root, home = self.tree({".git": None, "AGENTS.md": "x" * 40000})
        os.makedirs(os.path.join(home, ".codex"))
        open(os.path.join(home, ".codex", "config.toml"), "w").write(
            "project_doc_max_bytes = 65536\n")
        r = self.tool(home, "--harness", "codex", "--json", root)
        h = json.loads(r.stdout)["harnesses"][0]
        self.assertEqual(h["cap_bytes"], 65536)
        self.assertFalse(h["estimated_truncated"])


class ClaudeTests(Base):
    def test_upward_merge_order(self):
        root, home = self.tree({
            ".git": None, "CLAUDE.md": "# root\n",
            "sub/CLAUDE.md": "# sub\n", "sub/deep/CLAUDE.md": "# deep\n",
        })
        r = self.tool(home, "--harness", "claude", "--json", os.path.join(root, "sub", "deep"))
        paths = [c["path"] for c in json.loads(r.stdout)["harnesses"][0]["contributors"]]
        self.assertEqual(paths, [os.path.join(root, "CLAUDE.md"),
                                 os.path.join(root, "sub", "CLAUDE.md"),
                                 os.path.join(root, "sub", "deep", "CLAUDE.md")])

    def test_agentsmd_fallback(self):
        root, home = self.tree({".git": None, "AGENTS.md": "# agents\n"})
        r = self.tool(home, "--harness", "claude", "--json", root)
        roles = [c["role"] for c in json.loads(r.stdout)["harnesses"][0]["contributors"]]
        self.assertIn("project-fallback", roles)

    def test_local_md_included(self):
        root, home = self.tree({".git": None, "CLAUDE.local.md": "# local\n"})
        r = self.tool(home, "--harness", "claude", "--json", root)
        roles = [c["role"] for c in json.loads(r.stdout)["harnesses"][0]["contributors"]]
        self.assertIn("local", roles)

    def test_import_cycle_terminates(self):
        root, home = self.tree({
            "CLAUDE.md": "see @b.md\n",
            "b.md": "back @CLAUDE.md\n",
        })
        r = self.tool(home, "--harness", "claude", "--json", root)
        self.assertEqual(r.returncode, 0)
        h = json.loads(r.stdout)["harnesses"][0]
        self.assertTrue(any("cycle" in w for w in h["warnings"]))

    def test_import_depth_cap(self):
        struct = {"f0.md": "next @f1.md\n"}
        for i in range(1, 8):
            struct["f%d.md" % i] = "next @f%d.md\n" % (i + 1)
        struct["CLAUDE.md"] = "start @f0.md\n"
        root, home = self.tree(struct)
        r = self.tool(home, "--harness", "claude", "--json", root)
        h = json.loads(r.stdout)["harnesses"][0]

        def maxdepth(cs, d=0):
            m = d
            for c in cs:
                m = max(m, maxdepth(c["imports"], d + 1))
            return m
        self.assertLessEqual(maxdepth(h["contributors"][0]["imports"]), 4)
        self.assertTrue(any("depth cap" in w for w in h["warnings"]))

    def test_email_tokens_ignored(self):
        root, home = self.tree({"CLAUDE.md": "contact dev@example.com for help\n"})
        r = self.tool(home, "--harness", "claude", "--json", root)
        h = json.loads(r.stdout)["harnesses"][0]
        self.assertEqual(len(h["contributors"]), 1)
        self.assertEqual(h["warnings"], [])


class CursorTests(Base):
    def test_rule_globs_heuristic(self):
        root, home = self.tree({
            ".git": None, "src": None, "src/app.py": "# app\n",
            ".cursor/rules/py.mdc": "---\nglobs: \"**/*.py\"\n---\n# py\n",
            ".cursor/rules/js.mdc": "---\nglobs: \"**/*.js\"\n---\n# js\n",
        })
        r = self.tool(home, "--harness", "cursor", "--json", os.path.join(root, "src"))
        cs = {os.path.basename(c["path"]): c for c in
              json.loads(r.stdout)["harnesses"][0]["contributors"]}
        self.assertIn("MATCHES", cs["py.mdc"].get("scope", ""))
        self.assertIn("does not match", cs["js.mdc"].get("scope", ""))

    def test_legacy_cursorrules(self):
        root, home = self.tree({".git": None, ".cursorrules": "# legacy\n"})
        r = self.tool(home, "--harness", "cursor", "--json", root)
        roles = [c["role"] for c in json.loads(r.stdout)["harnesses"][0]["contributors"]]
        self.assertIn("project-rule", roles)

    def test_nested_agentsmd(self):
        root, home = self.tree({".git": None, "AGENTS.md": "# root\n",
                                 "sub/AGENTS.md": "# sub\n"})
        r = self.tool(home, "--harness", "cursor", "--json", os.path.join(root, "sub"))
        paths = [c["path"] for c in json.loads(r.stdout)["harnesses"][0]["contributors"]]
        self.assertIn(os.path.join(root, "AGENTS.md"), paths)
        self.assertIn(os.path.join(root, "sub", "AGENTS.md"), paths)


class CopilotTests(Base):
    def test_nearest_agentsmd_wins(self):
        root, home = self.tree({".git": None, "AGENTS.md": "# root\n",
                                 "sub/AGENTS.md": "# sub\n"})
        r = self.tool(home, "--harness", "copilot", "--json", os.path.join(root, "sub"))
        paths = [c["path"] for c in json.loads(r.stdout)["harnesses"][0]["contributors"]]
        self.assertEqual(paths, [os.path.join(root, "sub", "AGENTS.md")])

    def test_copilot_instructions(self):
        root, home = self.tree({
            ".git": None, ".github/copilot-instructions.md": "# copilot\n"})
        r = self.tool(home, "--harness", "copilot", "--json", root)
        self.assertEqual(r.returncode, 0)
        roles = [c["role"] for c in json.loads(r.stdout)["harnesses"][0]["contributors"]]
        self.assertIn("project", roles)


class GeminiTests(Base):
    def test_custom_filename_from_settings(self):
        root, home = self.tree({
            ".git": None,
            ".gemini/settings.json": '{"context": {"fileName": ["CONTEXT.md"]}}',
            "CONTEXT.md": "# ctx\n", "GEMINI.md": "# gem\n",
        })
        r = self.tool(home, "--harness", "gemini", "--json", root)
        paths = [c["path"] for c in json.loads(r.stdout)["harnesses"][0]["contributors"]]
        self.assertIn(os.path.join(root, "CONTEXT.md"), paths)
        self.assertNotIn(os.path.join(root, "GEMINI.md"), paths)

    def test_global_gemini_md(self):
        root, home = self.tree({})
        os.makedirs(os.path.join(home, ".gemini"))
        open(os.path.join(home, ".gemini", "GEMINI.md"), "w").write("# global\n")
        r = self.tool(home, "--harness", "gemini", "--json", root)
        self.assertEqual(r.returncode, 0)
        roles = [c["role"] for c in json.loads(r.stdout)["harnesses"][0]["contributors"]]
        self.assertIn("global", roles)

    def test_at_import_inline(self):
        root, home = self.tree({"GEMINI.md": "main @part.md\n", "part.md": "# part\n"})
        r = self.tool(home, "--harness", "gemini", "--json", root)
        h = json.loads(r.stdout)["harnesses"][0]
        self.assertEqual(h["contributors"][0]["imports"][0]["path"],
                         os.path.join(root, "part.md"))


class CliTests(Base):
    def test_empty_dir_exit_1(self):
        root, home = self.tree({})
        r = self.tool(home, root)
        self.assertEqual(r.returncode, 1)
        self.assertIn("no instruction files", r.stdout.lower())

    def test_unknown_harness_exit_2(self):
        root, home = self.tree({})
        r = self.tool(home, "--harness", "nope", root)
        self.assertEqual(r.returncode, 2)

    def test_not_a_dir_exit_2(self):
        root, home = self.tree({"f.txt": "x"})
        r = self.tool(home, os.path.join(root, "f.txt"))
        self.assertEqual(r.returncode, 2)

    def test_diff_harness(self):
        root, home = self.tree({".git": None, "AGENTS.md": "# root\n",
                                 "sub/AGENTS.md": "# sub\n"})
        r = self.tool(home, "--diff-harness", "codex", "copilot",
                     "--json", os.path.join(root, "sub"))
        self.assertEqual(r.returncode, 0)
        d = json.loads(r.stdout)
        self.assertIn(os.path.join(root, "AGENTS.md"), d["only_in_a"])  # codex: both
        self.assertIn(os.path.join(root, "sub", "AGENTS.md"), d["in_both"])

    def test_outline(self):
        root, home = self.tree({"AGENTS.md": "# a\n"})
        r = self.tool(home, "--harness", "codex", "--outline", root)
        self.assertIn("AGENTS.md", r.stdout)
        self.assertNotIn("via:", r.stdout)

    def test_read_only(self):
        root, home = self.tree({"AGENTS.md": "# a\n", "sub/CLAUDE.md": "# c\n"})
        before = set()
        for dp, _, fns in os.walk(root):
            before.update(os.path.join(dp, f) for f in fns)
        self.tool(home, "--full", root)
        after = set()
        for dp, _, fns in os.walk(root):
            after.update(os.path.join(dp, f) for f in fns)
        self.assertEqual(before, after)

    def test_tag_vocabulary(self):
        root, home = self.tree({
            ".git": None, "AGENTS.md": "# a\n", "CLAUDE.md": "# c\n",
            ".cursor/rules/r.mdc": "---\nglobs: \"*\"\n---\n",
            ".github/copilot-instructions.md": "# p\n", "GEMINI.md": "# g\n"})
        r = self.tool(home, "--json", root)
        doc = json.loads(r.stdout)
        for h in doc["harnesses"]:
            for c in h["contributors"]:
                self.assertTrue("[documented]" in c["via"] or "[inferred]" in c["via"],
                                c["path"])
            for w in h["warnings"] + h["notes"]:
                self.assertTrue("[documented]" in w or "[inferred]" in w, w)

    def test_json_shape(self):
        root, home = self.tree({"AGENTS.md": "# a\n"})
        r = self.tool(home, "--json", root)
        doc = json.loads(r.stdout)
        self.assertEqual(doc["tool"], "agentscope")
        self.assertIn("disclaimer", doc)
        self.assertIn("rules_verified", doc)
        c = doc["harnesses"][0]["contributors"][0]
        for k in ("path", "bytes", "role", "via"):
            self.assertIn(k, c)

    def test_version_and_list(self):
        root, home = self.tree({})
        r = self.tool(home, "--version")
        self.assertEqual(r.returncode, 0)
        self.assertIn("1.5.0", r.stdout)
        r = self.tool(home, "--list-harnesses")
        for h in ("codex", "claude", "cursor", "copilot", "gemini", "opencode"):
            self.assertIn(h, r.stdout)


class McpTests(Base):
    SECRET = "s3cr3t-token-value-xyz"

    def mcp_tool(self, home, *args, **kw):
        return self.tool(home, "--mcp", *args, **kw)

    def test_claude_all_scopes(self):
        root, home = self.tree({
            ".git": None,
            ".mcp.json": json.dumps({"mcpServers": {
                "gh": {"command": "gh-mcp", "args": ["--x"]}}}),
        })
        with open(os.path.join(home, ".claude.json"), "w") as f:
            json.dump({"mcpServers": {
                "stripe": {"url": "https://mcp.stripe.com/mcp"}}},
                      f)
        r = self.mcp_tool(home, "--harness", "claude", "--json", root)
        self.assertEqual(r.returncode, 0)
        doc = json.loads(r.stdout)
        self.assertEqual(doc["mode"], "mcp")
        sv = {s["name"]: s for s in doc["harnesses"][0]["servers"]}
        self.assertEqual(sv["gh"]["scope"], "project")
        self.assertEqual(sv["gh"]["transport"], "stdio")
        self.assertEqual(sv["stripe"]["scope"], "user")
        self.assertEqual(sv["stripe"]["transport"], "http")
        self.assertIn("approval", sv["gh"]["note"])

    def test_claude_local_scope_project_key(self):
        root, home = self.tree({".git": None})
        with open(os.path.join(home, ".claude.json"), "w") as f:
            json.dump({"projects": {root: {"mcpServers": {
                "db": {"command": "db-srv"}}}}}, f)
        r = self.mcp_tool(home, "--harness", "claude", root)
        self.assertEqual(r.returncode, 0)
        self.assertIn("db", r.stdout)
        self.assertIn("local scope", r.stdout)

    def test_env_values_never_printed(self):
        root, home = self.tree({
            ".mcp.json": json.dumps({"mcpServers": {"srv": {
                "command": "x", "env": {"API_KEY": self.SECRET}}}}),
        })
        r = self.mcp_tool(home, "--harness", "claude", root)
        self.assertNotIn(self.SECRET, r.stdout)
        self.assertNotIn(self.SECRET, r.stderr)
        self.assertIn("API_KEY=<set>", r.stdout)
        r = self.mcp_tool(home, "--harness", "claude", "--json", root)
        self.assertNotIn(self.SECRET, r.stdout)
        self.assertNotIn(self.SECRET, r.stderr)
        self.assertEqual(json.loads(r.stdout)["harnesses"][0]["servers"][0]["env"],
                         {"API_KEY": "<set>"})

    def test_url_query_and_fragment_masked(self):
        root, home = self.tree({
            ".mcp.json": json.dumps({"mcpServers": {"srv": {
                "url": "https://h.example/mcp?token=%s#frag" % self.SECRET,
                "headers": {"Authorization": self.SECRET}}}}),
        })
        r = self.mcp_tool(home, "--harness", "claude", root)
        self.assertNotIn(self.SECRET, r.stdout)
        self.assertIn("https://h.example/mcp", r.stdout)
        self.assertNotIn("?token=", r.stdout)
        r2 = self.mcp_tool(home, "--harness", "claude", "--json", root)
        self.assertNotIn(self.SECRET, r2.stdout)
        url = json.loads(r2.stdout)["harnesses"][0]["servers"][0]["url"]
        self.assertEqual(url, "https://h.example/mcp")

    def test_codex_toml_global_and_project(self):
        root, home = self.tree({".git": None})
        os.makedirs(os.path.join(home, ".codex"), exist_ok=True)
        with open(os.path.join(home, ".codex", "config.toml"), "w") as f:
            f.write('[mcp_servers.fs]\ncommand = "fs-srv"\nargs = ["--root", "/"]\n'
                    '[mcp_servers.remote]\nurl = "https://api.example.com/mcp"\n')
        os.makedirs(os.path.join(root, ".codex"), exist_ok=True)
        with open(os.path.join(root, ".codex", "config.toml"), "w") as f:
            f.write('[mcp_servers.proj]\ncommand = "proj-srv"\n')
        r = self.mcp_tool(home, "--harness", "codex", "--json", root)
        self.assertEqual(r.returncode, 0)
        sv = {s["name"]: s for s in json.loads(r.stdout)["harnesses"][0]["servers"]}
        self.assertEqual(sv["fs"]["scope"], "global")
        self.assertEqual(sv["fs"]["args"], ["--root", "/"])
        self.assertEqual(sv["remote"]["transport"], "http")
        self.assertEqual(sv["proj"]["scope"], "project")

    def test_codex_project_config_ignored_without_codex_dir(self):
        root, home = self.tree({".git": None})
        # stray config.toml outside a .codex/ dir must not load
        with open(os.path.join(root, "config.toml"), "w") as f:
            f.write('[mcp_servers.stray]\ncommand = "x"\n')
        r = self.mcp_tool(home, "--harness", "codex", root)
        self.assertEqual(r.returncode, 1)
        self.assertIn("(no MCP servers resolve", r.stdout)

    def test_cursor_pair(self):
        root, home = self.tree({
            ".git": None,
            ".cursor/mcp.json": json.dumps({"mcpServers": {
                "p": {"command": "p-srv"}}}),
        })
        os.makedirs(os.path.join(home, ".cursor"), exist_ok=True)
        with open(os.path.join(home, ".cursor", "mcp.json"), "w") as f:
            json.dump({"mcpServers": {"g": {"url": "https://g.example/mcp"}}}, f)
        r = self.mcp_tool(home, "--harness", "cursor", "--json", root)
        sv = {s["name"]: s for s in json.loads(r.stdout)["harnesses"][0]["servers"]}
        self.assertEqual(sv["p"]["scope"], "project")
        self.assertEqual(sv["g"]["scope"], "global")
        self.assertEqual(sv["g"]["transport"], "http")

    def test_copilot_project_and_global_and_servers_key(self):
        root, home = self.tree({
            ".git": None,
            ".github/mcp.json": json.dumps({"servers": {"p": {"command": "p"}}}),
        })
        os.makedirs(os.path.join(home, ".copilot"), exist_ok=True)
        with open(os.path.join(home, ".copilot", "mcp-config.json"), "w") as f:
            json.dump({"mcpServers": {"g": {"command": "g"}}}, f)
        r = self.mcp_tool(home, "--harness", "copilot", "--json", root)
        sv = {s["name"]: s for s in json.loads(r.stdout)["harnesses"][0]["servers"]}
        self.assertEqual(sv["p"]["scope"], "project")
        self.assertEqual(sv["g"]["scope"], "global")

    def test_gemini_pair(self):
        root, home = self.tree({
            ".git": None,
            ".gemini/settings.json": json.dumps({"mcpServers": {
                "p": {"command": "p"}}}),
        })
        os.makedirs(os.path.join(home, ".gemini"), exist_ok=True)
        with open(os.path.join(home, ".gemini", "settings.json"), "w") as f:
            json.dump({"mcpServers": {"u": {"command": "u"}}}, f)
        r = self.mcp_tool(home, "--harness", "gemini", "--json", root)
        sv = {s["name"]: s for s in json.loads(r.stdout)["harnesses"][0]["servers"]}
        self.assertEqual(sv["p"]["scope"], "project")
        self.assertEqual(sv["u"]["scope"], "user")

    def test_name_collision_warning(self):
        root, home = self.tree({
            ".git": None,
            ".gemini/settings.json": json.dumps({"mcpServers": {
                "dup": {"command": "p"}}}),
        })
        os.makedirs(os.path.join(home, ".gemini"), exist_ok=True)
        with open(os.path.join(home, ".gemini", "settings.json"), "w") as f:
            json.dump({"mcpServers": {"dup": {"command": "u"}}}, f)
        r = self.mcp_tool(home, "--harness", "gemini", root)
        self.assertIn("multiple scopes", r.stdout)
        self.assertIn("dup", r.stdout)

    def test_malformed_json_does_not_crash(self):
        root, home = self.tree({".git": None, ".mcp.json": "{not json"})
        r = self.mcp_tool(home, "--harness", "claude", root)
        self.assertEqual(r.returncode, 1)
        self.assertIn("WARNING", r.stdout)

    def test_malformed_toml_does_not_crash(self):
        root, home = self.tree({".git": None})
        os.makedirs(os.path.join(home, ".codex"), exist_ok=True)
        with open(os.path.join(home, ".codex", "config.toml"), "w") as f:
            f.write("[mcp_servers\nbroken = = =\n")
        r = self.mcp_tool(home, "--harness", "codex", root)
        self.assertIn("WARNING", r.stdout)

    def test_unknown_transport_listed_with_warning(self):
        root, home = self.tree({
            ".git": None,
            ".mcp.json": json.dumps({"mcpServers": {"weird": {"foo": 1}}}),
        })
        r = self.mcp_tool(home, "--harness", "claude", root)
        self.assertEqual(r.returncode, 0)
        self.assertIn("unknown", r.stdout)
        self.assertIn("WARNING", r.stdout)

    def test_full_refused_in_mcp_mode(self):
        root, home = self.tree({})
        r = self.mcp_tool(home, "--full", root)
        self.assertEqual(r.returncode, 2)
        self.assertIn("refused", r.stderr)

    def test_exit_1_when_nothing_found(self):
        root, home = self.tree({".git": None})
        r = self.mcp_tool(home, root)
        self.assertEqual(r.returncode, 1)
        self.assertIn("(no MCP servers resolve", r.stdout)

    def test_outline_and_diff(self):
        root, home = self.tree({
            ".git": None,
            ".mcp.json": json.dumps({"mcpServers": {"onlyA": {"command": "a"}}}),
        })
        r = self.mcp_tool(home, "--harness", "claude", "--outline", root)
        self.assertEqual(r.returncode, 0)
        self.assertIn("onlyA", r.stdout)
        r = self.mcp_tool(home, "--diff-harness", "claude", "codex", root)
        self.assertEqual(r.returncode, 0)
        self.assertIn("onlyA", r.stdout)
        self.assertIn("MCP servers", r.stdout)

    def test_unknown_harness_exit_2(self):
        root, home = self.tree({})
        r = self.mcp_tool(home, "--harness", "bogus", root)
        self.assertEqual(r.returncode, 2)

    def test_json_shape_and_no_secrets(self):
        root, home = self.tree({
            ".git": None,
            ".mcp.json": json.dumps({"mcpServers": {"s": {
                "command": "c", "args": ["a"], "env": {"K": self.SECRET}}}}),
        })
        r = self.mcp_tool(home, "--harness", "claude", "--json", root)
        doc = json.loads(r.stdout)
        s = doc["harnesses"][0]["servers"][0]
        for k in ("name", "source", "scope", "transport", "command",
                  "args", "url", "env", "headers", "via", "note"):
            self.assertIn(k, s)
        self.assertEqual(s["env"], {"K": "<set>"})
        self.assertNotIn(self.SECRET, r.stdout)
        self.assertIn("secret", doc["disclaimer"])


if __name__ == "__main__":
    unittest.main(verbosity=2)


class HooksTests(Base):
    CLAUDE_SETTINGS = json.dumps({
        "hooks": {
            "PreToolUse": [
                {"matcher": "Bash",
                 "hooks": [{"type": "command",
                            "command": "/hooks/guard.sh",
                            "timeout": 30}]},
                {"matcher": "Edit|Write",
                 "hooks": [{"type": "command",
                            "command": "/hooks/fmt.sh"}]},
            ],
            "SessionStart": [
                {"hooks": [{"type": "command", "command": "/hooks/ctx.sh"}]}
            ],
        }
    })

    def claude_struct(self, extra=None):
        s = {
            ".claude/settings.json": self.CLAUDE_SETTINGS,
        }
        if extra:
            s.update(extra)
        return s

    def hooks_json(self, root, home, *args):
        r = self.tool(home, "--hooks", "--json", *args, root)
        self.assertEqual(r.returncode, 0)
        hks = {}
        for h in json.loads(r.stdout)["harnesses"]:
            hks[h["harness"]] = h["hooks"]
        return hks, json.loads(r.stdout)

    def test_claude_three_scopes(self):
        root, home = self.tree(self.claude_struct({
            os.path.join(".claude", "settings.local.json"): json.dumps({
                "hooks": {"Stop": [{"hooks": [{"type": "command",
                                               "command": "/hooks/stop.sh"}]}]}}),
        }))
        os.makedirs(os.path.join(home, ".claude"), exist_ok=True)
        with open(os.path.join(home, ".claude", "settings.json"), "w") as f:
            f.write(json.dumps({
                "hooks": {"UserPromptSubmit": [
                    {"hooks": [{"type": "command",
                                "command": "/hooks/prompt.sh"}]}]}}))
        hks, _ = self.hooks_json(root, home, "--harness", "claude")
        cl = hks["claude"]
        self.assertEqual(len(cl), 5)
        scopes = {(h["event"], h["matcher"], h["scope"]) for h in cl}
        self.assertIn(("PreToolUse", "Bash", "project"), scopes)
        self.assertIn(("UserPromptSubmit", "", "user"), scopes)
        self.assertIn(("Stop", "", "local"), scopes)
        by_evt = {(h["event"], h["matcher"]): h for h in cl}
        self.assertEqual(by_evt[("PreToolUse", "Bash")]["command"], "/hooks/guard.sh")
        self.assertEqual(by_evt[("PreToolUse", "Bash")]["timeout"], 30)
        self.assertIsNone(by_evt[("PreToolUse", "Edit|Write")]["timeout"])
        for h in cl:
            self.assertIn("[documented]", h["via"])

    def test_claude_unknown_event_warned(self):
        root, home = self.tree({
            ".claude/settings.json": json.dumps({
                "hooks": {"MadeUpEvent": [
                    {"hooks": [{"type": "command", "command": "x"}]}]}}),
        })
        r = self.tool(home, "--hooks", "--harness", "claude", root)
        self.assertEqual(r.returncode, 0)
        self.assertIn("not a documented Claude Code event", r.stdout)

    def test_claude_entry_without_command_skipped(self):
        root, home = self.tree({
            ".claude/settings.json": json.dumps({
                "hooks": {"Stop": [{"hooks": [{"type": "prompt",
                                              "prompt": "check"}]}]}}),
        })
        r = self.tool(home, "--hooks", "--harness", "claude", "--json", root)
        self.assertEqual(r.returncode, 1)  # nothing resolvable
        h = json.loads(r.stdout)["harnesses"][0]
        self.assertEqual(h["hooks"], [])
        self.assertTrue(any("no 'command'" in w for w in h["warnings"]))

    def test_claude_malformed_json_warned(self):
        root, home = self.tree({".claude/settings.json": "{not json"})
        r = self.tool(home, "--hooks", "--harness", "claude", root)
        self.assertEqual(r.returncode, 1)
        self.assertIn("unreadable/not JSON", r.stdout)

    def test_codex_json_and_toml(self):
        root, home = self.tree({
            ".codex/hooks.json": json.dumps({
                "hooks": {"PreToolUse": [
                    {"matcher": "Bash",
                     "hooks": [{"type": "command", "command": "c.sh"}]}]}}),
            ".codex/config.toml": '[hooks]\nPostToolUse = [{matcher = "*", hooks = [{type = "command", command = "t.sh"}]}]\n',
        })
        r = self.tool(home, "--hooks", "--harness", "codex", "--json", root)
        self.assertEqual(r.returncode, 0)
        cx = json.loads(r.stdout)["harnesses"][0]
        self.assertEqual(len(cx["hooks"]), 2)
        self.assertTrue(all(h["scope"] == "project" for h in cx["hooks"]))
        self.assertTrue(any("trusted projects" in n for n in cx["notes"]))
        self.assertTrue(any("borrows the Claude hook shape" in h["via"]
                            for h in cx["hooks"]))

    def test_codex_no_proj_dir_note(self):
        root, home = self.tree({})
        r = self.tool(home, "--hooks", "--harness", "codex", root)
        self.assertEqual(r.returncode, 1)
        self.assertIn("no .codex/ dir", r.stdout)

    def test_cursor_reads_own_and_claude_hooks(self):
        root, home = self.tree({
            ".cursor/hooks.json": json.dumps({
                "hooks": {"PreToolUse": [
                    {"matcher": "Shell",
                     "hooks": [{"type": "command", "command": "cur.sh"}]}]}}),
            ".claude/settings.json": self.CLAUDE_SETTINGS,
        })
        hks, _ = self.hooks_json(root, home, "--harness", "cursor")
        cur = hks["cursor"]
        self.assertEqual(len(cur), 3 + 1)  # 1 own + 3 via claude
        via_claude = [h for h in cur if "via claude" in h["scope"]]
        self.assertEqual(len(via_claude), 3)
        self.assertTrue(all("Bash->Shell" in h["note"] for h in via_claude))
        own = [h for h in cur if h["scope"] == "project"]
        self.assertEqual(own[0]["command"], "cur.sh")

    def test_copilot_repo_and_user_hooks(self):
        root, home = self.tree({
            ".git": None,
            ".github/hooks/router.json": json.dumps({
                "version": 1,
                "hooks": {"postToolUse": [
                    {"matcher": "edit|create",
                     "bash": ".github/hooks/scripts/test-router.sh",
                     "timeoutSec": 60}]}}),
        })
        os.makedirs(os.path.join(home, ".copilot", "hooks"), exist_ok=True)
        with open(os.path.join(home, ".copilot", "hooks", "u.json"), "w") as f:
            f.write(json.dumps({
                "hooks": {"agentStop": [
                    {"command": "notify.sh"}]}}))  # no version -> warning
        hks, full = self.hooks_json(root, home, "--harness", "copilot")
        cp = hks["copilot"]
        self.assertEqual(len(cp), 2)
        scopes = {h["scope"] for h in cp}
        self.assertEqual(scopes, {"repo", "user"})
        repo = [h for h in cp if h["scope"] == "repo"][0]
        self.assertEqual(repo["event"], "postToolUse")
        self.assertEqual(repo["matcher"], "edit|create")
        self.assertEqual(repo["timeout"], 60)
        self.assertTrue(any("version" in w for w in
                            full["harnesses"][0]["warnings"]))
    def test_copilot_inline_settings_hooks(self):
        # the official GitHub Copilot hooks reference documents inline
        # "hooks" blocks in settings.json (repo copilot settings, user
        # copilot settings, and a cross-tool read of .claude/settings.json)
        root, home = self.tree({
            ".git": None,
            ".github/copilot/settings.json": json.dumps({
                "hooks": {"sessionStart": [
                    {"command": "./scripts/init.sh"}]}}),
            ".github/copilot/settings.local.json": json.dumps({
                "hooks": {"preToolUse": [
                    {"matcher": "^bash$", "bash": "deny.sh"}]}}),
            ".claude/settings.json": json.dumps({
                "hooks": {"userPromptSubmitted": [
                    {"command": "log.sh"}]}}),
        })
        os.makedirs(os.path.join(home, ".copilot"), exist_ok=True)
        with open(os.path.join(home, ".copilot", "settings.json"), "w") as f:
            f.write(json.dumps({"hooks": {"sessionEnd": [
                {"bash": "bye.sh", "timeout": 45}]}}))  # timeout alias
        hks, full = self.hooks_json(root, home, "--harness", "copilot")
        cp = hks["copilot"]
        by_scope = {h["scope"] for h in cp}
        self.assertEqual(by_scope, {"repo", "repo-inline", "user-inline"})
        inline = [h for h in cp if h["scope"] == "repo"][0]
        self.assertEqual(inline["event"], "sessionStart")
        self.assertEqual(inline["command"], "./scripts/init.sh")
        local_inline = [h for h in cp if h["matcher"] == "^bash$"][0]
        self.assertEqual(local_inline["scope"], "repo")
        claude_x = [h for h in cp if h["scope"] == "repo-inline"][0]
        self.assertEqual(claude_x["event"], "userPromptSubmitted")
        user_inline = [h for h in cp if h["scope"] == "user-inline"][0]
        self.assertEqual(user_inline["event"], "sessionEnd")
        self.assertEqual(user_inline["timeout"], 45)  # timeout alias honored
        # inline blocks carry no version field: no version warnings allowed
        self.assertFalse(any("version" in w for w in
                             full["harnesses"][0]["warnings"]))

    def test_copilot_disable_all_hooks_settings(self):
        # disableAllHooks at settings.json level skips every hook
        root, home = self.tree({
            ".git": None,
            ".github/copilot/settings.json": json.dumps({
                "disableAllHooks": True,
                "hooks": {"sessionStart": [{"command": "x.sh"}]}}),
            ".github/hooks/a.json": json.dumps({
                "version": 1,
                "hooks": {"sessionStart": [{"command": "y.sh"}]}}),
        })
        hks, full = self.hooks_json(root, home, "--harness", "copilot")
        cp = hks["copilot"]
        self.assertFalse(any(h["scope"] == "repo-inline" for h in cp))
        self.assertTrue(any("disableAllHooks=true" in n
                            and "every hook" in n
                            for n in full["harnesses"][0]["notes"]))

    def test_copilot_disable_all_hooks_single_file(self):
        # disableAllHooks in one hooks-dir file skips only that file
        root, home = self.tree({
            ".git": None,
            ".github/hooks/off.json": json.dumps({
                "version": 1, "disableAllHooks": True,
                "hooks": {"sessionStart": [{"command": "x.sh"}]}}),
            ".github/hooks/on.json": json.dumps({
                "version": 1,
                "hooks": {"sessionStart": [{"command": "y.sh"}]}}),
        })
        hks, full = self.hooks_json(root, home, "--harness", "copilot")
        cp = hks["copilot"]
        self.assertEqual([h["command"] for h in cp], ["y.sh"])
        self.assertTrue(any("disableAllHooks=true" in n
                            for n in full["harnesses"][0]["notes"]))

    def test_copilot_exec_form_and_http_skipped(self):
        # "exec"+args runs the executable directly, no shell; "http" hooks
        # are not shell commands and are skipped with a warning
        root, home = self.tree({
            ".git": None,
            ".github/hooks/x.json": json.dumps({
                "version": 1,
                "hooks": {
                    "preToolUse": [
                        {"exec": "python3", "args": ["guard.py", "--fast"],
                         "matcher": "^bash$"}],
                    "sessionStart": [
                        {"type": "http", "url": "https://example.com/hook"}],
                    "sessionEnd": [
                        {"powershell": "bye.ps1"}]}}),
        })
        hks, full = self.hooks_json(root, home, "--harness", "copilot")
        cp = hks["copilot"]
        self.assertEqual(len(cp), 1)
        self.assertEqual(cp[0]["command"], "python3 guard.py --fast")
        self.assertIn("no shell", cp[0]["note"])
        warns = full["harnesses"][0]["warnings"]
        self.assertTrue(any("http" in w and "not a shell command" in w
                            for w in warns))
        self.assertTrue(any("powershell-only" in w for w in warns))

    def test_copilot_home_env_redirects_user_hooks(self):
        # COPILOT_HOME set -> user hooks live in $COPILOT_HOME/hooks
        root, home = self.tree({".git": None})
        chome = tempfile.mkdtemp(prefix="as-chome-")
        self._tmp.append(chome)
        os.makedirs(os.path.join(chome, "hooks"))
        with open(os.path.join(chome, "hooks", "u.json"), "w") as f:
            f.write(json.dumps({"version": 1, "hooks": {"sessionStart": [
                {"command": "chome.sh"}]}}))
        env = dict(os.environ, HOME=home, COPILOT_HOME=chome)
        r = subprocess.run([sys.executable, TOOL, "--hooks", "--json",
                            "--harness", "copilot", root],
                           capture_output=True, text=True, env=env)
        self.assertEqual(r.returncode, 0)
        cp = json.loads(r.stdout)["harnesses"][0]["hooks"]
        self.assertEqual(len(cp), 1)
        self.assertEqual(cp[0]["command"], "chome.sh")
        self.assertEqual(cp[0]["scope"], "user")
        self.assertIn("COPILOT_HOME", cp[0]["via"])
        notes = json.loads(r.stdout)["harnesses"][0]["notes"]
        self.assertTrue(any("/etc/github-copilot/policy.d" in n
                            for n in notes))

    def test_gemini_hooks_documented_not_modeled(self):
        # the stale v1.2.0 note said no hook mechanism existed; the Gemini
        # CLI hooks reference (geminicli.com/docs/hooks/) is real — the
        # honest note now says documented-but-not-modeled.
        root, home = self.tree({})
        r = self.tool(home, "--hooks", "--harness", "gemini", "--json", root)
        self.assertEqual(r.returncode, 1)
        g = json.loads(r.stdout)["harnesses"][0]
        self.assertEqual(g["hooks"], [])
        self.assertTrue(any("HAS a documented hook mechanism" in n
                            for n in g["notes"]))
        self.assertTrue(any("geminicli.com/docs/hooks/" in n
                            for n in g["notes"]))
        self.assertTrue(any("hooktest" in n for n in g["notes"]))

    def test_exit_codes_and_text(self):
        root, home = self.tree(self.claude_struct())
        r = self.tool(home, "--hooks", "--harness", "claude", root)
        self.assertEqual(r.returncode, 0)
        self.assertIn("NEVER EXECUTED", r.stdout)
        self.assertIn("== claude (hooks) ==", r.stdout)
        self.assertIn("command: /hooks/guard.sh", r.stdout)
        r2 = self.tool(home, "--hooks", "--harness", "gemini", root)
        self.assertEqual(r2.returncode, 1)
        self.assertIn("(no hooks resolve for this directory)", r2.stdout)

    def test_mcp_and_hooks_exclusive(self):
        root, home = self.tree({})
        r = self.tool(home, "--mcp", "--hooks", root)
        self.assertEqual(r.returncode, 2)
        self.assertIn("mutually exclusive", r.stderr)

    def test_diff_harness_hooks(self):
        root, home = self.tree(self.claude_struct())
        r = self.tool(home, "--hooks", "--diff-harness", "claude", "cursor",
                      root)
        self.assertEqual(r.returncode, 0)
        self.assertIn("only in claude (hooks):", r.stdout)
        self.assertIn("only in cursor (hooks):", r.stdout)

    def test_outline_mode(self):
        root, home = self.tree(self.claude_struct())
        r = self.tool(home, "--hooks", "--harness", "claude", "--outline", root)
        self.assertEqual(r.returncode, 0)
        self.assertIn("PreToolUse  ['Bash', project]", r.stdout)
        self.assertNotIn("command: /hooks/guard.sh", r.stdout)

    def test_json_shape(self):
        root, home = self.tree(self.claude_struct())
        r = self.tool(home, "--hooks", "--harness", "claude", "--json", root)
        body = json.loads(r.stdout)
        self.assertEqual(body["mode"], "hooks")
        self.assertEqual(body["version"], "1.5.0")
        self.assertIn("never executed", body["disclaimer"])
        hk = body["harnesses"][0]["hooks"][0]
        for k in ("event", "matcher", "source", "scope", "command",
                  "timeout", "via", "note"):
            self.assertIn(k, hk)

    def test_never_executes_hooks(self):
        # A hook command that would create a file if executed must not run.
        tmpdir = tempfile.mkdtemp()
        self._tmp.append(tmpdir)
        marker = os.path.join(tmpdir, "pwned")
        root, home = self.tree({
            ".claude/settings.json": json.dumps({
                "hooks": {"SessionStart": [
                    {"hooks": [{"type": "command",
                                "command": "touch %s" % marker}]}]}}),
        })
        r = self.tool(home, "--hooks", "--harness", "claude", root)
        self.assertEqual(r.returncode, 0)
        self.assertFalse(os.path.exists(marker))


class PermissionsTests(Base):
    def claude_settings(self, home, root):
        os.makedirs(os.path.join(home, ".claude"), exist_ok=True)
        os.makedirs(os.path.join(root, ".claude"), exist_ok=True)
        with open(os.path.join(home, ".claude", "settings.json"), "w") as f:
            json.dump({"permissions": {
                "allow": ["Bash(git status)"],
                "deny": ["Bash(rm -rf *)"]}}, f)
        with open(os.path.join(root, ".claude", "settings.json"), "w") as f:
            json.dump({"permissions": {"ask": ["WebFetch"]}}, f)
        with open(os.path.join(root, ".claude", "settings.local.json"), "w") as f:
            json.dump({"permissions": {"allow": ["Read(./notes/**)"]}}, f)

    def perm_json(self, home, args):
        r = self.tool(home, "--permissions", "--json", *args)
        self.assertEqual(r.returncode, 0)
        body = json.loads(r.stdout)
        return {h["harness"]: h for h in body["harnesses"]}

    def test_claude_three_scopes_merge(self):
        root, home = self.tree({".git": None})
        self.claude_settings(home, root)
        h = self.perm_json(home, ["--harness", "claude", root])["claude"]
        by_scope = {}
        for rule in h["rules"]:
            by_scope.setdefault(rule["scope"], []).append(
                (rule["kind"], rule["rule"]))
        # rules emit in evaluation order: deny, ask, allow
        self.assertEqual(by_scope["user"],
                         [("deny", "Bash(rm -rf *)"),
                          ("allow", "Bash(git status)")])
        self.assertEqual(by_scope["project"], [("ask", "WebFetch")])
        self.assertEqual(by_scope["local"], [("allow", "Read(./notes/**)")])
        self.assertTrue(any("deny" in n and "allow" in n for n in h["notes"]))

    def test_claude_no_settings_exit_1(self):
        root, home = self.tree({".git": None})
        r = self.tool(home, "--permissions", "--harness", "claude", root)
        self.assertEqual(r.returncode, 1)
        self.assertIn("no permission rules found", r.stdout)

    def test_claude_malformed_json_forgiven(self):
        root, home = self.tree({".git": None})
        os.makedirs(os.path.join(home, ".claude"), exist_ok=True)
        with open(os.path.join(home, ".claude", "settings.json"), "w") as f:
            f.write("{not json")
        r = self.tool(home, "--permissions", "--harness", "claude", root)
        self.assertEqual(r.returncode, 1)  # nothing resolved, no crash

    def test_codex_profiles_and_context(self):
        root, home = self.tree({".git": None})
        os.makedirs(os.path.join(home, ".codex"), exist_ok=True)
        with open(os.path.join(home, ".codex", "config.toml"), "w") as f:
            f.write('default_permissions = "hardened"\n'
                    'sandbox_mode = "workspace-write"\n'
                    'approval_policy = "on-request"\n'
                    '[permissions.hardened.filesystem]\n'
                    '":project_roots" = { "." = "write", "**/*.env" = "none" }\n'
                    '"/home/u/.ssh" = "none"\n')
        os.makedirs(os.path.join(root, ".codex"), exist_ok=True)
        with open(os.path.join(root, ".codex", "config.toml"), "w") as f:
            f.write('default_permissions = "proj"\n')
        h = self.perm_json(home, ["--harness", "codex", root])["codex"]
        rules = {(r["scope"], r["kind"], r["rule"]) for r in h["rules"]}
        self.assertIn(("profile:hardened", "fs",
                       ":project_roots . => write"), rules)
        self.assertIn(("profile:hardened", "fs",
                       ":project_roots **/*.env => none"), rules)
        self.assertIn(("profile:hardened", "fs",
                       "/home/u/.ssh => none"), rules)
        ctx = {(c["name"], c["value"]) for c in h["context"]}
        self.assertIn(("default_permissions", "hardened"), ctx)
        self.assertIn(("sandbox_mode", "workspace-write"), ctx)
        self.assertIn(("approval_policy", "on-request"), ctx)
        vias = [c["via"] for c in h["context"]] + \
            [r["via"] for r in h["rules"]]
        self.assertTrue(any("trusted" in v for v in vias))

    def test_codex_flat_filesystem_and_granular_approval(self):
        root, home = self.tree({".git": None})
        os.makedirs(os.path.join(home, ".codex"), exist_ok=True)
        with open(os.path.join(home, ".codex", "config.toml"), "w") as f:
            f.write('approval_policy = { sandbox_approval = true, '
                    'rules = true }\n'
                    '[permissions.ci.filesystem]\n'
                    '"/srv/data" = "read"\n')
        h = self.perm_json(home, ["--harness", "codex", root])["codex"]
        self.assertIn(("profile:ci", "fs", "/srv/data => read"),
                      {(r["scope"], r["kind"], r["rule"]) for r in h["rules"]})
        ap = [c for c in h["context"] if c["name"] == "approval_policy"][0]
        self.assertIn("sandbox_approval", ap["value"])

    def test_gemini_core_allowlist_and_sandbox(self):
        root, home = self.tree({".git": None})
        os.makedirs(os.path.join(home, ".gemini"), exist_ok=True)
        with open(os.path.join(home, ".gemini", "settings.json"), "w") as f:
            json.dump({"tools": {"core": ["ReadFileTool"],
                                 "sandbox": "docker"}}, f)
        os.makedirs(os.path.join(root, ".gemini"), exist_ok=True)
        with open(os.path.join(root, ".gemini", "settings.json"), "w") as f:
            json.dump({"coreTools": ["ShellTool(gh pr view)"]}, f)
        h = self.perm_json(home, ["--harness", "gemini", root])["gemini"]
        rules = {(r["scope"], r["rule"]) for r in h["rules"]}
        self.assertIn(("global", "ReadFileTool"), rules)
        self.assertIn(("project", "ShellTool(gh pr view)"), rules)
        ctx = {(c["name"], c["value"]) for c in h["context"]}
        self.assertIn(("tools.sandbox", "docker"), ctx)

    def test_cursor_copilot_unmodeled(self):
        root, home = self.tree({".git": None})
        for name in ("cursor", "copilot"):
            r = self.tool(home, "--permissions", "--harness", name, root)
            self.assertEqual(r.returncode, 1)
            self.assertIn("no documented static allow/deny", r.stdout)

    def test_exit_codes(self):
        root, home = self.tree({".git": None})
        r = self.tool(home, "--permissions", root)
        self.assertEqual(r.returncode, 1)  # nothing anywhere
        self.claude_settings(home, root)
        r = self.tool(home, "--permissions", root)
        self.assertEqual(r.returncode, 0)
        r = self.tool(home, "--permissions", "/nonexistent-dir-xyz")
        self.assertEqual(r.returncode, 2)

    def test_modes_mutually_exclusive(self):
        root, home = self.tree({".git": None})
        for flags in (["--permissions", "--hooks"],
                      ["--permissions", "--mcp"]):
            r = self.tool(home, *flags, root)
            self.assertEqual(r.returncode, 2)
            self.assertIn("mutually exclusive", r.stderr)

    def test_outline_counts(self):
        root, home = self.tree({".git": None})
        self.claude_settings(home, root)
        r = self.tool(home, "--permissions", "--outline", "--harness",
                      "claude", root)
        self.assertEqual(r.returncode, 0)
        self.assertIn("4 rule(s)", r.stdout)

    def test_diff_harness_permissions(self):
        root, home = self.tree({".git": None})
        self.claude_settings(home, root)
        r = self.tool(home, "--permissions", "--diff-harness", "claude",
                      "gemini", root)
        self.assertEqual(r.returncode, 0)
        self.assertIn("permission rules", r.stdout)
        self.assertIn("Bash(git status)", r.stdout)

    def test_rules_never_executed(self):
        # A rule naming a command that would create a file must not run.
        tmpdir = tempfile.mkdtemp()
        self._tmp.append(tmpdir)
        marker = os.path.join(tmpdir, "pwned")
        root, home = self.tree({".git": None})
        os.makedirs(os.path.join(home, ".claude"), exist_ok=True)
        with open(os.path.join(home, ".claude", "settings.json"), "w") as f:
            json.dump({"permissions": {"deny": ["Bash(touch %s)" % marker]}},
                      f)
        r = self.tool(home, "--permissions", "--harness", "claude", root)
        self.assertEqual(r.returncode, 0)
        self.assertIn(marker, r.stdout)  # shown verbatim
        self.assertFalse(os.path.exists(marker))  # never executed

    def test_json_shape(self):
        root, home = self.tree({".git": None})
        self.claude_settings(home, root)
        r = self.tool(home, "--permissions", "--json", root)
        self.assertEqual(r.returncode, 0)
        body = json.loads(r.stdout)
        self.assertEqual(body["mode"], "permissions")
        self.assertIn("not runtime truth", body["disclaimer"])
        cl = [h for h in body["harnesses"]
              if h["harness"] == "claude"][0]
        rule = cl["rules"][0]
        for k in ("kind", "rule", "source", "scope", "via", "tag", "note"):
            self.assertIn(k, rule)


class OpencodeTests(Base):
    def test_first_match_wins(self):
        # AGENTS.md shadows CLAUDE.md at the same level [documented]
        root, home = self.tree({
            "AGENTS.md": "# agents\n", "CLAUDE.md": "# claude\n",
        })
        r = self.tool(home, "--harness", "opencode", "--json", root)
        self.assertEqual(r.returncode, 0)
        c = json.loads(r.stdout)["harnesses"][0]
        paths = [x["path"] for x in c["contributors"]]
        self.assertEqual(paths, [os.path.join(root, "AGENTS.md")])
        self.assertTrue(any("shadows" in x["via"] for x in c["contributors"]))

    def test_claude_only_level_used(self):
        root, home = self.tree({"sub/CLAUDE.md": "# claude-only\n"})
        r = self.tool(home, "--harness", "opencode", "--json",
                      os.path.join(root, "sub"))
        self.assertEqual(r.returncode, 0)
        c = json.loads(r.stdout)["harnesses"][0]
        paths = [x["path"] for x in c["contributors"]]
        self.assertIn(os.path.join(root, "sub", "CLAUDE.md"), paths)

    def test_global_precedence_over_compat(self):
        root, home = self.tree({"AGENTS.md": "# root\n"})
        os.makedirs(os.path.join(home, ".config", "opencode"))
        open(os.path.join(home, ".config", "opencode", "AGENTS.md"),
             "w").write("# global\n")
        os.makedirs(os.path.join(home, ".claude"))
        open(os.path.join(home, ".claude", "CLAUDE.md"), "w").write("# compat\n")
        r = self.tool(home, "--harness", "opencode", "--json", root)
        c = json.loads(r.stdout)["harnesses"][0]
        paths = [x["path"] for x in c["contributors"]]
        self.assertIn(os.path.join(home, ".config", "opencode", "AGENTS.md"),
                      paths)
        self.assertNotIn(os.path.join(home, ".claude", "CLAUDE.md"), paths)
        self.assertTrue(any("takes precedence" in n for n in c["notes"]))

    def test_compat_fallback(self):
        root, home = self.tree({"AGENTS.md": "# root\n"})
        os.makedirs(os.path.join(home, ".claude"))
        open(os.path.join(home, ".claude", "CLAUDE.md"), "w").write("# compat\n")
        r = self.tool(home, "--harness", "opencode", "--json", root)
        c = json.loads(r.stdout)["harnesses"][0]
        paths = [x["path"] for x in c["contributors"]]
        self.assertIn(os.path.join(home, ".claude", "CLAUDE.md"), paths)
        self.assertTrue(any("compat fallback" in x["via"]
                            for x in c["contributors"]))

    def test_compat_disabled_via_env(self):
        root, home = self.tree({"AGENTS.md": "# root\n"})
        os.makedirs(os.path.join(home, ".claude"))
        open(os.path.join(home, ".claude", "CLAUDE.md"), "w").write("# compat\n")
        env = dict(os.environ, HOME=home, OPENCODE_DISABLE_CLAUDE_CODE="1")
        r = subprocess.run([sys.executable, TOOL, "--harness", "opencode",
                            "--json", root], capture_output=True, text=True,
                           env=env)
        c = json.loads(r.stdout)["harnesses"][0]
        paths = [x["path"] for x in c["contributors"]]
        self.assertNotIn(os.path.join(home, ".claude", "CLAUDE.md"), paths)
        self.assertTrue(any("disabled via env" in n for n in c["notes"]))

    def test_instructions_entries(self):
        root, home = self.tree({
            "opencode.json": json.dumps({"instructions": [
                "docs/team.md", "pkg/*/*.md",
                "https://example.com/remote.md", "missing-file.md"]}),
            "docs/team.md": "# team\n",
            "pkg/a/rules.md": "# a\n", "pkg/b/rules.md": "# b\n",
        })
        r = self.tool(home, "--harness", "opencode", "--json", root)
        c = json.loads(r.stdout)["harnesses"][0]
        paths = [x["path"] for x in c["contributors"]]
        self.assertIn(os.path.join(root, "docs", "team.md"), paths)
        self.assertIn(os.path.join(root, "pkg", "a", "rules.md"), paths)
        self.assertIn(os.path.join(root, "pkg", "b", "rules.md"), paths)
        self.assertTrue(any("remote" in w and "NOT fetched" in w
                            for w in c["warnings"]))
        self.assertTrue(any("matched nothing" in w for w in c["warnings"]))

    def test_upward_opencode_json_discovery(self):
        root, home = self.tree({
            "opencode.json": json.dumps({"instructions": ["rules.md"]}),
            "rules.md": "# rules\n", "deep/deeper/": None,
        })
        r = self.tool(home, "--harness", "opencode", "--json",
                      os.path.join(root, "deep", "deeper"))
        c = json.loads(r.stdout)["harnesses"][0]
        paths = [x["path"] for x in c["contributors"]]
        self.assertIn(os.path.join(root, "rules.md"), paths)

    def test_no_import_expansion(self):
        # opencode does not parse @file references [documented] — the
        # referenced file must NOT be followed.
        root, home = self.tree({
            "AGENTS.md": "See @other.md for details\n", "other.md": "# o\n",
        })
        r = self.tool(home, "--harness", "opencode", "--json", root)
        c = json.loads(r.stdout)["harnesses"][0]
        paths = [x["path"] for x in c["contributors"]]
        self.assertEqual(paths, [os.path.join(root, "AGENTS.md")])

    def test_empty_dirs_exit_1(self):
        root, home = self.tree({"sub/": None})
        r = self.tool(home, "--harness", "opencode",
                      os.path.join(root, "sub"))
        self.assertEqual(r.returncode, 1)

    def test_diff_harness(self):
        root, home = self.tree({"AGENTS.md": "# root\n",
                                "CLAUDE.md": "# claude-only\n"})
        r = self.tool(home, "--diff-harness", "claude", "opencode",
                      "--json", root)
        self.assertEqual(r.returncode, 0)
        body = json.loads(r.stdout)
        self.assertEqual(body["what"], "instruction files")
        # claude merges upward (both files); opencode takes first match
        self.assertIn(os.path.join(root, "CLAUDE.md"), body["only_in_a"])


class OpencodeMcpTests(Base):
    MCP_CFG = {
        "mcp": {"servers": {
            "fs": {"type": "local",
                   "command": ["npx", "-y", "server-fs", "/tmp"],
                   "environment": {"MY_VAR": "value",
                                   "KEY": "{env:MY_VAR}"}},
            "ctx": {"type": "remote",
                    "url": "https://mcp.example.com/mcp?token=SECRET",
                    "headers": {"X-Auth": "{env:TOKEN}"}},
            "old": {"type": "local", "command": ["old-bin"],
                    "disabled": True},
        }}}

    def test_local_and_remote_servers(self):
        root, home = self.tree(
            {"opencode.json": json.dumps(self.MCP_CFG)})
        r = self.tool(home, "--mcp", "--harness", "opencode", "--json", root)
        self.assertEqual(r.returncode, 0)
        ss = {s["name"]: s
              for s in json.loads(r.stdout)["harnesses"][0]["servers"]}
        self.assertEqual(ss["fs"]["transport"], "stdio")
        self.assertEqual(ss["fs"]["command"], "npx")
        self.assertEqual(ss["fs"]["args"], ["-y", "server-fs", "/tmp"])
        self.assertEqual(ss["ctx"]["transport"], "http")
        self.assertIn("never probed", ss["ctx"]["note"])

    def test_secret_safety_structural(self):
        root, home = self.tree(
            {"opencode.json": json.dumps(self.MCP_CFG)})
        r = self.tool(home, "--mcp", "--harness", "opencode", root)
        self.assertNotIn("token=SECRET", r.stdout)      # query stripped
        self.assertNotIn('"MY_VAR": "value"', r.stdout)  # values not shown
        self.assertIn("MY_VAR", r.stdout)               # names shown
        self.assertIn("X-Auth", r.stdout)

    def test_disabled_flag(self):
        root, home = self.tree(
            {"opencode.json": json.dumps(self.MCP_CFG)})
        r = self.tool(home, "--mcp", "--harness", "opencode", "--json", root)
        ss = {s["name"]: s
              for s in json.loads(r.stdout)["harnesses"][0]["servers"]}
        self.assertIn("disabled", ss["old"]["note"])

    def test_project_replaces_global(self):
        root, home = self.tree(
            {"opencode.json": json.dumps(
                {"mcp": {"servers": {
                    "dup": {"type": "remote",
                            "url": "https://proj.example.com/mcp"}}}})})
        gcfg = os.path.join(home, ".config", "opencode", "opencode.json")
        os.makedirs(os.path.dirname(gcfg))
        with open(gcfg, "w") as f:
            json.dump({"mcp": {"servers": {
                "dup": {"type": "local", "command": ["glob-bin"]}}}}, f)
        r = self.tool(home, "--mcp", "--harness", "opencode", "--json", root)
        body = json.loads(r.stdout)["harnesses"][0]
        self.assertTrue(any("replaces the entire" in n for n in body["notes"]))

    def test_legacy_shape_warning(self):
        root, home = self.tree(
            {"opencode.json": json.dumps(
                {"mcp": {"fs": {"type": "local",
                                "command": ["old-bin"]}}})})
        r = self.tool(home, "--mcp", "--harness", "opencode", "--json", root)
        body = json.loads(r.stdout)["harnesses"][0]
        self.assertEqual(body["servers"], [])
        self.assertTrue(any("pre-v2" in w for w in body["warnings"]))

    def test_unknown_type_warning(self):
        root, home = self.tree(
            {"opencode.json": json.dumps(
                {"mcp": {"servers": {"weird": {"foo": 1}}}})})
        r = self.tool(home, "--mcp", "--harness", "opencode", "--json", root)
        body = json.loads(r.stdout)["harnesses"][0]
        self.assertEqual(body["servers"], [])
        self.assertTrue(any("unknown/absent `type`" in w
                            for w in body["warnings"]))


class OpencodePermissionsTests(Base):
    def test_permissions_array(self):
        root, home = self.tree(
            {"opencode.json": json.dumps({"permissions": [
                {"action": "ctx_*", "resource": "*", "effect": "deny"},
                {"action": "opencode_read_mcp_resource",
                 "resource": "docs:*", "effect": "allow"}]})})
        r = self.tool(home, "--permissions", "--harness", "opencode",
                      "--json", root)
        self.assertEqual(r.returncode, 0)
        rules = json.loads(r.stdout)["harnesses"][0]["rules"]
        self.assertEqual(len(rules), 2)
        self.assertIn("action=ctx_* resource=* effect=deny",
                      [x["rule"] for x in rules])
        self.assertTrue(all("opencode.ai/v2/docs/mcp-servers" in x["via"]
                            for x in rules))
        self.assertTrue(any("no merge order assumed" in x["note"]
                            for x in rules))

    def test_no_permissions_note(self):
        root, home = self.tree({"opencode.json": json.dumps({"mcp": {}})})
        r = self.tool(home, "--permissions", "--harness", "opencode",
                      "--json", root)
        body = json.loads(r.stdout)["harnesses"][0]
        self.assertEqual(body["rules"], [])
        self.assertTrue(any("no `permissions` array" in n
                            for n in body["notes"]))
