Read `01_test_plan.json` and `02_test_results.json`. For each failing test, read the test and the
code it tests (`workspace.read`, `workspace.grep`), and check recent changes with `workspace.git`
(`log`, `diff`, `blame`) when they help. Do not change files and do not run commands.

Save `03_diagnosis.md` with:
1. **Summary** — passed / failed / errors, and the main causes in 2 lines.
2. **Per failure** — the test (`path:line`), what fails, the likely cause with evidence
   (`path:line` in the code, or the commit that changed it), how sure you are (high, medium, low)
   and a concrete fix (what to change and where).
3. **Failures that are not the code** — setup, environment, flaky tests — and how to tell.
4. **Not checked** — what you could not verify.
