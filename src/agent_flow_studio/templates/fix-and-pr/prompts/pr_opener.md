The human approved the changes. Open a **draft** pull request from the `branch` input into
`{{base_branch}}` through the connector `{{connector}}` (`http_write`), with the request that fits
the connector type:
- **GitHub**: `POST /repos/{{repository}}/pulls` with
  `{"title", "head": "<branch>", "base": "{{base_branch}}", "body", "draft": true}`.
- **Azure DevOps**: `POST /<project>/_apis/git/repositories/<repository>/pullrequests?api-version=7.1`
  (`<project>/<repository>` = `{{repository}}`) with
  `{"sourceRefName": "refs/heads/<branch>", "targetRefName": "refs/heads/{{base_branch}}",
  "title", "description", "isDraft": true}`.
- **GitLab**: `POST /projects/<id>/merge_requests` (`<id>` = `{{repository}}` URL-encoded) with
  `{"source_branch": "<branch>", "target_branch": "{{base_branch}}", "title": "Draft: …",
  "description"}`.

Title: short, with the `issue` id and the `issue_title`. Body: the `cause` (with `path:line`), the
`summary` of the changes and tests, and a link to the issue (`issue_url`). The request follows
the flow autonomy: if it is refused, do not retry.

Save these outputs with `set_output`: `opened`, and from the response `pr_url` and `pr_id`.
