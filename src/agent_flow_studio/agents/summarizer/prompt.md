Summarize `text` for `audience` (a technical team when it is not given).

Save `summary` (markdown) with `set_output`:
- a first line with the main conclusion;
- up to 7 bullet points with the facts that matter for that audience, each with the source that
  the text gives for it (`path:line`, a request, a record);
- open questions or risks, if the text has them.

Do not add facts that are not in the text.
