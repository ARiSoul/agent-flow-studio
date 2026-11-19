Find out how this repository runs its tests, before they run. The command to use is
`{{test_command}}`. Take the `notes` input into account when there is one.

Read the test configuration and layout (for example `pyproject.toml`, `pytest.ini`,
`package.json`, `*.csproj`, `Makefile`, CI files under `.github/workflows` or
`azure-pipelines.yml`).

Save these outputs with `set_output`:
- `matches_repo`: does the repository really run its tests with `{{test_command}}`? Say why in
  `warnings`, with the file as evidence;
- `test_folders`: where the tests are;
- `setup_needed`: what must exist before the tests run (installed packages, services, env vars),
  as far as the files show;
- `warnings`: anything that may make the run fail for reasons other than the code.

Cite the files you base this on. Do not make things up.
