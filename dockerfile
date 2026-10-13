FROM python:3.12-slim

# 1. Establish the workspace and install curl to grab the binary during the build phase
WORKDIR /app

RUN apt-get update && apt-get install -y \
    curl \
    && rm -rf /var/lib/apt/lists/*

# 2. Pull the canonical Linux x64 binary directly from the Proton servers. replace with the correct file for your system
RUN curl -fsSL -o proton-drive https://proton.me/download/drive/cli/0.9.0/linux-x64/proton-drive \ 
    && chmod +x proton-drive

# 3. Layer your custom synchronization script engines into the directory space
COPY proton_drive_sync.py .
COPY docker_runner.py .

# 4. Initialize structural mount points
RUN mkdir -p /app/.cache

# --- MANDATORY EMBEDDED ENGINE DEFAULTS ---
ENV AM_I_DOCKER="true"
ENV PROTON_DRIVE_CACHE_DIR="/app/.cache"
ENV PROTON_DRIVE_CREDENTIALS_STORE="unsafe_file"

# --- RUNNER CADENCE CONFIGURATIONS ---
ENV INTERVAL_HOURS=24
ENV REMOTE_DIR="/my-files/backup"
ENV EMPTY_TRASH="false"

CMD ["python3", "docker_runner.py"]
