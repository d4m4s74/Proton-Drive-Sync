# 1. Upgrade to the newest Python runtime branch
FROM python:3.13-slim

# Establish the workspace
WORKDIR /app

# Expose the target architecture variable automatically supplied by Docker Buildx
ARG TARGETARCH

# 2. Combine apt actions, use curl, dynamically map architecture, and immediately purge in a single layer
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && if [ "$TARGETARCH" = "arm64" ]; then \
         ARCH_DIR="linux-arm64"; \
       else \
         ARCH_DIR="linux-x64"; \
       fi \
    && curl -fsSL -o proton-drive "https://proton.me/download/drive/cli/0.9.0/${ARCH_DIR}/proton-drive" \
    && chmod +x proton-drive \
    && apt-get purge -y --auto-remove curl \
    && rm -rf /var/lib/apt/lists/*

# 3. Layer your custom synchronization script engines into the directory space
COPY proton_drive_sync.py .
COPY docker_runner.py .

# 4. Initialize structural mount points
RUN mkdir -p /app/.cache && chmod -R 777 /app

# --- MANDATORY EMBEDDED ENGINE DEFAULTS ---
ENV AM_I_DOCKER="true"
ENV PROTON_DRIVE_CACHE_DIR="/app/.cache"
ENV PROTON_DRIVE_CREDENTIALS_STORE="unsafe_file"

# --- RUNNER CADENCE CONFIGURATIONS ---
ENV INTERVAL_HOURS=24
ENV REMOTE_DIR="/my-files/backup"
ENV EMPTY_TRASH="false"

CMD ["python3", "docker_runner.py"]
