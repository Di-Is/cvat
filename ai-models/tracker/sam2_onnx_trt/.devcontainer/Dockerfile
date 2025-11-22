FROM nvcr.io/nvidia/cuda:12.8.1-devel-ubuntu22.04

# Align with template: remove default ubuntu user for fresh UID mapping.
RUN userdel -f -r ubuntu || true

ENV DEBIAN_FRONTEND=noninteractive

# Mirror change to a JP mirror for speed (template behavior).
RUN sed -i.bak -r 's@http://(jp\.)?archive\.ubuntu\.com/ubuntu/?@https://ftp.udx.icscoe.jp/Linux/ubuntu/@g' /etc/apt/sources.list

RUN --mount=type=cache,target=/var/lib/apt/lists \
    --mount=type=cache,target=/var/cache/apt/archives \
    apt update && \
    apt install -y --no-install-recommends ca-certificates

RUN --mount=type=cache,target=/var/lib/apt/lists \
    --mount=type=cache,target=/var/cache/apt/archives \
    apt upgrade -y

RUN --mount=type=cache,target=/var/lib/apt/lists \
    --mount=type=cache,target=/var/cache/apt/archives \
    apt install -y --no-install-recommends \
    locales \
    locales-all
RUN locale-gen en_US.UTF-8

RUN --mount=type=cache,target=/var/lib/apt/lists \
    --mount=type=cache,target=/var/cache/apt/archives \
    apt install -y --no-install-recommends \
    curl \
    git \
    sudo \
    vim

# Install mise (used for uv/python management).
RUN curl https://mise.run | MISE_INSTALL_PATH="/usr/local/bin/mise" sh
RUN mkdir -p /etc/mise && \
    echo "[settings]\ntrusted_config_paths = ['~/']" > /etc/mise/config.toml && \
    echo 'eval \"$(mise activate bash)\"' >> /etc/bash.bashrc

ARG USER_ID
ARG _USERNAME=ubuntu
ARG _HOME=/home/${_USERNAME}
ARG _WORKSPACE=${_HOME}/workspace

RUN groupadd -g ${USER_ID} ${_USERNAME} && \
    useradd -m -u ${USER_ID} -g ${USER_ID} -s /bin/bash ${_USERNAME}
RUN echo "${_USERNAME} ALL=(ALL) NOPASSWD:ALL" >> /etc/sudoers

USER ${_USERNAME}
WORKDIR ${_WORKSPACE}

ENV PATH=${PATH}:${_HOME}/.local/bin

RUN mkdir ${_HOME}/.vscode-server ${_HOME}/.local ${_HOME}/.config
