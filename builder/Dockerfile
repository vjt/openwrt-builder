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

# Install opkg-utils from upstream
RUN git clone https://git.yoctoproject.org/opkg-utils /opt/opkg-utils \
    && ln -s /opt/opkg-utils/opkg-make-index /usr/local/bin/opkg-make-index \
    && ln -s /opt/opkg-utils/opkg-build /usr/local/bin/opkg-build

WORKDIR /app

COPY requirements.txt .
RUN pip3 install --break-system-packages -r requirements.txt

COPY *.py ./
COPY tests/ ./tests/

# Create directories and configure SSH for private repos
RUN mkdir -p /feed /opt/sdk /opt/repos /tmp/build-logs /root/.ssh \
    && printf "StrictHostKeyChecking no\nIdentityFile /root/.ssh/id\n" > /root/.ssh/config

# Fix SSH key permissions at startup (bind mounts inherit host perms, and :ro prevents chmod)
CMD ["sh", "-c", "if [ -f /root/.ssh/builder.key ]; then cp /root/.ssh/builder.key /root/.ssh/id && chmod 600 /root/.ssh/id; fi; exec python3 main.py"]
