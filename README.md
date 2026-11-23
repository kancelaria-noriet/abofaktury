# abofaktury

An internal web helper for a law firm. For each subscription client and month, it calculates the
invoice lines from the hours and costs in a Google Sheet, and makes a PDF report of the hours. It can
create the invoices in Subiekt 123 (InsERT) and send them with the report through Gmail.

Every page needs a login. Only `/privacy` and `/healthz` are public.

## Run locally

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
DJANGO_DEBUG=1 .venv/bin/python manage.py migrate
DJANGO_DEBUG=1 .venv/bin/python manage.py createsuperuser
DJANGO_DEBUG=1 DJANGO_ALLOWED_HOSTS=<host> <other settings> .venv/bin/python manage.py runserver <host>:<port>
```

WeasyPrint needs the pango system libraries. In local dev the secrets can be files outside the
repository (`*_FILE` settings). Configure the clients in the admin ("Konfiguracja klientów").

## Run in a container

```bash
docker build -t abofaktury .
docker run -p 8000:8000 -v abofaktury-data:/data --env-file <env file> abofaktury
```

- The container runs `migrate`, then gunicorn on port 8000. Logs go to stdout.
- Mount a persistent volume on `/data`: the SQLite database is `/data/db.sqlite3`.
- Run it behind a reverse proxy that ends TLS, redirects HTTP to HTTPS, and sends
  `X-Forwarded-Proto`. The app itself does not redirect, so the health check can use plain HTTP.
- Health check: `GET /healthz` returns `ok` (no login, no external calls).
- Create a user: `docker exec -it <container> python manage.py createsuperuser`. Use a long, random password.

## Settings (env vars)

| Variable | Meaning |
|---|---|
| `DJANGO_DEBUG` | `1` for local dev. Any other value means production. |
| `DJANGO_SECRET_KEY` | Required in production. A long random value. |
| `DJANGO_ALLOWED_HOSTS` | The public host names, comma-separated (`localhost` is always allowed). |
| `DJANGO_CSRF_TRUSTED_ORIGINS` | The public origins, for example `https://app.example.com`. |
| `DJANGO_DB_PATH` | The SQLite file. Default: `db.sqlite3` next to `manage.py`; the image sets `/data/db.sqlite3`. |
| `DJANGO_HSTS_SECONDS` | HSTS max-age in production. Default: `3600`. |
| `DJANGO_LOG_LEVEL` | Default: `INFO`. |
| `GOOGLE_SHEET_ID` | The Google Sheet with the hours. |
| `GOOGLE_SA_KEY_JSON` / `GOOGLE_SA_KEY_FILE` | The key of a read-only service account (JSON, or a file path). |
| `SUBIEKT_CLIENT_ID`, `SUBIEKT_CLIENT_SECRET`, `SUBIEKT_SUBSCRIPTION_KEY` | The Subiekt 123 API credentials. |
| `SUBIEKT_CREDENTIALS_FILE` | Or a file with `ClientId=`, `ClientSecret=` and `SubscriptionKey=`. |
| `SUBIEKT_REDIRECT_URI` | `https://<host>/subiekt/callback`; the same URI as in the app registration. |
| `SUBIEKT_ISSUER` | The issuer name on the invoices. |
| `SUBIEKT_PLACE` | The place of issue on the invoices (optional). |
| `SUBIEKT_BANK_ACCOUNT_NUMBER` | The bank account number on the invoices (required to create invoices). |
| `SUBIEKT_BANK_ACCOUNT_NAME` | The name of that account, as in Subiekt (optional). |
| `GMAIL_CLIENT_JSON` / `GMAIL_CLIENT_FILE` | A Google OAuth client of type "Web application" (Gmail API), JSON or a file. |
| `GMAIL_REDIRECT_URI` | `https://<host>/gmail/callback`; the same URI as in the OAuth client. |
| `EMAIL_ACCOUNT` | The Google account that sends (a login hint). |
| `EMAIL_FROM` | The From address (a "send as" alias of that account). |
| `EMAIL_TEST_RECIPIENT` | Test mode: all emails go only to this address. |
| `EMAIL_TEST_INVOICE_NUMBER` | Test mode: the number of an invoice in Subiekt that every test email carries. |
| `BUGSINK_DSN` | Error reporting (Sentry SDK). Empty: off. |
| `APP_ENV` | The environment name for error reporting. Default: `production`. |
| `SOURCE_COMMIT` | The release for error reporting (set by the build platform; `HEAD` is ignored). |

Keep every secret out of the repository and out of build arguments.

## Test

```bash
DJANGO_DEBUG=1 .venv/bin/python manage.py test billing
```

The tests use invented data and need no network.
