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

    0 2 * * * /usr/bin/python3 /path/to/your/project/proton_backup.py

This means:

- `0 ` = minute 0
- `2 ` = hour 2 AM
- ` * ` = every day of the month
- ` * ` = every month
- `* ` = every day of the week

So the script will run every night at 2:00 AM.

### Logging with cron

If you want to save output to a log file, you can redirect stdout and stderr:

    0 2 * * * /usr/bin/python3 /path/to/your/project/proton_backup.py >> /path/to/your/project/backup_runs.log 2>&1

That means:

- `>> /path/to/your/project/backup_runs.log` appends normal output to the log 
- `2>&1` sends error output into the same log file

## State file

The script keeps a local state file here:

    `${LOCAL_DIR}/.proton_sync_state`

This file lets the script detect what changed since the previous run.

## Notes

- The script is cautious about deletions and uses Proton Drive's trash mechanism
- It preserves folder structure on Proton Drive
- Paths with spaces are supported
- It is designed for incremental nightly backup sync, not full replacement of a tool like rsync
