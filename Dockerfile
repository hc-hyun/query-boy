# syntax=docker/dockerfile:1.7

FROM python:3.15-slim-bookworm@sha256:2a41267d9de9d358f77a1de356e0f16ecba8d39827855be0ce9ef4792edce402 AS builder

COPY --from=ghcr.io/astral-sh/uv:0.9.18@sha256:5713fa8217f92b80223bc83aac7db36ec80a84437dbc0d04bbc659cae030d8c9 /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_NO_DEV=1 \
    UV_PYTHON_DOWNLOADS=0

WORKDIR /app

COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-install-project

COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable


FROM python:3.15-slim-bookworm@sha256:2a41267d9de9d358f77a1de356e0f16ecba8d39827855be0ce9ef4792edce402

ARG QUERY_MAN_VCS_REF=unknown
LABEL org.opencontainers.image.revision="${QUERY_MAN_VCS_REF}"

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN groupadd --gid 10001 queryman \
    && useradd --uid 10001 --gid queryman --no-create-home --home-dir /nonexistent \
        --shell /usr/sbin/nologin queryman

WORKDIR /app

COPY --from=builder --chown=queryman:queryman /app/.venv /app/.venv
COPY --chown=queryman:queryman config ./config

USER queryman

EXPOSE 3000

CMD ["/app/.venv/bin/query-man"]
