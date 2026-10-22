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
        self.assertIn("1.0.0", r.stdout)
        r = self.tool(home, "--list-harnesses")
        for h in ("codex", "claude", "cursor", "copilot", "gemini"):
            self.assertIn(h, r.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
