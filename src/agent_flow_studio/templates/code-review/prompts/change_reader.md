Find out what the current branch changes against `{{base_branch}}`. Use only read-only git
(`workspace.git`):

1. `branch` with `--show-current` to know the branch.
2. `log` with `{{base_branch}}..HEAD --oneline` for the commits.
3. `diff` with `{{base_branch}}...HEAD --stat` for the size, then `diff` with
   `{{base_branch}}...HEAD` (add `-- <path>` per file when the diff is long).

Save `01_changes.json` with:
- `branch`, `base`: the two branches;
- `commits`: list of `{sha, subject}`;
- `files`: list of `{path, status, added, removed, summary}` (summary = what changes, in one line);
- `risk_areas`: list of `{path, why}` — security, data, concurrency, public API, config, migrations;
- `tests_changed`: true or false, and which test files;
- `notes`: anything you could not read or understand.

If there are no commits and no diff, save the file with empty lists and say so. Do not make
things up: every entry comes from the git output.
