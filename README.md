# Proton Drive Sync Script

This script synchronizes a local backup folder to Proton Drive.

## What it does

- Scans /srv/data/backup recursively
- Uploads changed files and folders to /my-files/backup
- Trashes remote files and folders that were deleted locally
- Preserves folder structure
- Keeps a local state file so unchanged items are skipped on later runs

## About this project

This script was mainly created with the help of AI (Gemini and ChatGPT) as a way to practice using AI to write code and refine shell scripts interactively.

That process helped with:

- designing the sync logic
- iterating on edge cases
- testing behavior against the Proton Drive CLI
- improving robustness over several revisions
- working around "unintended features" (interface leaks) in the ChatGPT interface (just for fun, try asking ChatGPT to generate a readme.md file)

## Prep

Before running the script for the first time:
1. Make sure the Proton Drive CLI is installed and if necessary modify the `PROTON_BIN` variable to point to its location.
2. Authenticate with Proton Drive:

    ./proton-drive auth login

    This will open your browser so you can sign in.

 3. If your environment does not have a working keyring / secret store, uncomment these lines in the script:

    export PROTON_DRIVE_CREDENTIALS_STORE=unsafe_file
    export PROTON_DRIVE_CACHE_DIR="$HOME/.config/proton-drive-cli"

4. Make sure the remote backup folder exists or modify the `REMOTE_BACKUP_DIR` variable:

    /my-files/backup

5. Make sure the local backup folder exists or modify the `LOCAL_BACKUP_DIR` variable:

    /srv/data/backup

## Usage

You can run the script manually:

    ./proton_drive_sync.sh

This is useful for testing or running a sync on demand.

### Running it automatically with cron

cron is a scheduler built into Linux that runs commands at specific times.

To make this script run automatically every night, you add a line to your crontab.

Open your crontab editor with:

    crontab -e

Then add a line like this:

    0 2 * * * /foo/bar/proton_drive_sync.sh

This means:

- `0 ` = minute 0
- `2 ` = hour 2 AM
- ` * ` = every day of the month
- ` * ` = every month
- `* ` = every day of the week

So the script will run every night at 2:00 AM.

### Logging with cron

If you want to save output to a log file, you can redirect stdout and stderr:

    0 2 * * * /foo/bar/proton_drive_sync.sh >> /foo/bar/proton_backup.log 2>&1

That means:

- `>> /foo/bar/proton_backup.log` appends normal output to the log 
- `2>&1` sends error output into the same log file

## State file

The script keeps a local state file here:

    ${LOCAL_BACKUP_DIR}/.proton_sync_state

This file lets the script detect what changed since the previous run.

## Notes

- The script is cautious about deletions and uses Proton Drive's trash mechanism
- It preserves folder structure on Proton Drive
- Paths with spaces are supported
- It is designed for incremental nightly backup sync, not full replacement of a tool like rsync
