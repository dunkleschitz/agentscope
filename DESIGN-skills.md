# agentscope `--skills` mode — hazard-first design review

Date: 2026-10-23. Gate satisfied: per-harness official-doc verification
(`toolsmith/scratch/agentscope-skills-verification.md`, RULES_VERIFIED
2026-10-23). Building as agentscope v1.7.0.

Question answered: "which skills will harness X actually SEE for
directory Y?" (not "which skills will it load").

## Hazard 1 — the October 22 rejection is real, and this design answers it

The Oct 22 pass rejected this exact angle: "Skills load on-demand by
description match, not per-directory like instructions/MCP/hooks/
permissions — an 'effective set' would overstate loading semantics."

The design takes that objection seriously and answers it structurally,
not dismissively:

- The mode never claims an effective *loaded* set. Every report carries
  a **loading-model note**: harnesses discover skills from on-disk
  locations (that's what agentscope resolves), but loading is
  on-demand/gated — the harness lists name+description at startup and
  loads the SKILL.md body on description match (progressive disclosure,
  per the Agent Skills spec and the harness docs). What the tool reports
  is the **visible (discoverable) set** per harness per directory.
- The banner names the mode's claim honestly: "skills the harness can
  see", never "skills the harness will use". The Oct 22 rejection's
  warning ("an 'effective set' would overstate loading semantics") is
  quoted verbatim in the Limitations note and in the README, so the
  failure mode is visible to the reader, not buried.
- This is why the tool is worth building despite the rejection: the
  installer/tooling layer (APM, `npx skills`, marketplaces) answers
  "where do I PUT a skill", never "what does harness X SEE from
  directory Y". The discovery path is where people actually get stuck
  ("I installed the skill, the harness doesn't offer it") — usually a
  wrong-folder or an invalid-frontmatter problem, both of which this
  mode diagnoses deterministically.

## Hazard 2 — invalid skills must not be silently skipped

Per the verification doc: invalid frontmatter (name ≠ folder name,
missing/empty description) is a REPORT-AS-INVALID verdict, never a
silent skip. The harm case is a skill that silently doesn't load —
exactly the debug scenario. Verdicts: VALID / INVALID (reason carried)
 / DUPLICATE-NAME. INVALID skills still exit 0 (they're a finding, not
a usage error).

## Hazard 3 — frontmatter parsing must not pretend to be YAML

Full YAML is out of scope (stdlib-only, heuristic tool). The parser
reads only: `name` (first-line scalar), `description` (first-line
scalar, multi-line folded detection → present), and the presence of
`allowed-tools` / `metadata` / `opencode/*` keys (listed, not judged).
Anything ambiguous is "unparseable → INVALID (frontmatter unreadable)",
never a silent skip, never a guessed name. Description >1024 chars or
name outside the spec charset → INVALID per spec constraints, with the
spec rule quoted as evidence.

## Hazard 4 — same-name collisions need per-harness precedence

- claude: enterprise > personal > project [documented-via-guides];
  reported as the winner with a note naming the source tier.
- antigravity: workspace > global (deepwiki).
- opencode: v2 publishes a precedence order between sources
  [documented]; v1 derives ID from frontmatter `name`.
- Everything else: same-name collisions surface as DUPLICATE-NAME with
  all claimants listed; the tie-break rule is explicit in output as
  [inferred] where the docs are silent.

## Hazard 5 — the Antigravity global-path conflict

Two global candidates (`~/.gemini/config/skills/` recent, citing
official docs, vs `~/.gemini/antigravity/skills/` older). The tool
models BOTH, each tagged with its evidence age, and emits a standing
note: "global path unresolved — verify against the docs". This is the
honesty model working as designed: a conflict in the evidence is
reported as a conflict, not resolved by fiat.

## Hazard 6 — opencode v1 vs v2 skill-ID derivation

v1: skill ID = frontmatter `name` [documented]. v2: skill ID derived
from the PATH, not frontmatter `name` [documented]; v2 also publishes
source precedence and supports configured `skills.urls` HTTP catalogs.
When the tool sees an opencode 2 config layout, it switches to
path-derived IDs and adds a stale-model note; when it doesn't, it stays
on v1 semantics with a note that v2 differs. Remotes are NEVER fetched —
listed as (remote, unfetched) entries only.

## Hazard 7 — never execute, never fetch

Skills may contain `scripts/` directories. agentscope NEVER descends
into them, never executes anything, never touches the network (the
`skills.urls` find above makes this a hard rule, with a dedicated test
asserting the resolution code contains no socket/urllib usage for the
skills path — mirroring gowho's no-network test).

## Hazard 8 — frontmatter gates are "present but gated", not broken

`disable-model-invocation`, Codex `policy.allow_implicit_invocation`
gating, Gemini's `activate_skill` interactive confirmation — all surface
as notes on the skill ("present, harness-gated"), never as INVALID.
Gates are modeled, not judged.

## Contract

- Flags: `--skills` (mutually exclusive with --mcp/--hooks/
  --permissions); `--full` ignored (skill bodies stay on disk — same
  reasoning as hooks/permissions ignoring it).
- Exit codes: 0 skills found (or INVALID findings present) / 1 none /
  2 usage. Same family contract.
- `--diff-harness A B DIR` on (skill-id, scope, source-dir) identity.
- Strictly read-only. Rules tagged [documented]/[inferred]; banner with
  RULES_VERIFIED.
- Renderer: text + outline + JSON, matching the mode-family pattern.

## Per-harness discovery map (from the verification doc)

- claude: `~/.claude/skills/` (personal), `<root>/.claude/skills/`
  (project, nested upward walk), plugin `skills/` (namespaced
  `plugin:name`), enterprise dir (noted). Precedence: enterprise >
  personal > project [guide].
- codex: project `.agents/skills/` upward from cwd to repo root;
  personal `~/.agents/skills/`. 2%-of-context cap → warning when the
  discovered set is large.
- cursor: project `.agents/skills/`, `.cursor/skills/`, compat
  `.claude/skills/`, `.codex/skills/`; personal `~/.agents/skills/`,
  `~/.cursor/skills/`.
- copilot: project `.github/skills/`, `.claude/skills/`,
  `.agents/skills/`; personal `~/.copilot/skills/`,
  `~/.agents/skills/` (+ `~/.claude/skills/` under VS Code — noted).
- gemini: project `.gemini/skills/`, `.agents/skills/`; personal
  `~/.gemini/skills/`, `~/.agents/skills/`. Interactive
  `activate_skill` gating → note.
- opencode: six locations (own/claude-compat/vendor-neutral ×
  project/global); upward walk from cwd to git worktree root; v1-vs-v2
  branch per Hazard 6.
- antigravity: `<workspace-root>/.agents/skills/` + `.agent/skills/`
  compat; global BOTH candidates per Hazard 5.

## Verdict

Build it. The rejection's core concern (overstating loading semantics)
is answered by narrowing the claim to the discoverable set and saying
so in the banner, the README, and a verbatim quote of the objection.
The residual risk is a user misreading "skills the harness can see" as
"skills the harness will load" — mitigated, not eliminated; the
honesty notes are the mitigation.
