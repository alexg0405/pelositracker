# syntax=docker/dockerfile:1

# ---- build stage: compile the Rust engine into a wheel ----
FROM python:3.11-bookworm AS builder
RUN curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y --profile minimal
ENV PATH="/root/.cargo/bin:${PATH}"
WORKDIR /src
COPY pyproject.toml ./
COPY native_engine ./native_engine
COPY app ./app
# Builds a mixed wheel: the app package + the compiled app._native_engine.so
RUN pip install --no-cache-dir "maturin>=1.9,<2.0" \
    && maturin build --release --out /wheels

# ---- runtime stage: slim image, no Rust toolchain ----
FROM python:3.11-slim-bookworm
ENV PYTHONUNBUFFERED=1 PORT=8000
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY --from=builder /wheels/*.whl /tmp/
RUN pip install --no-cache-dir /tmp/*.whl && rm -f /tmp/*.whl
EXPOSE 8000
# Render/Railway inject $PORT; bind it, default 8000 locally.
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
