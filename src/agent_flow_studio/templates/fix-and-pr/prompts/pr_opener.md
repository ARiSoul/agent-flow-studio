Read `01_plan.json` and `02_changes.json`. If `pushed` is false, do not open a PR: save
`03_pr.json` with `opened: false` and the reason.

Open a **draft** pull request from `fix/<issue>` into `{{base_branch}}` through the connector
`{{connector}}` (`http_write`), with the request that fits the connector type:
- **GitHub**: `POST /repos/{{repository}}/pulls` with
  `{"title", "head": "fix/<issue>", "base": "{{base_branch}}", "body", "draft": true}`.
- **Azure DevOps**: `POST /<project>/_apis/git/repositories/<repository>/pullrequests?api-version=7.1`
  (`<project>/<repository>` = `{{repository}}`) with
  `{"sourceRefName": "refs/heads/fix/<issue>", "targetRefName": "refs/heads/{{base_branch}}",
  "title", "description", "isDraft": true}`.
- **GitLab**: `POST /projects/<id>/merge_requests` (`<id>` = `{{repository}}` URL-encoded) with
  `{"source_branch": "fix/<issue>", "target_branch": "{{base_branch}}", "title": "Draft: …",
  "description"}`.

Title: short, with the issue id. Body: the cause (with `path:line`), what changed, the tests and
their result, and a link to the issue. The request follows the flow autonomy: if it is refused,
do not retry.

Save `03_pr.json` with `{opened, url, id, title}` from the response.
