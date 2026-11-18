# abofaktury

An internal web helper for a law firm. For each subscription client and month, it calculates the
invoice lines from the hours and costs in a Google Sheet, and makes a PDF report of the hours. It can
connect to Subiekt 123 (InsERT).

## Set up

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python manage.py migrate
.venv/bin/python manage.py createsuperuser
```

WeasyPrint needs the pango system libraries.

## Run

```bash
DJANGO_DEBUG=1 DJANGO_ALLOWED_HOSTS=<host> \
GOOGLE_SHEET_ID=<sheet id> GOOGLE_SA_KEY_FILE=<key file> \
SUBIEKT_CREDENTIALS_FILE=<credentials file> SUBIEKT_REDIRECT_URI=http://<host>:<port>/subiekt/callback \
.venv/bin/python manage.py runserver <host>:<port>
```

Keep the key and credentials files outside the repository. Configure the clients in the admin
("Konfiguracja klientów").

## Settings (env vars)

| Variable | Meaning |
|---|---|
| `DJANGO_DEBUG` | `1` for local dev. |
| `DJANGO_SECRET_KEY` | Required when `DJANGO_DEBUG` is not `1`. |
| `DJANGO_ALLOWED_HOSTS` | Comma-separated host names. |
| `GOOGLE_SHEET_ID` | The Google Sheet with the hours. |
| `GOOGLE_SA_KEY_FILE` / `GOOGLE_SA_KEY_JSON` | The key of a read-only service account (file path or JSON). |
| `SUBIEKT_CREDENTIALS_FILE` | A file with `ClientId=`, `ClientSecret=` and `SubscriptionKey=`. |
| `SUBIEKT_REDIRECT_URI` | The redirect URI of the app registration in the InsERT developer portal. |
| `SUBIEKT_ISSUER` | The issuer name on the invoices. |
| `SUBIEKT_PLACE` | The place of issue on the invoices (optional). |

## Test

```bash
DJANGO_DEBUG=1 .venv/bin/python manage.py test billing
```

The tests use invented data and need no network.
