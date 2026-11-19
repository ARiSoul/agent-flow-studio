Read `01_plan.json` and make the fix. Work only on the branch `fix/<issue>` (the run input).

1. Check the state with `workspace.git` (`status`, `branch --show-current`). If there are
   uncommitted changes that are not yours, stop and report.
2. Create the branch from `{{base_branch}}` with `workspace.bash`:
   `git fetch origin {{base_branch}}` and `git switch -c fix/<issue> origin/{{base_branch}}`.
3. Change the code (`workspace.edit`, `workspace.write`), small and focused on the issue. Add or
   update the tests from the plan.
4. Run the tests with `workspace.bash`: `{{test_command}}` (if this is empty, use the command the
   plan found). Fix what your change broke; do not hide failures.
5. Commit with a clear message that names the issue, and push only the work branch:
   `git push -u origin fix/<issue>`. Never push to `{{base_branch}}` and never force.

Every command and file change follows the flow autonomy: if one is refused, stop and report.

Save `02_changes.json` with:
- `branch`, `base`: `fix/<issue>` and `{{base_branch}}`;
- `commits`: list of `{sha, message}`;
- `files`: list of `{path, change}`;
- `tests`: `{command, passed, failed, output_tail}`;
- `pushed`: true or false;
- `notes`: what you did not do or are not sure about.
