Review the code given in `scope` (and use `context` when you have it). Read the relevant files
(`workspace.read`), look for callers and tests (`workspace.grep`, `workspace.glob`) and use
`workspace.git` (`diff`, `show`, `blame`, `log`) when the history matters.

Save these outputs with `set_output`:
- `review` (markdown):
  1. **Summary**: 3 lines on what the code does and its state.
  2. **Findings**: by severity (blocker, major, minor). Each one: the problem, why it matters,
     `path:line` as evidence, and a concrete fix.
  3. **Tests**: what is covered and what is missing.
  4. **Not checked**: what you did not read or could not verify.
- `blockers`: how many blocker findings;
- `verdict`: `ready`, `changes_needed` or `blocked`.

Every finding needs evidence from the code; do not list generic advice.
