# syntax=docker/dockerfile:1.7
FROM pytorch/pytorch:2.4.1-cuda12.1-cudnn9-runtime AS sam2-base

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        curl \
        git \
        libglib2.0-0 \
        libgl1 \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN curl -LsSf https://astral.sh/uv/install.sh | sh \
    && mv /root/.local/bin/uv /usr/local/bin/uv \
    && mv /root/.local/bin/uvx /usr/local/bin/uvx

WORKDIR /workspace/ai-models/interactor/sam2

FROM sam2-base AS sam2-deps

COPY ai-models/interactor/sam2/pyproject.toml /workspace/ai-models/interactor/sam2/pyproject.toml
COPY ai-models/interactor/sam2/uv.lock /workspace/ai-models/interactor/sam2/uv.lock
COPY ai-models/interactor/sam2/README.md /workspace/ai-models/interactor/sam2/README.md
COPY cvat-cli /workspace/cvat-cli
COPY cvat-sdk /workspace/cvat-sdk

RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=cache,target=/root/.cache/pip \
    uv sync --frozen

FROM sam2-base

COPY cvat-cli /workspace/cvat-cli
COPY cvat-sdk /workspace/cvat-sdk
COPY ai-models/interactor/sam2 /workspace/ai-models/interactor/sam2
COPY --from=sam2-deps /workspace/ai-models/interactor/sam2/.venv /workspace/ai-models/interactor/sam2/.venv

ENV PATH="/workspace/ai-models/interactor/sam2/.venv/bin:${PATH}"

CMD ["uv", "run", "python", "func.py"]
