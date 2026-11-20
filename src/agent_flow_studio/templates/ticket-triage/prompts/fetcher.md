Read the ticket `ticket_number` from the `{{connector}}` platform with the action
`{{ticket_action}}` (`run_action` with `params: {"number": <ticket_number>}`). It is read only.

Save these outputs with `set_output`:
- `ticket_text` (markdown): the ticket as it is on the platform — title, state, requester,
  dates, description and the comments or notes, in order. Keep the original words; do not
  summarize or judge it here.
- `evidence`: the action and the number you read, e.g. `{{connector}}.{{ticket_action}}(INC0012345)`.

If the action fails or the ticket does not exist, save `ticket_text` with what the platform said
(the error) and say so clearly; do not invent content.
