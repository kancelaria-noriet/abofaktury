# abofaktury: Django + gunicorn. All configuration comes from environment variables (see README.md).
FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# WeasyPrint needs pango; the PDF report uses DejaVu Sans; the health check needs curl.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        libpango-1.0-0 libpangoft2-1.0-0 libharfbuzz-subset0 fonts-dejavu-core curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .
# The secret key is a placeholder: collectstatic needs settings, not a real key.
RUN DJANGO_DEBUG=0 DJANGO_SECRET_KEY=collectstatic-only python manage.py collectstatic --noinput

# Run as a non-root user. /data holds the SQLite database (mount a persistent volume there).
RUN useradd --uid 10001 --no-create-home --home-dir /app --shell /usr/sbin/nologin app \
    && mkdir -p /data \
    && chown app /data \
    && chmod +x start.sh
USER app
# /app is read-only for the app user: gunicorn's control socket and the fontconfig cache go to /tmp.
ENV HOME=/tmp

ENV DJANGO_DB_PATH=/data/db.sqlite3
VOLUME ["/data"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD curl -fsS http://localhost:8000/healthz || exit 1

CMD ["./start.sh"]
