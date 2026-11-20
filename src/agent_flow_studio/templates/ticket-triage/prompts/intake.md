Decide where the ticket comes from, using your inputs (`source`, and `text`, `url` or `number`).

- `source` is `text`: if `text` has the ticket, save `route` = `ready` and `ticket_text` = the
  text as given (do not change it). If it is empty, save `route` = `invalid` and `problem`.
- `source` is `number`: save `route` = `fetch` and `ticket_number` = the number, trimmed.
- `source` is `url`: find the ticket number in the URL (path or query) of the
  `{{connector}}` platform and save `route` = `fetch` and `ticket_number`. If the URL has no
  ticket number, or it is clearly another site, save `route` = `invalid` and `problem`.

Do not invent a number. Save only these outputs; do not triage the ticket.
