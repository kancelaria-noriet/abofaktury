# abofaktury

An internal web helper for a law firm. For each subscription client and month, it calculates the
invoice data (the subscription for the month, plus the overtime and the costs of the previous month).
It also makes a PDF report of the activities.

Status: in progress. The plan is outside this repository.

No client data is in this repository. The app reads the hours from the Google Sheet and keeps the
client settings and the Subiekt tokens in its database (`db.sqlite3`, not in git). The tests use
invented data (`billing/fixtures/test_clients.json`, `billing/tests/helpers.py`).

## Architecture

To do - fill in after the demo feedback. Short version for now:

- `billing/calc.py`: the pure calculation (no Django imports).
- `billing/source.py`: the source of the entries. The Google Sheet (read-only service account, one
  `values:batchGet` per page) and a pure parser `parse_tab()`. A problem with one
  tab becomes a warning for that client. A problem with the whole sheet shows a message on the page.
- `billing/views.py` and the templates: the month view (all clients on one page) and the PDF report
  (WeasyPrint).
- `billing/models.py`: the client config (Django admin).
- `billing/subiekt.py`: the Subiekt 123 API. OAuth 2.0 with PKCE at kontoapi.insert.com.pl, the tokens in
  `SubiektConnection` (one row, not in the admin), refresh, the client and product lookup, and the invoice:
  `build_invoice()` (pure), create, read and delete. `SubiektInvoice` stores one invoice for each client and
  month. The app never sends to KSeF; the operator does that in Subiekt.
- Every page needs a login (`LoginRequiredMiddleware`). The admin login form is the login page.

## Decisions

To do - fill in after the demo feedback.
