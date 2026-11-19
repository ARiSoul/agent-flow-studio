Make the fix from the `plan` and `files_to_change` inputs. Work only on the branch
`fix/<issue>` (the `issue` input).

1. Check the state with `workspace.git` (`status`, `branch --show-current`). If there are
   uncommitted changes that are not yours, stop and report.
2. Create the branch from `{{base_branch}}` with `workspace.bash`:
   `git fetch origin {{base_branch}}` and `git switch -c fix/<issue> origin/{{base_branch}}`.
3. Change the code (`workspace.edit`, `workspace.write`), small and focused on the issue. Add or
   update the tests from the plan.
4. Run the tests with `workspace.bash`: `{{test_command}}` (if this is empty, use the command in
   the plan). Fix what your change broke; do not hide failures.
5. Commit with a clear message that names the issue, and push only the work branch:
   `git push -u origin fix/<issue>`. Never push to `{{base_branch}}` and never force.

Every command and file change follows the flow autonomy: if one is refused, stop and report.

Save these outputs with `set_output`:
- `branch`: `fix/<issue>`;
- `pushed`: true only when the push worked;
- `tests_passed`: true when the tests passed after your change;
- `commits`: list of `{sha, message}`;
- `summary` (markdown): the files you changed and why, the test command and its result, and what
  you did not do or are not sure about.
