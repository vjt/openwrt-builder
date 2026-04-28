FROM debian:trixie-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    cmake \
    libncurses-dev \
    gawk \
    git \
    wget \
    curl \
    python3 \
    python3-pip \
    python3-venv \
    file \
    rsync \
    unzip \
    zstd \
    gzip \
    xz-utils \
    ca-certificates \
    openssh-client \
    qemu-user-static \
    tmux \
    && rm -rf /var/lib/apt/lists/*

# Build and install usign (OpenWrt's ed25519 signing tool) for feed signing
RUN git clone https://github.com/openwrt/usign.git /tmp/usign && \
    cd /tmp/usign && cmake . && make && cp usign /usr/local/bin/ && \
    rm -rf /tmp/usign

# Install hcloud CLI for Hetzner remote builds
RUN ARCH=$(dpkg --print-architecture) && \
    wget -q -O /tmp/hcloud.tar.gz \
    "https://github.com/hetznercloud/cli/releases/latest/download/hcloud-linux-${ARCH}.tar.gz" && \
    tar xzf /tmp/hcloud.tar.gz -C /usr/local/bin hcloud && \
    rm /tmp/hcloud.tar.gz

# Non-root runtime user. UID/GID 1000 matches the typical host user so
# bind-mounted ./runtime and ./feed stay host-owned. Override with
# `user: "UID:GID"` in docker-compose if your host user is different.
ARG BUILDER_UID=1000
ARG BUILDER_GID=1000
RUN groupadd --gid ${BUILDER_GID} builder && \
    useradd --uid ${BUILDER_UID} --gid ${BUILDER_GID} \
            --create-home --shell /bin/bash builder

WORKDIR /app

COPY requirements.txt .
RUN pip3 install --break-system-packages -r requirements.txt

COPY *.py ./
COPY tests/ ./tests/

# Directories the runtime writes into. Named volumes (/opt/sdk, /opt/repos)
# inherit ownership from the image on first init, so chown-ing them here
# lets the non-root user write without a host-side chown. Bind mounts
# (/runtime, /feed) keep host-side ownership — the compose recipe expects
# those to be UID 1000 on the host.
RUN mkdir -p /feed /opt/sdk /opt/repos /app && \
    chown -R builder:builder /feed /opt/sdk /opt/repos /app

# SSH config for private git repos. Lives in the user home so ssh picks it
# up without /root access.
RUN mkdir -p /home/builder/.ssh && \
    printf "StrictHostKeyChecking no\nUserKnownHostsFile /dev/null\nIdentityFile /runtime/ssh.key\n" \
        > /home/builder/.ssh/config && \
    chown -R builder:builder /home/builder/.ssh && \
    chmod 700 /home/builder/.ssh && \
    chmod 600 /home/builder/.ssh/config

USER builder

CMD ["python3", "main.py"]
