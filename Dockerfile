# syntax=docker/dockerfile:1.7
FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

RUN groupadd --system --gid 10001 scanner \
    && useradd --system --uid 10001 --gid scanner --home-dir /app \
       --shell /usr/sbin/nologin scanner

COPY requirements.txt ./
RUN python -m pip install --no-cache-dir -r requirements.txt

COPY --chown=scanner:scanner src ./src
COPY --chown=scanner:scanner config ./config

RUN mkdir -p /app/data && chown -R scanner:scanner /app/data

USER scanner:scanner

# SQLite and backups live here. Mount a persistent volume in production.
VOLUME ["/app/data"]

HEALTHCHECK --interval=60s --timeout=10s --start-period=180s --retries=3 \
    CMD ["python", "-m", "src.healthcheck"]

ENTRYPOINT ["python", "-m", "src.main"]
CMD ["run"]


# Test stage: docker build --target test -t ebay-scanner-test .
FROM python:3.12-slim AS test

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements-dev.txt ./
RUN python -m pip install --no-cache-dir -r requirements-dev.txt

COPY src ./src
COPY config ./config
COPY tests ./tests
COPY pytest.ini ./

RUN useradd --system --uid 10001 --home-dir /app --shell /usr/sbin/nologin scanner \
    && chown -R scanner:scanner /app/data 2>/dev/null || true
USER scanner

CMD ["python", "-m", "pytest", "-q"]
