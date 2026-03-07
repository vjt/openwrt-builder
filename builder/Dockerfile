FROM debian:trixie-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
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
    && rm -rf /var/lib/apt/lists/*

# Install opkg-utils (upstream git.openwrt.org repo was removed)
RUN git clone https://github.com/oso/opkg-utils.git /opt/opkg-utils \
    && ln -s /opt/opkg-utils/opkg-make-index /usr/local/bin/opkg-make-index \
    && ln -s /opt/opkg-utils/opkg-build /usr/local/bin/opkg-build

WORKDIR /app

COPY requirements.txt .
RUN pip3 install --break-system-packages -r requirements.txt

COPY *.py ./
COPY tests/ ./tests/

# Create directories
RUN mkdir -p /feed /opt/sdk /opt/repos /tmp/build-logs

CMD ["python3", "main.py"]
