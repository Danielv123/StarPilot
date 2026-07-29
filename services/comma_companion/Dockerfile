# syntax=docker/dockerfile:1.8

ARG NODE_IMAGE=node:22-bookworm-slim@sha256:6c74791e557ce11fc957704f6d4fe134a7bc8d6f5ca4403205b2966bd488f6b3
ARG PYTHON_IMAGE=python:3.12-slim-bookworm@sha256:d50fb7611f86d04a3b0471b46d7557818d88983fc3136726336b2a4c657aa30b
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.11.2@sha256:c4f5de312ee66d46810635ffc5df34a1973ba753e7241ce3a08ef979ddd7bea5
ARG COMMA_DYNAMICS_MODEL_FILENAME=neural_lateral_plant.pt
ARG COMMA_DYNAMICS_MODEL_IMAGE_PATH=/app/artifacts/dynamics/neural_lateral_plant.pt
ARG COMMA_DYNAMICS_MODEL_SHA256=fb1b8b951fdff19ff5f9349470415d003b61ee5655fff996365b429d93f6dc45
ARG COMPANION_SOURCE_SHA256=0000000000000000000000000000000000000000000000000000000000000000

FROM ${UV_IMAGE} AS uv

FROM ${NODE_IMAGE} AS web-build
WORKDIR /build/web

COPY web/package.json web/package-lock.json ./
RUN --mount=type=cache,target=/root/.npm \
    npm ci --no-audit --no-fund

COPY web/ ./
RUN npm run build \
    && find dist -type f -name '*.map' -delete

FROM ${PYTHON_IMAGE} AS python-build
ARG COMMA_DYNAMICS_MODEL_FILENAME
ARG COMMA_DYNAMICS_MODEL_IMAGE_PATH
COPY --from=uv /uv /uvx /usr/local/bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

COPY backend/ /app/services/comma_companion/backend/
COPY media_worker/ /app/services/comma_companion/media_worker/
COPY adapters/dynamics/ /app/services/comma_companion/adapters/dynamics/
COPY adapters/rlog/ /app/services/comma_companion/adapters/rlog/

# These independent locks deliberately remain independent. The durable worker
# invokes the media, rlog, and dynamics adapters through subprocess contracts.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --project /app/services/comma_companion/backend --locked --no-dev --no-editable \
    && uv sync --project /app/services/comma_companion/media_worker --locked --no-dev --no-editable \
    && uv sync --project /app/services/comma_companion/adapters/dynamics --locked --no-dev --no-editable \
    && uv venv --python /usr/local/bin/python /app/services/comma_companion/adapters/rlog/.venv \
    && uv pip install \
      --python /app/services/comma_companion/adapters/rlog/.venv/bin/python \
      --only-binary=:all: \
      --strict \
      "pycapnp==2.1.0" \
      "zstandard==0.25.0"

# Named build contexts keep the full StarPilot checkout out of the image and
# avoid sending unrelated logs, models, source, or secrets to the builder.
COPY --from=model_context /${COMMA_DYNAMICS_MODEL_FILENAME} \
  ${COMMA_DYNAMICS_MODEL_IMAGE_PATH}
COPY --from=cereal_context /__init__.py /app/cereal/__init__.py
COPY --from=cereal_context /log.capnp /app/cereal/log.capnp
COPY --from=cereal_context /custom.capnp /app/cereal/custom.capnp
COPY --from=cereal_context /legacy.capnp /app/cereal/legacy.capnp
COPY --from=cereal_context /include/ /app/cereal/include/
# cereal/car.capnp is a repository symlink. Copy its target as a regular file.
COPY --from=opendbc_car_context /car.capnp /app/cereal/car.capnp

FROM ${PYTHON_IMAGE} AS runtime

ARG APP_UID=65532
ARG APP_GID=65532
ARG NODE_IMAGE
ARG PYTHON_IMAGE
ARG UV_IMAGE
ARG STARPILOT_COMMIT=unknown
ARG COMMA_DYNAMICS_MODEL_IMAGE_PATH
ARG COMMA_DYNAMICS_MODEL_SHA256
ARG COMPANION_SOURCE_SHA256

LABEL org.opencontainers.image.title="Comma Companion" \
      org.opencontainers.image.description="Private comma archive, control plane, and dynamics workbench" \
      org.opencontainers.image.source="https://github.com/Danielv123/StarPilot" \
      org.opencontainers.image.revision="${STARPILOT_COMMIT}" \
      no.danielv.comma-companion.base.node="${NODE_IMAGE}" \
      no.danielv.comma-companion.base.python="${PYTHON_IMAGE}" \
      no.danielv.comma-companion.base.uv="${UV_IMAGE}" \
      no.danielv.comma-companion.source-bundle.sha256="${COMPANION_SOURCE_SHA256}" \
      no.danielv.comma-companion.dynamics-model.path="${COMMA_DYNAMICS_MODEL_IMAGE_PATH}" \
      no.danielv.comma-companion.dynamics-model.sha256="${COMMA_DYNAMICS_MODEL_SHA256}"

