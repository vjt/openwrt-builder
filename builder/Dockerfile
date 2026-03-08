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

WORKDIR /app

COPY requirements.txt .
RUN pip3 install --break-system-packages -r requirements.txt

COPY *.py ./
COPY tests/ ./tests/

# Create directories and configure SSH for private repos
RUN mkdir -p /feed /opt/sdk /opt/repos /tmp/build-logs /root/.ssh \
    && printf "StrictHostKeyChecking no\nIdentityFile /runtime/ssh.key\n" > /root/.ssh/config

CMD ["python3", "main.py"]
