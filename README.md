# Proton Drive Sync

This application synchronizes a local backup folder to Proton Drive.

## What it does

- Scans a chosen directory recursively
- Uploads changed files and folders to a folder on Proton Drive
- Trashes remote files and folders that were deleted locally
- Preserves folder structure
- Keeps a local state file so unchanged items are skipped on later runs
### With two way sync enabled
- Downloads new and modified files from the folder on Proton Drive
- Deletes files from the local folder when deleted from Proton Drive

## About this project

This is a full manual rewrite of my original vibe coded Proton Drive Sync script  
I have designed and written the logic myself, though I have used AI (Gemini) as a rubber duck and to find out about commands I haven't used before  
The docker_runner.py script was generated as a quick addition for ease of deployment with only a few small manual changes as the script was expanded 

## Requirements
- Python 3.12 or later
- Proton Drive CLI

## Config options
### local_dir
The local directory to synchronize with Proton Drive  .
### remote_dir
The remote directory to upload files to.  
At Proton Drive this always starts with `/my-files/`
### proton_drive_binary
The location of the Proton Drive CLI binary.
### proton_drive_cache_dir (Optional)
The location for Proton Drive to store its cache. When not set it defaults to Proton Drive's default.  
For defaults see https://github.com/ProtonDriveApps/sdk/blob/main/cli/README.md
### unsafe_login (Optional)
When set to true this stores the login information in the cache directory. When set to false or not set it uses your device's credential store.
### empty_trash (Optional)
When set to true the script empties the trash after running and permanently deletes anything in it.
### log_level (Optional)
Set to "quiet", "info" or "debug". Defaults to info when running manually, quiet when run by a cron job.
### two_way (Optional)
Enables two way sync. Useful when you synchronise a directory on two different devices to the same directory on Proton Drive. 
Danger: When a file is changed or deleted remotely it will be changed deleted locally. Do not use for backups. 
### resolve_strategy (Optional)
Used with `two_way`. When a file is changed on multiple devices the script recognizes it as a conflict. Options are:  
Newest: The file with the most recent modification time is saved in its original location. The older file is moved.  
Local:  The file uploaded by the script is always saved in its original location. The existing file is moved.  
Remote: The file already in the remote folder is kept at its original location. The new file is moved.
### trust_remote_state_file (Optional)
Used with two_way. Defaults to true.  
When enabled, the script downloads the most recently uploaded state file and uses it to determine which files to download, avoiding a full crawl of the remote directory on every run.  
Setting this to false makes the script crawl the entire remote directory on every run.  
To detect files uploaded manually to Proton Drive without crawling the entire remote directory, you can configure watch_dir to scan a specific remote directory.
This option was added at the request of the Proton Drive team to reduce unnecessary requests to the server.
### watch_dir (Optional)
Used when trust_remote_state_file is set to true (default). Crawls one remote directory on every run to detect files uploaded manually to Proton Drive.
Set this to the remote path of the directory to watch. It must be a child directory of remote_dir.
For example: `${REMOTE_DIR}/watch`
### post_backup_on_failure (Optional)
Used with Post Backup Hooks. When enabled run the post backup hook even if the script fails.
### webhook_url (Optional)
Send yourself a discord message when the script exits.  
Set this to the Discord Webhook URL of your choice  
Request a webhook URL in the Discord app: https://support.discord.com/hc/en-us/articles/228383668-Intro-to-Webhooks 
### discord_user_id (Optional)
Set this to your Discord user ID if you want the script to tag you


## Prep

Download the appropriate version of Proton Drive CLI from https://proton.me/download/drive/cli/index.html and save it somewhere you can find it. For example in the same directory as proton_drive_sync.py script.

This script uses a config file. Copy the example file and modify its contents:
```bash
cp config.toml.example config.toml
```
```toml
local_dir = "${HOME}/backup"
remote_dir = "/my-files/backup" #Always starts with /my-files/

proton_drive_binary = "${SCRIPT_DIR}/proton-drive" 
# proton_drive_cache_dir = "${SCRIPT_DIR}/.cache" #optional, changes the cache directory from its default location
# unsafe_login = false #Optional. Use if you don't have a credentials store. Defaults to false

# empty_trash = false # Optional. Empties the trash after sync completes. This includes files trashed outside of the script. Defaults to false
# log_level = "info" #Optional. Options are "quiet", "info" and "debug". Defaults to "info" when running in the terminal and "quiet" when loaded by a script.

two_way = false #optional. Setting to true enables 2 way sync between the local and remote directory
# resolve_strategy = "newest" #Optional. Options are "newest", "local" and "remote". Defaults to "newest". losers are stored in ${REMOTE_DIR}/conflict
# trust_remote_state_file = true # Optional. Disables crawling and uses the remote state file to know what to sync. Misses manual uploads. Defaults to true
# watch_dir = "${REMOTE_DIR}/watch" # Optional. Only used with trust_remote_state_file. Crawls this folder for manual uploads

# post_backup_on_failure = false # Optional. Runs the post backup hook even if the main script fails

# webhook_url = "https://discordapp.com/api/webhooks/..." # Optional, send a discord message when the script exits
# discord_user_id = 01234567890123456 # Optional: Ping yourself 
```
*The configuration parser naturally interprets and resolves `${HOME}` (user profile paths), `${REMOTE_DIR}` (The remote directory on Proton Drive) and `${SCRIPT_DIR}` (the local directory hosting the script file) for local_dir, proton_drive_binary and proton_drive_cache_dir, and ${REMOTE_DIR} for watch_dir*

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

