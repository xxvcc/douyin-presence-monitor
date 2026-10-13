FROM python:3.12-slim AS build
WORKDIR /app/build
ENV TMPDIR=/app/build-tmp
RUN mkdir -m 0700 /app/build-tmp
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN python -m pip wheel --no-deps --wheel-dir /app/wheels .

FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 TMPDIR=/app/tmp
ENV DPM_HOME=/app PATH=/app/current/venv/bin:$PATH
COPY --from=build /app/wheels /app/wheels
RUN python -m venv --copies /app/releases/0.1.0/venv \
    && /app/releases/0.1.0/venv/bin/pip install --no-index --no-cache-dir --no-deps /app/wheels/*.whl \
    && rm -rf /app/wheels \
    && ln -s releases/0.1.0 /app/current \
    && mkdir -p /app/config /app/data /app/logs /app/backups /app/exports /app/tmp \
    && chown 10001:10001 /app/config /app/data /app/logs /app/backups /app/exports /app/tmp \
    && chmod 0700 /app/config /app/data /app/logs /app/backups /app/exports /app/tmp
USER 10001:10001
WORKDIR /app
ENTRYPOINT ["/app/current/venv/bin/douyin-monitor"]
CMD ["run"]
