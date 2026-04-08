FROM docker.io/library/python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends git curl \
    && rm -rf /var/lib/apt/lists/*

COPY opds.openlibrary.org/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY opds.openlibrary.org/app/ ./app/

RUN groupadd --gid 1000 appuser \
    && useradd --uid 1000 --gid 1000 --no-create-home appuser

USER appuser

EXPOSE 8080

CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port 8080 --workers ${WEB_CONCURRENCY:-1}"]
