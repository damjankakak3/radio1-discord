FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.11.0 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PYTHONUNBUFFERED=1
WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY src ./src
RUN uv sync --locked --no-dev && useradd --uid 10001 --create-home radio1 \
    && mkdir /app/data && chown radio1:radio1 /app/data
USER radio1
EXPOSE 5000
CMD ["/app/.venv/bin/radio1"]
