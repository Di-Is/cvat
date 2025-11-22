# syntax=docker/dockerfile:1.7
FROM nvcr.io/nvidia/cuda:12.8.1-devel-ubuntu22.04 AS sam2-base

ENV PYTHONUNBUFFERED=1

RUN sed -i.bak -r 's@http://(jp\.)?archive\.ubuntu\.com/ubuntu/?@https://ftp.udx.icscoe.jp/Linux/ubuntu/@g' /etc/apt/sources.list
RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        curl \
        git \
        libglib2.0-0 \
        libgl1 \
        ca-certificates \
        build-essential \
    && rm -rf /var/lib/apt/lists/*

RUN curl -LsSf https://astral.sh/uv/install.sh | sh \
    && mv /root/.local/bin/uv /usr/local/bin/uv

WORKDIR /workspace/ai-models/tracker/sam2

COPY ai-models/tracker/sam2/pyproject.toml /workspace/ai-models/tracker/sam2/pyproject.toml
COPY ai-models/tracker/sam2/uv.lock /workspace/ai-models/tracker/sam2/uv.lock
COPY ai-models/tracker/sam2/README.md /workspace/ai-models/tracker/sam2/README.md
COPY cvat-cli /workspace/cvat-cli
COPY cvat-sdk /workspace/cvat-sdk

RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=cache,target=/root/.cache/pip \
    uv sync

ENV PATH="/workspace/ai-models/tracker/sam2/.venv/bin:${PATH}"

COPY ai-models/tracker/sam2/func.py /workspace/ai-models/tracker/sam2/
COPY ai-models/tracker/sam2/utils /workspace/ai-models/tracker/sam2/utils
COPY ai-models/tracker/sam2/experiments /workspace/ai-models/tracker/sam2/experiments

WORKDIR /workspace/ai-models/tracker/sam2

CMD ["uv", "run", "func.py"]
