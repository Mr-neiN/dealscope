# syntax=docker/dockerfile:1.7

# ------------------------------------------------------------------ test image
# Kept first so that a plain `docker build .` falls through to the runtime
# stage below. Run the suite explicitly with:
#   docker build --target test -t ebay-scanner-test .
FROM python:3.12-slim AS test

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements-dev.txt ./
RUN python -m pip install --no-cache-dir -r requirements-dev.txt

COPY --chown=nobody:nogroup src ./src
COPY --chown=nobody:nogroup config ./config
COPY --chown=nobody:nogroup tests ./tests
COPY --chown=nobody:nogroup pytest.ini ./

USER nobody:nogroup

CMD ["python", "-m", "pytest", "-q"]


# ------------------------------------------------------------- runtime image
# Default build target.
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