RUN printf '%s\n' "${COMPANION_SOURCE_SHA256}" | grep -Eq '^[0-9a-f]{64}$' \
    && test "${COMPANION_SOURCE_SHA256}" != "0000000000000000000000000000000000000000000000000000000000000000" \
    && apt-get update \
    && apt-get install --yes --no-install-recommends \
      ca-certificates \
      ffmpeg \
      libgomp1 \
      tini \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid "${APP_GID}" companion \
    && useradd \
      --uid "${APP_UID}" \
      --gid "${APP_GID}" \
      --home-dir /nonexistent \
      --no-create-home \
      --shell /usr/sbin/nologin \
      companion \
    && install -d \
      -o "${APP_UID}" \
      -g "${APP_GID}" \
      /archive/comma-companion \
      /run/companion-secrets \
      /var/lib/comma-companion/database \
      /var/lib/comma-companion/sessions

WORKDIR /app

COPY --from=python-build /app/ /app/
COPY --from=web-build /build/web/dist/ /app/services/comma_companion/web/dist/
COPY --chmod=0555 deploy/entrypoint.py deploy/healthcheck.py deploy/preflight_storage.py /usr/local/lib/comma-companion/
COPY --chmod=0555 deploy/comma-companion-rlog /usr/local/bin/comma-companion-rlog

RUN ln -s /app/services/comma_companion/backend/.venv/bin/comma-companion-api /usr/local/bin/comma-companion-api \
    && ln -s /app/services/comma_companion/backend/.venv/bin/comma-companion-worker /usr/local/bin/comma-companion-worker \
    && ln -s /app/services/comma_companion/backend/.venv/bin/comma-companion-pruner /usr/local/bin/comma-companion-pruner \
    && ln -s /app/services/comma_companion/backend/.venv/bin/comma-companion-hash-password /usr/local/bin/comma-companion-hash-password \
    && ln -s /app/services/comma_companion/media_worker/.venv/bin/comma-companion-media /usr/local/bin/comma-companion-media \
    && ln -s /app/services/comma_companion/adapters/dynamics/.venv/bin/comma-companion-dynamics /usr/local/bin/comma-companion-dynamics \
    && ffmpeg -hide_banner -encoders 2>&1 | grep -Eq '[[:space:]]libsvtav1[[:space:]]' \
    && ffmpeg -hide_banner -encoders 2>&1 | grep -Eq '[[:space:]]libopus[[:space:]]' \
    && ffmpeg -hide_banner -h muxer=webm 2>&1 | grep -q cues_to_front \
    && ffmpeg -hide_banner -h muxer=webm 2>&1 | grep -q reserve_index_space \
    && comma-companion-media --help >/dev/null \
    && comma-companion-rlog --help >/dev/null \
    && printf '%s\n' '{"id":"image-build","method":"model_info","params":{}}' \
      | env COMMA_DYNAMICS_MODEL="${COMMA_DYNAMICS_MODEL_IMAGE_PATH}" \
        comma-companion-dynamics \
      | python -c 'import json, sys; payload = json.loads(sys.stdin.readline()); assert payload["ok"]; assert payload["result"]["model"]["sha256"] == sys.argv[1]' \
        "${COMMA_DYNAMICS_MODEL_SHA256}"

ENV COMMA_DYNAMICS_MODEL=${COMMA_DYNAMICS_MODEL_IMAGE_PATH} \
    COMMA_DYNAMICS_TORCH_THREADS=1 \
    COMPANION_ARCHIVE_ROOT=/archive/comma-companion \
    COMPANION_DATABASE_PATH=/var/lib/comma-companion/database/companion.sqlite3 \
    COMPANION_SESSION_DIR=/var/lib/comma-companion/sessions \
    COMPANION_WEB_ROOT=/app/services/comma_companion/web/dist \
    OMP_NUM_THREADS=1 \
    PYTHONPATH=/app \
    PYTHONUNBUFFERED=1 \
    STARPILOT_COMMIT=${STARPILOT_COMMIT} \
    TMPDIR=/tmp

USER ${APP_UID}:${APP_GID}

EXPOSE 8000
STOPSIGNAL SIGTERM

HEALTHCHECK --interval=60s --timeout=20s --start-period=60s --retries=3 \
  CMD ["python", "/usr/local/lib/comma-companion/healthcheck.py"]

ENTRYPOINT ["/usr/bin/tini", "-g", "--", "python", "/usr/local/lib/comma-companion/entrypoint.py"]
CMD ["api"]
