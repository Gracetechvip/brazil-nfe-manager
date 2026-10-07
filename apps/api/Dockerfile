FROM postgres:16-bookworm AS pgtools

FROM python:3.13-slim
WORKDIR /app
# Use the same major-version PostgreSQL client as the Compose database. A
# newer pg_restore can emit settings (for example transaction_timeout) that a
# PostgreSQL 16 server cannot parse.
COPY --from=pgtools /usr/lib/postgresql/16/bin/pg_dump /usr/local/bin/pg_dump
COPY --from=pgtools /usr/lib/postgresql/16/bin/pg_restore /usr/local/bin/pg_restore
COPY --from=pgtools /usr/lib/postgresql/16/bin/createdb /usr/local/bin/createdb
COPY --from=pgtools /usr/lib/postgresql/16/bin/dropdb /usr/local/bin/dropdb
RUN apt-get update \
    && apt-get install -y --no-install-recommends libpq5 libreadline8 libzstd1 liblz4-1 libssl3 \
    && rm -rf /var/lib/apt/lists/* \
    && pg_dump --version
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
ENV PYTHONUNBUFFERED=1
CMD ["uvicorn","app.main:app","--host","0.0.0.0","--port","8000"]
