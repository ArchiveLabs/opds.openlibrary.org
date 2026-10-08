FROM docker.io/library/python:3.12-slim

WORKDIR /app

# apt reads only lowercase http_proxy, not the HTTP_PROXY the build runner
# sets, so point it at the proxy explicitly when there is one.
RUN if [ -n "$HTTP_PROXY" ]; then echo "Acquire::http::Proxy \"$HTTP_PROXY\";" > /etc/apt/apt.conf.d/99proxy; fi \
  && apt-get update && apt-get install -y --no-install-recommends git curl \
  && rm -rf /var/lib/apt/lists/* /etc/apt/apt.conf.d/99proxy

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/

RUN groupadd --gid 1000 appuser \
  && useradd --uid 1000 --gid 1000 --no-create-home appuser

USER appuser

EXPOSE 8080

CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port 8080 --workers ${WEB_CONCURRENCY:-2}"]
