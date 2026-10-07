# syntax=docker/dockerfile:1
# kick-tts image: python 3.12 slim, torch (CPU wheel) + pinned requirements + sha256-verified weights baked in.
#   docker build --build-arg EMA_REVISION=<hf commit sha> -t kick-tts:<tag> .

# ---------------------------------------------------------------- builder
FROM python:3.12-slim AS builder

ARG TORCH_VERSION=2.14.1
# Hugging Face commit sha of the EMA Lightning weights. Required: the build fails when empty.
ARG EMA_REVISION

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:${PATH}"

# torch from the PyTorch CPU index only; no other torch-family packages are installed.
RUN pip install --no-cache-dir --index-url https://download.pytorch.org/whl/cpu torch==${TORCH_VERSION}

WORKDIR /build
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Weights: pinned revision, sha256 verified against weights.lock.json.
# fetch_weights.py exits non-zero on a hash mismatch or on the placeholder lock.
COPY app ./app
COPY scripts ./scripts
COPY weights.lock.json ./
RUN if [ -z "${EMA_REVISION}" ]; then echo "ERROR: --build-arg EMA_REVISION=<sha> is required" >&2; exit 1; fi
RUN python scripts/fetch_weights.py --revision "${EMA_REVISION}" --out /opt/weights --lock weights.lock.json

# ---------------------------------------------------------------- runtime
FROM python:3.12-slim AS runtime

ENV HF_HUB_OFFLINE=1 \
    OMP_NUM_THREADS=2 \
    MKL_NUM_THREADS=2 \
    TORCH_NUM_THREADS=2 \
    EMA_WEIGHTS_DIR=/opt/weights \
    XDG_CACHE_HOME=/cache \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:${PATH}"

RUN groupadd --gid 1000 app \
    && useradd --uid 1000 --gid 1000 --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin app \
    && mkdir -p /cache \
    && chown 1000:1000 /cache

COPY --from=builder /opt/venv /opt/venv
COPY --from=builder /opt/weights /opt/weights

WORKDIR /srv
COPY app ./app
COPY scripts ./scripts
COPY weights.lock.json ./

USER 1000:1000
EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--proxy-headers"]
