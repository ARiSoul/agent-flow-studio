Read the issue given in the `issue` input through the connector `{{connector}}` (`http_get`, read
only), then find its cause in the code. The repository at the provider is `{{repository}}`. Use the
`notes` input when there is one.

Read the issue with the request that fits the connector type (it is in the "Connectors" section
below):
- **GitHub**: `GET /repos/{{repository}}/issues/<issue>` (and `/comments` if needed).
- **Azure DevOps**: `GET /<project>/_apis/wit/workitems/<issue>?api-version=7.1` — `<project>` is
  the part of `{{repository}}` before the `/`.
- **GitLab**: `GET /projects/<id>/issues/<issue>` — `<id>` is `{{repository}}` URL-encoded
  (`/` → `%2F`).

Then read the code (`workspace.read`, `workspace.grep`, `workspace.glob`) and the history
(`workspace.git`: `log`, `blame`, `show`) to find where the problem is.

Save these outputs with `set_output`:
- `issue_title`, `issue_url`;
- `fixable`: false when the issue was not found, is unclear, needs a decision from a person, or is
  not a code change — then say why in `reason`;
- `cause`: the likely cause, with `path:line` evidence, and how sure you are (high, medium, low);
- `files_to_change`: list of `{path, what}`;
- `plan`: the fix in short steps, the tests to add or update, and how the tests run (from the
  repository files).

Do not make things up. Do not change anything.
