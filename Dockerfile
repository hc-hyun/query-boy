# syntax=docker/dockerfile:1.7

FROM python:3.14-slim-bookworm@sha256:9ab8d9c8514b44f90cf0029dd42fdd7e9e211e639c8b995304cc04568dee900f AS builder

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


FROM python:3.14-slim-bookworm@sha256:9ab8d9c8514b44f90cf0029dd42fdd7e9e211e639c8b995304cc04568dee900f

ARG QUERY_MAN_VCS_REF=unknown
LABEL org.opencontainers.image.revision="${QUERY_MAN_VCS_REF}"

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN groupadd --gid 10001 queryman \
    && useradd --uid 10001 --gid queryman --no-create-home --home-dir /nonexistent \
        --shell /usr/sbin/nologin queryman \
    && install -d -o queryman -g queryman -m 0700 /var/lib/query-man/diagnostics

WORKDIR /app

COPY --from=builder --chown=queryman:queryman /app/.venv /app/.venv
COPY --chown=queryman:queryman config ./config

USER queryman

EXPOSE 3000

CMD ["/app/.venv/bin/query-man"]
