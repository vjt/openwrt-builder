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
    qemu-user-static \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip3 install --break-system-packages -r requirements.txt

COPY *.py ./
COPY tests/ ./tests/

# Create directories and configure SSH for private repos
RUN mkdir -p /feed /opt/sdk /opt/repos /tmp/build-logs /root/.ssh \
    && printf "StrictHostKeyChecking no\nIdentityFile /root/.ssh/builder.key\n" > /root/.ssh/config

CMD ["python3", "main.py"]
