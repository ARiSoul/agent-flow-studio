Triage the ticket. Its text is in `fetched_text` (read from the platform; `evidence` says how) or
in `given_text` (pasted by the person) — use the one you have.

Save these outputs with `set_output`:
- `summary` (markdown, a draft for human review):
  1. **What is asked** — two or three lines.
  2. **Facts** — who, what system, since when, what was already tried; each with a short quote
     from the ticket as evidence.
  3. **Missing information** — what you would need to ask the requester.
  4. **Next steps** — concrete and in order.
- `category`: incident, request, question, access or other.
- `priority`: low, medium, high or critical, with the reason in the summary.

Do not invent facts: if the ticket does not say something, write that it is unknown.
