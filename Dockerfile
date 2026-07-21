# syntax=docker/dockerfile:1.7

# Rust is supplied by a versioned official image; the build never executes a
# remote shell installer. Debian Bookworm's Python 3.11 matches the runtime ABI.
FROM rust:1.97.0-slim-bookworm AS builder
RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 python3-dev python3-pip patchelf \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /src
COPY pyproject.toml ./
COPY native_engine ./native_engine
COPY app ./app
RUN python3 -m pip install --break-system-packages --no-cache-dir maturin==1.14.1 \
    && python3 -m maturin build --release --out /wheels

FROM python:3.11.13-slim-bookworm AS runtime
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8000 \
    WEB_CONCURRENCY=1
WORKDIR /app
COPY requirements.txt ./
RUN python -m pip install --no-cache-dir -r requirements.txt \
    && groupadd --system app \
    && useradd --system --gid app --home-dir /app app
COPY --from=builder /wheels/*.whl /tmp/
RUN python -m pip install --no-cache-dir /tmp/*.whl \
    && rm -f /tmp/*.whl \
    && mkdir -p /app/data \
    && chown -R app:app /app
USER app
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:'+os.getenv('PORT','8000')+'/api/ready', timeout=3)" || exit 1
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1"]
