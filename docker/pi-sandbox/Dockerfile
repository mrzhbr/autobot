FROM node:22-bookworm-slim

ARG PI_VERSION=0.75.5

ENV DEBIAN_FRONTEND=noninteractive \
    NPM_CONFIG_UPDATE_NOTIFIER=false \
    NPM_CONFIG_FUND=false

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        bash \
        build-essential \
        ca-certificates \
        git \
        python-is-python3 \
        python3 \
        python3-pip \
        python3-venv \
        ripgrep \
    && npm install -g --ignore-scripts "@earendil-works/pi-coding-agent@${PI_VERSION}" \
    && pi --version \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*
