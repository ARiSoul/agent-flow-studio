Explore the workspace repository (read-only). If you got a `focus`, look at that part first, but
still describe the whole repository briefly.

Use `workspace.glob` to see the layout, `workspace.read` for the README, the build files and the
main entry points, and `workspace.grep` to find where things are defined.

Save these outputs with `set_output`:
- `overview` (markdown): what the project does; the folder structure (main folders, one line
  each); technologies and frameworks; entry points; how to build, test and run it (only what the
  files show). Cite `path` or `path:line` for each fact.
- `main_language`: the main programming language.

Do not guess: when something is not in the files, say it is unknown.
