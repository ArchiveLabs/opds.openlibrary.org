FROM docker.io/library/python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends git curl \
    && rm -rf /var/lib/apt/lists/*

COPY opds.openlibrary.org/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY opds.openlibrary.org/app/ ./app/

EXPOSE 8080

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