# Create a backup copy of your live Vaultwarden database
sqlite3 /path/to/vaultwarden/db.sqlite3 ".backup '/path/to/backup/db_vault_day_${DAY_OF_WEEK}.sqlite3'"
```

Make the hook file executable on your device:
```bash
chmod +x before_run.sh
```
*Note: If the `before_run.sh` script exits with an error status code (anything other than `0`), the master Python engine will automatically abort the synchronization process to protect your remote cloud backups from corruption.*


## Post-Backup Hooks (Optional)

The sync engine also supports executing an optional custom shell script immediately after the synchronization cycle ends. This can be used to clean up files created in the pre-backup hook or taking applications back out of mainainance mode.  
By default the script only runs if the sync succeeded, but you can enable the config variable `post_backup_on_failure` to always run.

If a script file named `after_run.sh` is placed inside the same directory as `proton_drive_sync.py`, it will be executed automatically.

### Example: Nextcloud maintainance mode
Create a `before_run.sh` file:
```bash
nano before_run.sh
```

Enable maintainance mode (Ensure the file begins with a proper Shebang interpreter declaration):
```bash
#!/bin/bash
# --- SET MAINTENANCE MODE ---
echo "Activating Nextcloud Maintenance Mode..."
docker exec --user www-data nextcloud-app php occ maintenance:mode --on
```

Create an `after_run.sh` file:
```bash
nano after_run.sh
```

Disable maintainance mode:
```bash
#!/bin/bash
# --- SET MAINTENANCE MODE ---
echo "🔓 Ensuring Nextcloud Maintenance Mode is disabled..."
docker exec --user www-data nextcloud-app php occ maintenance:mode --off || true
```

Make the hook files executable on your device:
```bash
chmod +x after_run.sh before_run.sh
```


## Migrating from the old bash script

The structure of the .proton_sync_state file from the old script is incompatible with the one from the new script. Delete it and do one full run. Proton Drive CLI will automatically disregard unmodified files after uploading. 

## Usage

After installation always run the script manually at least once in order to log into Proton Drive:
```bash
python3 proton_drive_sync.py
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

When not used with docker the script keeps a local state file here:

    `${LOCAL_DIR}/.proton_sync_state`

This file lets the script detect what changed since the previous run.
When used with Docker the state file is stored in the cache directory. 

## Docker Installation

It is also possible to run this script in a docker container  
The Docker Hub image is compatible with linux/x64 and linux/arm64  
The arm64 version was tested on a Raspberry Pi 3 Model B.

By default your local folder is mounted as **Read-Only (`:ro`)**, so the statefile is stored outside of the folder. It will be synced to Proton Drive but won't be in the local folder  
If you enable two-way sync the local folder is mounted as **Read-Write**, but the statefile is still kept outside of the folder so you can always revert to one-way

### Configure via Docker Compose
Create an empty directory on your device, open a file named `docker-compose.yml`, and paste the following setup:

```yaml
services:
  proton-sync:
    image: d4m4s74/proton-drive-sync:latest
    container_name: proton-sync
    restart: unless-stopped
    environment:
      - USER=1000                         # Optional: Set user and group to your user so downloaded files aren't owned by root.
      - GROUP=1000                        # Optional: Set user and group to your user so downloaded files aren't owned by root.
      - INTERVAL_HOURS=24                 # Number of hours between sync
      # - START_HOUR=2                    # Optional: Un-comment to align initial run (e.g., 2 AM)
      - REMOTE_DIR=/my-files/backup       # Must always begin with /my-files/
      - EMPTY_TRASH=false                 # Optional: Set to true to automatically purge the remote trash folder post-sync
      - TWO_WAY=false                     # Optional: Enables two-way sync
      # - RESOLVE_STRATEGY=newest           # Optional. Options are newest, local and remote. Defaults to newest. Losers are stored in ${REMOTE_DIR}/conflict
      # - LOG_LEVEL=quiet                   # Optional: Options are quiet, info and debug. Defaults to quiet
      # - TRUST_REMOTE_STATE_FILE=true      # Optional: Disables remote crawl (unless there is no state file). Defaults to True
      # - WATCH_DIR=/my-files/backup/watch  # Optional: Only used when using the remote state file. Scans this dir for manually added files.
      # - POST_BACKUP_ON_FAILURE=false      # Optional: Runs the post backup hook even if the sync fails.
      # - WEBHOOK_URL=https://discordapp.com/api/webhooks/...
      # - DISCORD_USER_ID=01234567890123456

    volumes:
      # Map your host local source folder to the container's data workspace
      # For One-Way Sync / Backup Mode (Read-Only)
      - /path/to/local/backup:/data/backup:ro
      # For Two-Way Sync Mode (Read-Write)
      # - /path/to/local/backup:/data/backup
      
      # Map a local folder to persist your authentication and state file across updates
      - ./proton_sync:/app/.cache
      
      # Optional: Map a pre-backup hook
      # - ./before_run.sh:/app/before_run.sh
```
Modify the file for your local setup.

Launch the container infrastructure:
```bash
docker compose up -d
```

### Interactive Authentication
The first time using the script you have to log into proton drive. To do that use the following command and follow the instructions in the terminal
```bash
docker exec -it proton-sync ./proton-drive auth login
```

To see if the login and synchronization were succesful you can check the logs by using the following command
```bash
docker logs -f proton-sync
```

## Notes

- The script is cautious about deletions and uses Proton Drive's trash mechanism
- It preserves folder structure on Proton Drive
- Paths with spaces are supported
- It is designed for incremental nightly backup sync, not full replacement of a tool like rsync
