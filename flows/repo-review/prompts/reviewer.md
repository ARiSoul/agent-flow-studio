Use the `overview` input to choose what to read, and review the most relevant code (read-only).
If you got a `focus`, give it priority.

Save these outputs with `set_output`:
- `review` (markdown):
  1. Summary (3 lines).
  2. Risks and likely bugs, by severity, each with `path:line` as evidence.
  3. Concrete improvement suggestions (missing tests, simplifications, security).
  4. What you did not check.
- `risks`: how many risks and likely bugs you listed.

Be specific; do not list generic points without evidence.
