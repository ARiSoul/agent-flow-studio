Run exactly this command once, in the workspace, with `workspace.bash`:

```
{{test_command}}
```

Run nothing else: no installs, no fixes, no other commands. If the command is refused (it needs
approval and was not approved), save `ran: false` and explain in `output_tail`.

Save these outputs with `set_output`:
- `ran` (true/false), `exit_code`;
- `passed`, `failed`, `errors`, `skipped`: counts, from the tool output;
- `failures`: list of `{test, file, line, message}` for each failing test (message = the key line
  of the error, not the whole trace);
- `output_tail`: the last 40 lines of the output.

Copy the numbers and names from the output. Do not guess.
