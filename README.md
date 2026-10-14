# Proton Drive Sync

This application synchronizes a local backup folder to Proton Drive.

## What it does

- Scans a chosen directory recursively
- Uploads changed files and folders to a folder on Proton Drive
- Trashes remote files and folders that were deleted locally
- Preserves folder structure
- Keeps a local state file so unchanged items are skipped on later runs

## About this project

This is a full manual rewrite of my original vibe coded Proton Drive Sync script  
I have designed and written the logic myself, though I have used AI (Gemini) as a rubber duck and to find out about commands I haven't used before  
The docker_runner.py script was generated as a quick addition for ease of deployment with only a few small manual changes to reduce complexity

## Prep

This script uses a config file. Copy the example file and modify its contents:
```bash
cp config.toml.example config.toml
```
```toml
local_dir = "${HOME}/backup"
remote_dir = "/my-files/backup"  # Must always begin with /my-files/
proton_drive_binary = "${SCRIPT_DIR}/proton-drive" 
proton_drive_cache_dir = "${SCRIPT_DIR}/.cache"  # Optional path override

unsafe_login = false  # Set to true if headless/no keyring present
empty_trash = false   # Set to true to automatically purge the remote trash folder post-sync

# log_level = "info"  # Optional: "quiet", "info", or "debug"
```
*The configuration parser naturally interprets and resolves both `${HOME}` (user profile paths) and `${SCRIPT_DIR}` (the local directory hosting the script file) for local_dir, proton_drive_binary and proton_drive_cache_dir.*

## Pre-Backup Hooks (Optional)

The sync engine natively supports executing an optional custom shell script immediately before the synchronization cycle begins. This is ideal for performing local database dumps, hot backups, or service snapshot maintenance tasks right before the code maps files to the cloud.

If a script file named `before_run.sh` is placed inside the same directory as `proton_drive_sync.py`, it will be executed automatically.

### Example: Vaultwarden SQLite Hot Backups
Create a `before_run.sh` file:
```bash
nano before_run.sh
```

Paste your automated shell commands (Ensure the file begins with a proper Shebang interpreter declaration):
```bash
#!/bin/bash
# Get the numeric day of the week (1-7)
DAY_OF_WEEK=$(date +%u)

# Create an online hot-copy copy of your live Vaultwarden database
sqlite3 /path/to/vaultwarden/db.sqlite3 ".backup '/path/to/backup/db_vault_day_${DAY_OF_WEEK}.sqlite3'"
```

Make the hook file executable on your server:
```bash
chmod +x before_run.sh
```
*Note: If the `before_run.sh` script exits with an error status code (anything other than `0`), the master Python engine will automatically abort the synchronization process to protect your remote cloud backups from corruption.*


## Migrating from the old bash script

The structure of the .proton_sync_state file from the old script is incompatible with the one from the new script. Delete it and do one full run. Proton Drive CLI will automatically disregard unmodified files after uploading. 

## Usage

After installation always run the script manually at least once in order to log into Proton Drive:
```bash
python3 proton_backup.py
```

### Running it automatically with cron

cron is a scheduler built into Linux that runs commands at specific times.

To make this script run automatically every night, you add a line to your crontab.

Open your crontab editor with:

    crontab -e

Then add a line like this:

    0 2 * * * /usr/bin/python3 /path/to/your/project/proton_drive_sync.py

This means:

- `0 ` = minute 0
- `2 ` = hour 2 AM
- ` * ` = every day of the month
- ` * ` = every month
- `* ` = every day of the week

So the script will run every night at 2:00 AM.

### Logging with cron

If you want to save output to a log file, you can redirect stdout and stderr:

    0 2 * * * /usr/bin/python3 /path/to/your/project/proton_drive_sync.py >> /path/to/your/project/backup_runs.log 2>&1

That means:

- `>> /path/to/your/project/backup_runs.log` appends normal output to the log 
- `2>&1` sends error output into the same log file

## State file

The script keeps a local state file here:

    `${LOCAL_DIR}/.proton_sync_state`

This file lets the script detect what changed since the previous run.

## Docker Installation

It is also possible to run this script in a docker container.  
Note: Currently only for linux/x64

Your backup directory is mounted as **Read-Only (`:ro`)**, so the statefile is stored outside of the backup. It will be synced to Proton Drive but won't be in the local folder

### Configure via Docker Compose
Create an empty directory on your server, open a file named `docker-compose.yml`, and paste the following setup:

```yaml
services:
  proton-sync:
    image: d4m4s74/proton-drive-sync:latest
    container_name: proton-sync
    restart: unless-stopped
    environment:
      - INTERVAL_HOURS=24
      # - START_HOUR=2          # Optional: Un-comment to align initial run (e.g., 2 AM)
      - REMOTE_DIR=/my-files/backup  # Must always begin with /my-files/
      - EMPTY_TRASH=false       # Optional: Set to true to automatically purge the remote trash folder post-sync
    volumes:
      # Map your host local backup source folder to the container's data workspace as Read-Only
      - /path/to/your/local/backup:/data/backup:ro
      # Map a local folder to persist your authentication and state file across updates
      - ./proton_sync:/app/.cache
```

Launch the container infrastructure:
```bash
docker compose up -d
```

### Interactive Authentication
The first time using the script you have to log into proton drive. To do that use the following command and follow the instructions in the terminal
```bash
docker exec -it proton-sync ./proton-drive auth login
```

To see if the login and syncronisation were succesful you can check the logs by using the following command
```bash
docker logs -f proton-sync
```

## Notes

- The script is cautious about deletions and uses Proton Drive's trash mechanism
- It preserves folder structure on Proton Drive
- Paths with spaces are supported
- It is designed for incremental nightly backup sync, not full replacement of a tool like rsync
