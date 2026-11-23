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
- `billing/views.py` and the templates: the month view (all clients on one page) and the PDF report of the hours
  (WeasyPrint; no cost rows, a "Razem" total).
- `billing/models.py`: the client config (Django admin).
- `billing/subiekt.py`: the Subiekt 123 API. OAuth 2.0 with PKCE at kontoapi.insert.com.pl, the tokens in
  `SubiektConnection` (one row, not in the admin), refresh, the client and product lookup, and the invoice:
  `build_invoice()` (pure), create, read and delete. `SubiektInvoice` stores one invoice for each client and
  month. The app never sends to KSeF; the operator does that in Subiekt. The API does not give the company's
  bank accounts and does not use the default account of the web UI, so the account comes from the settings.
- `billing/gmail.py` and `billing/mailing.py`: the Gmail connection (OAuth with PKCE, a "Web application"
  client, the callback `/gmail/callback` checks the `state`; scope `gmail.send`) and the email with the
  invoice and the report. The text is in `templates/billing/email/`: `body.txt` and `body.html` (the same
  content), and `logo.jpg`, which goes into the email as an inline image (CID), not as a remote link. `EmailSend` logs every email. A test
  mode sends only to a test address.
- Every page needs a login (`LoginRequiredMiddleware`). The admin login form is the login page. Only
  `/privacy` and `/healthz` are public. Every view that changes data accepts only POST with CSRF.
- Deployment: `Dockerfile` and `start.sh` (migrate, then gunicorn). Production settings apply when
  `DJANGO_DEBUG` is not `1`: HTTPS behind a proxy, WhiteNoise, logs to stdout, SQLite at `DJANGO_DB_PATH`.
  Secrets come from env vars in production and from files in local dev. Errors go to Bugsink
  (`BUGSINK_DSN`), with no personal data and no secrets (`_scrub_event` in the settings).

## Decisions

To do - fill in after the demo feedback.
