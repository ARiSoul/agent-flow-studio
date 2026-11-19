Review the changes against `{{base_branch}}`, using your inputs (`files`, `risk_areas`, `commits`,
`tests_changed`, and `focus` when given). Read each changed file around the changed lines
(`workspace.read`), and look for the callers and tests of what changed (`workspace.grep`,
`workspace.glob`). Use `workspace.git` (`diff`, `show`, `blame`) when you need the exact change.

Save these outputs with `set_output`:
- `review` (markdown):
  1. **Summary** — 3 lines: what the branch does and whether it is ready.
  2. **Findings** — by severity (blocker, major, minor). Each one: the problem, why it matters,
     `path:line` as evidence, and a concrete fix.
  3. **Tests** — what is covered, what is missing (cases, edge cases, errors).
  4. **Suggestions** — simplifications or clean-ups, only when they are worth it.
  5. **Not checked** — what you did not read or could not verify.
- `blockers`: how many blocker findings;
- `verdict`: `ready`, `changes_needed` or `blocked`.

Be specific. Every finding needs evidence from the code; do not list generic advice.
