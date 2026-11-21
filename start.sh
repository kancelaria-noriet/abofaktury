#!/bin/sh
# Container start: apply the migrations, then run gunicorn as PID 1 (so that it receives SIGTERM).
set -e

python manage.py migrate --noinput

# 2 workers: SQLite allows one writer at a time. Timeout 120 s: the PDF, the sheet and the batch send
# can be slow. The access log shows the path without the query string (OAuth codes stay out of the log).
exec gunicorn abofaktury.wsgi \
    --bind 0.0.0.0:8000 \
    --workers 2 \
    --timeout 120 \
    --access-logfile - \
    --error-logfile - \
    --access-logformat '%(h)s "%(m)s %(U)s %(H)s" %(s)s %(b)s %(M)sms'
