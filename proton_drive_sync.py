import os
import subprocess
import sys
import tomllib
import pathlib
import logging
import time
import json
import shutil
import atexit
import random
import tempfile
from datetime import datetime

class ProtonCliError(Exception):
    """Raised when a Proton Drive CLI command fails with stderr output."""
    def __init__(self, cmd, stderr):
        self.cmd = cmd
        self.stderr = stderr
        super().__init__(f"Command '{' '.join(cmd)}' failed:\n{stderr}")

def is_path_readonly(path: str) -> bool:
    """Check if a given directory path is read-only by attempting a temporary write test."""
    abs_path = os.path.abspath(path)
    if not os.path.exists(abs_path):
        return False
        
    test_file_path = os.path.join(abs_path, ".sync_write_test")
    try:
        with open(test_file_path, "w") as f:
            f.write("test")
        os.remove(test_file_path)
        return False  # Writable
    except (PermissionError, OSError):
        return True   # Caught read-only filesystem block

def run_proton_cli(args, fail=True):
    """Helper to execute Proton Drive CLI commands, log results, and exit on error."""
    cmd = [PROTON_DRIVE_BINARY] + args
    logging.debug(f"Executing CLI command: {' '.join(cmd)}")
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.stderr.strip() and fail:
        logging.error(f"Unexpected CLI error for command '{' '.join(cmd)}':\n{result.stderr}")
        raise ProtonCliError(cmd, result.stderr)
    return result 

def local_path_to_remote(path):
    return os.path.join(REMOTE_DIR,os.path.relpath(path, LOCAL_DIR))

def remote_path_to_local(path):
    os.path.join(LOCAL_DIR,os.path.relpath(path, REMOTE_DIR))

#local walk
def local_walk():
    filenames = set()
    meta = set()
    for root, dirs, files in os.walk(LOCAL_DIR):
        for name in files:
            if name == STATE_FILE_NAME or name == ".proton_sync.lock":
                continue  # Skip tracking the state file itself!
            path = os.path.join(root,name)
            filenames.add(path)
            meta.add(f"f::{path}::{int(os.path.getmtime(path))}::{os.path.getsize(path)}")
        for name in dirs:
            path = os.path.join(root,name)
            if path == CONFLICT_DIR:
                continue #skip tracking conflict dir itself
            filenames.add(path)
            meta.add(f"d::{path}")
    return meta, filenames

#remote crawl
def remote_crawl(path=""):
    logging.debug(f"Crawling remote folder {os.path.join(REMOTE_DIR,path)}")
    meta = set()
    result = run_proton_cli(['filesystem', 'list', os.path.join(REMOTE_DIR,path), '-j'])
    logging.debug(f"Remote crawl CLI execution results: {result}")
    items = json.loads(result.stdout)
    for item in items:
        filename = item['name']['value']
        if filename == STATE_FILE_NAME or filename == ".proton_sync.lock":
            continue
        itemstring = ""
        folder = False
        if item['type'] == "folder":
            folder = True
            itemstring += "d"
        else:
            itemstring += "f"
        itemstring += f"::{os.path.join(LOCAL_DIR,path,filename)}"
        if not folder:
            dt = datetime.fromisoformat(item['activeRevision']['claimedModificationTime'].replace("Z", "+00:00"))
            epoch_int = int(dt.timestamp())
            itemstring += f"::{epoch_int}::{item['activeRevision']['claimedSize']}"
        if os.path.join(LOCAL_DIR,path,filename) != CONFLICT_DIR: #skip adding conflict dir because times never match
            meta.add(itemstring)
        if folder:
            meta.update(remote_crawl(os.path.join(path,filename)))
    return meta

def calculate_remote_changes(meta, filenames, old_meta): #Calculates things to change on the remote server
    upload = meta - old_meta #every file/metadata combo that is in the new set but not in the old set. Contains inefficiencies.
    total_uploaded = len(upload)
    temp = upload.copy()
    for file in temp:
        path = file.split('::')[1]
        if f"d::{os.path.split(path)[0]}" in temp: #If the folder is new too you don't have to upload separately
            upload.remove(file)
    total_replaced = 0
    delete = old_meta - meta #every file/metadata that is in the old set but not the new set. Contains false positives.
    temp = delete.copy()
    for file in temp:
        path = file.split('::')[1]
        if path in filenames: #If the file is being replaced, not removed, do not count as deletion.
            total_replaced += 1
    total_deleted = len(delete) - total_replaced
    
    total_uploaded = total_uploaded - total_replaced
    temp = delete.copy()
    for file in temp:
        path = file.split('::')[1]
        if f"d::{os.path.split(path)[0]}" in temp: #If the folder is to be deleted too you don't have to delete separately
            delete.remove(file)
    logging.debug(f"Calculated change sets: {len(upload)} items to upload, {len(delete)} items to trash.")
    return upload, total_uploaded, total_replaced, delete, total_deleted

def calculate_local_changes(meta, old_meta, remote_meta): #calculate things to change in the local folder
    download = remote_meta - old_meta #contains inefficiencies
    total_downloaded = len(download)
    temp = download.copy()
    all_downloads = download.copy() #used for restoring dates/times after download
    for file in temp:
        path = file.split('::')[1]
        if f"d::{os.path.split(path)[0]}" in temp: #If the folder is new too you don't have to download separately
            download.remove(file)
    local_delete = old_meta - remote_meta #contains inefficiencies
    #calculate the remote filenames
    filenames = set()
    for item in remote_meta:
        filenames.add(item.split("::")[1])
    temp = local_delete.copy()
    total_replaced = 0
    for file in temp:
        path = file.split('::')[1]
        if path in filenames: #If the file is being replaced, not removed, remove from the delete count.
            total_replaced += 1
    total_deleted = len(local_delete) - total_replaced
    total_downloaded -= total_replaced
    temp = local_delete.copy()
    for file in temp:
        path = file.split('::')[1]
        if f"d::{os.path.split(path)[0]}" in temp: #If the folder is to be deleted too you don't have to delete separately
            local_delete.remove(file)
    logging.debug(f"Calculated change sets: {len(download)} items to download, {len(local_delete)} items to trash.")
    return download, all_downloads, total_downloaded, total_replaced, local_delete, total_deleted

def resolve_conflicts(download, upload, local_delete, remote_delete): #note, updates the given sets.
    num_conflicts = 0
    #First split up the meta lines for easy search
    download_files = dict()
    download_folders = set()
    upload_files = dict()
    upload_folders = set()
    local_delete_files = dict()
    local_delete_folders = set()
    remote_delete_files = dict()
    remote_delete_folders = set()
    for item in download:
        parts = item.split("::")
        if parts[0] == 'f':
            download_files[parts[1]] = (parts[2],parts[3])
        else:
            download_folders.add(parts[1])
    for item in upload:
        parts = item.split("::")
        if parts[0] == 'f':
            upload_files[parts[1]] = (parts[2],parts[3])
        else:
            upload_folders.add(parts[1])
    for item in local_delete:
        parts = item.split("::")
        if parts[0] == 'f':
            local_delete_files[parts[1]] = (parts[2],parts[3])
        else:
            local_delete_folders.add(parts[1])
    for item in remote_delete:
        parts = item.split("::")
        if parts[0] == 'f':
            remote_delete_files[parts[1]] = (parts[2],parts[3])
        else:
            remote_delete_folders.add(parts[1])
    
    #upload vs deletion. Upload always wins.
    for name in upload_files:
        if name in local_delete_files:
            num_conflicts += 1
            logging.info(f"Conflict deleted file or folder {name} uploaded to remote")
            local_delete.remove(f"f::{name}::{local_delete_files[name][0]}::{local_delete_files[name][1]}")
        if name in local_delete_folders:
            local_delete.remove(f"d::{name}")
            num_conflicts += 1
            logging.info(f"Conflict deleted file or folder {name} uploaded to remote")

    #download vs deletion. download always wins.
    for name in download_files:
        if name in remote_delete_files:
            remote_delete.remove(f"f::{name}::{remote_delete_files[name][0]}::{remote_delete_files[name][1]}")
            num_conflicts += 1
            logging.info(f"Conflict deleted file or folder {name} redownloaded from remote")
        if name in remote_delete_folders:
            remote_delete.remove(f"d::{name}")
            num_conflicts += 1
            logging.info(f"Conflict deleted file or folder {name} redownloaded from remote")

    if RESOLVE_STRATEGY == "remote": #easiest first
        for name in {**download_files}.keys() | download_folders:
            if name in upload_files:
                num_conflicts += 1
                upload.remove(f"f::{name}::{upload_files[name][0]}::{upload_files[name][1]}")
                basename = os.path.basename(name)
                filename, ext = os.path.splitext(basename)
                conflict_filename = f"{filename}_{START_TIME}{ext}"
                shutil.move(name, os.path.join(CONFLICT_DIR,conflict_filename))
                logging.warning(f"Conflict local file or folder {name} moved to {os.path.join(CONFLICT_DIR,conflict_filename)}")
            if name in upload_folders:
                num_conflicts += 1
                logging.warning(f"Conflict local folder {name} moved")
                upload.remove(f"d::{name}")
                basename = os.path.basename(name)
                conflict_filename = f"{basename}_{START_TIME}"
                shutil.move(name, os.path.join(CONFLICT_DIR,conflict_filename))
                logging.warning(f"Conflict local file or folder {name} moved to {os.path.join(CONFLICT_DIR,conflict_filename)}")

    elif RESOLVE_STRATEGY == "local":
        for name in {**upload_files}.keys() | upload_folders:
            if name in download_files:
                num_conflicts += 1
                mtime = download_files[name][0]
                size = download_files[name][1]
                download.remove(f"f::{name}::{download_files[name][0]}::{download_files[name][1]}")
                basename = os.path.basename(name)
                filename, ext = os.path.splitext(basename)
                conflict_filename = f"{filename}_{START_TIME}{ext}"
                old_remote_path = local_path_to_remote(name)
                result = run_proton_cli(['filesystem', 'move', old_remote_path, CONFLICT_DIR_REMOTE, '-j'])
                logging.debug(f"File/folder move CLI execution results: {result}")
                result = run_proton_cli(['filesystem', 'rename', os.path.join(CONFLICT_DIR_REMOTE,basename), conflict_filename, '-j'])
                logging.debug(f"File/folder rename CLI execution results: {result}")
                download.add(f"f::{os.path.join(CONFLICT_DIR,conflict_filename)}::{mtime}::{size}")
                logging.warning(f"Conflict remote file or folder {name} moved to {os.path.join(CONFLICT_DIR,conflict_filename)}")
            if name in download_folders:
                num_conflicts += 1
                download.remove(f"d::{name}")
                basename = os.path.basename(name)
                conflict_filename = f"{basename}_{START_TIME}"
                old_remote_path = local_path_to_remote(name)
                result = run_proton_cli(['filesystem', 'move', old_remote_path, CONFLICT_DIR_REMOTE, '-j'])
                logging.debug(f"File/folder upload CLI execution results: {result}")
                result = run_proton_cli(['filesystem', 'rename', os.path.join(CONFLICT_DIR_REMOTE,basename), conflict_filename, '-j'])
                logging.debug(f"File/folder rename CLI execution results: {result}")
                download.add(f"d::{os.path.join(CONFLICT_DIR,conflict_filename)}")
                logging.warning(f"Conflict remote file or folder {name} moved to {os.path.join(CONFLICT_DIR,conflict_filename)}")
    else:
        for name in upload_files:
            if name in download_files:
                if upload_files[name][0] > download_files[name][0]:
                    num_conflicts += 1
                    #local file is newer
                    mtime = download_files[name][0]
                    size = download_files[name][1]
                    download.remove(f"f::{name}::{download_files[name][0]}::{download_files[name][1]}")
                    basename = os.path.basename(name)
                    filename, ext = os.path.splitext(basename)
                    conflict_filename = f"{filename}_{START_TIME}{ext}"
                    old_remote_path = local_path_to_remote(name)
                    result = run_proton_cli(['filesystem', 'move', old_remote_path, CONFLICT_DIR_REMOTE, '-j'])
                    logging.debug(f"File/folder move CLI execution results: {result}")
                    result = run_proton_cli(['filesystem', 'rename', os.path.join(CONFLICT_DIR_REMOTE,basename), conflict_filename, '-j'])
                    logging.debug(f"File/folder rename CLI execution results: {result}")
                    download.add(f"f::{os.path.join(CONFLICT_DIR,conflict_filename)}::{mtime}::{size}")
                    logging.warning(f"Conflict remote file or folder {name} moved to {os.path.join(CONFLICT_DIR,conflict_filename)}")
                else:
                    num_conflicts += 1
                    #remote file is newer
                    upload.remove(f"f::{name}::{upload_files[name][0]}::{upload_files[name][1]}")
                    basename = os.path.basename(name)
                    filename, ext = os.path.splitext(basename)
                    conflict_filename = f"{filename}_{START_TIME}{ext}"
                    shutil.move(name, os.path.join(CONFLICT_DIR,conflict_filename))
                    logging.warning(f"Conflict local file or folder {name} moved to {os.path.join(CONFLICT_DIR,conflict_filename)}")
            if name in download_folders:
                result = run_proton_cli(['filesystem', 'info', local_path_to_remote(name), '-j'])
                item = json.loads(result.stdout)
                # Use top-level 'modificationTime' for folders
                dt = datetime.fromisoformat(item['modificationTime'].replace("Z", "+00:00"))
                remote_folder_mtime = int(dt.timestamp())
                if upload_files[name][0] > remote_folder_mtime:
                    num_conflicts += 1
                    #local file is newer
                    download.remove(f"d::{name}")
                    basename = os.path.basename(name)
                    conflict_filename = f"{basename}_{START_TIME}"
                    old_remote_path = local_path_to_remote(name)
                    result = run_proton_cli(['filesystem', 'move', old_remote_path, CONFLICT_DIR_REMOTE, '-j'])
                    logging.debug(f"File/folder upload CLI execution results: {result}")
                    result = run_proton_cli(['filesystem', 'rename', os.path.join(CONFLICT_DIR_REMOTE,basename), conflict_filename, '-j'])
                    logging.debug(f"File/folder rename CLI execution results: {result}")
                    download.add(f"d::{os.path.join(CONFLICT_DIR,conflict_filename)}")
                    logging.warning(f"Conflict remote file or folder {name} moved to {os.path.join(CONFLICT_DIR,conflict_filename)}")
                else:
                    #remote folder is newer
                    num_conflicts += 1
                    upload.remove(f"f::{name}::{upload_files[name][0]}::{upload_files[name][1]}")
                    basename = os.path.basename(name)
                    filename, ext = os.path.splitext(basename)
                    conflict_filename = f"{filename}_{START_TIME}{ext}"
                    shutil.move(name, os.path.join(CONFLICT_DIR,conflict_filename))
                    logging.warning(f"Conflict local file or folder {name} moved to {os.path.join(CONFLICT_DIR,conflict_filename)}")
        for name in upload_folders:
            if name in download_files:
                local_folder_mtime = int(os.path.getmtime(name))
                if local_folder_mtime > download_files[name][0]:
                    #local folder is newer
                    num_conflicts += 1
                    mtime = download_files[name][0]
                    size = download_files[name][1]
                    download.remove(f"f::{name}::{download_files[name][0]}::{download_files[name][1]}")
                    basename = os.path.basename(name)
                    filename, ext = os.path.splitext(basename)
                    conflict_filename = f"{filename}_{START_TIME}{ext}"
                    old_remote_path = local_path_to_remote(name)
                    result = run_proton_cli(['filesystem', 'move', old_remote_path, CONFLICT_DIR_REMOTE, '-j'])
                    logging.debug(f"File/folder move CLI execution results: {result}")
                    result = run_proton_cli(['filesystem', 'rename', os.path.join(CONFLICT_DIR_REMOTE,basename), conflict_filename, '-j'])
                    logging.debug(f"File/folder rename CLI execution results: {result}")
                    download.add(f"f::{os.path.join(CONFLICT_DIR,conflict_filename)}::{mtime}::{size}")
                    logging.warning(f"Conflict remote file or folder {name} moved to {os.path.join(CONFLICT_DIR,conflict_filename)}")
                else:
                    #remote file is newer:
                    num_conflicts += 1
                    upload.remove(f"d::{name}")
                    basename = os.path.basename(name)
                    conflict_filename = f"{basename}_{START_TIME}"
                    shutil.move(name, os.path.join(CONFLICT_DIR,conflict_filename))
                    logging.warning(f"Conflict remote file or folder {name} moved to {os.path.join(CONFLICT_DIR,conflict_filename)}")
            if name in download_folders:
                local_folder_mtime = int(os.path.getmtime(name))
                result = run_proton_cli(['filesystem', 'info', local_path_to_remote(name), '-j'])
                item = json.loads(result.stdout)
                # Use top-level 'modificationTime' for folders
                dt = datetime.fromisoformat(item['modificationTime'].replace("Z", "+00:00"))
                remote_folder_mtime = int(dt.timestamp())
                if local_folder_mtime > remote_folder_mtime:
                    num_conflicts += 1
                    #local folder is newer
                    download.remove(f"d::{name}")
                    basename = os.path.basename(name)
                    conflict_filename = f"{basename}_{START_TIME}"
                    old_remote_path = local_path_to_remote(name)
                    result = run_proton_cli(['filesystem', 'move', old_remote_path, CONFLICT_DIR_REMOTE, '-j'])
                    logging.debug(f"File/folder upload CLI execution results: {result}")
                    result = run_proton_cli(['filesystem', 'rename', os.path.join(CONFLICT_DIR_REMOTE,basename), conflict_filename, '-j'])
                    logging.debug(f"File/folder rename CLI execution results: {result}")
                    download.add(f"d::{os.path.join(CONFLICT_DIR,conflict_filename)}")
                    logging.warning(f"Conflict remote file or folder {name} moved to {os.path.join(CONFLICT_DIR,conflict_filename)}")
                else:
                    #remote folder is newer
                    num_conflicts += 1
                    upload.remove(f"d::{name}")
                    basename = os.path.basename(name)
                    filename, ext = os.path.splitext(basename)
                    conflict_filename = f"{filename}_{START_TIME}{ext}"
                    shutil.move(name, os.path.join(CONFLICT_DIR,conflict_filename))
                    logging.warning(f"Conflict local file or folder {name} moved to {os.path.join(CONFLICT_DIR,conflict_filename)}")

    if RESOLVE_STRATEGY in ("local", "newest") and num_conflicts > 0:
        #Note in the end upload (merge) conflict folder separately
        result = run_proton_cli(['filesystem', 'upload', '-f', 'replace', '-d', 'merge', CONFLICT_DIR, REMOTE_DIR, '-j', "--skip-thumbnails"])
        logging.debug(f"File/folder upload CLI execution results: {result}")
    return num_conflicts



SCRIPT_DIR = str(pathlib.Path(__file__).parent.resolve())
CONFIG_PATH = os.path.join(SCRIPT_DIR,"config.toml")
HOME_DIR = os.environ.get("HOME", os.path.expanduser("~"))

try:
    with open(str(CONFIG_PATH), "rb") as f:
        config = tomllib.load(f)
except Exception as e:
    logging.basicConfig(
        level=logging.ERROR,
        format='%(asctime)s [%(levelname)s] %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S',
        handlers=[logging.StreamHandler(sys.stdout)]
    )
    logging.error(f"Failed to load config.toml: {e}")
    sys.exit(1)

if sys.stdin.isatty():
    default_log_level = "info"
    log_handlers = [logging.StreamHandler(sys.stdout)]
else:
    default_log_level = "quiet"
    log_handlers = [logging.StreamHandler(sys.stderr)]

log_level = config.get('log_level', default_log_level).lower()

LEVEL_MAP = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "quiet": logging.ERROR,
}

if log_level not in LEVEL_MAP:
    log_level = default_log_level

target_level = LEVEL_MAP.get(log_level)

# FIX: This configuration block MUST sit here, BEFORE any logging call executes!
logging.basicConfig(
    level=target_level,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
    handlers=log_handlers
)

LOCAL_DIR = config['local_dir'].replace("${SCRIPT_DIR}", SCRIPT_DIR).replace("${HOME}", HOME_DIR)
PROTON_DRIVE_BINARY = config['proton_drive_binary'].replace("${SCRIPT_DIR}", SCRIPT_DIR).replace("${HOME}", HOME_DIR)
REMOTE_DIR = config['remote_dir']
if 'proton_drive_cache_dir' in config:
    os.environ["PROTON_DRIVE_CACHE_DIR"] = config['proton_drive_cache_dir'].replace("${SCRIPT_DIR}",SCRIPT_DIR)
if 'unsafe_login' in config and config['unsafe_login'] == True:
    os.environ['PROTON_DRIVE_CREDENTIALS_STORE'] = "unsafe_file"
STATE_FILE_NAME = ".proton_sync_state"
CONFLICT_DIR = os.path.join(LOCAL_DIR,"conflicts")
CONFLICT_DIR_REMOTE = os.path.join(REMOTE_DIR,"conflicts")
TWO_WAY = config.get('two_way', False)
if TWO_WAY and is_path_readonly(LOCAL_DIR):
    logging.error(f"    {LOCAL_DIR} mounted as read-only. Two-way sync forcibly disabled.")
    TWO_WAY = False

# Check for the explicit Docker image environment flag
if os.environ.get("AM_I_DOCKER") == "true":
    STATE_FILE = os.path.join("./.cache", STATE_FILE_NAME) # Locked to persistent container volume
else:
    STATE_FILE = os.path.join(LOCAL_DIR, STATE_FILE_NAME)  # Clean native host filesystem fallback

EMPTY_TRASH = config.get('empty_trash', False)

START_TIME = datetime.now().strftime("%Y%m%d%H%M%S") #used for conflicts

# Prepare local conflict folders for 2-way sync
if TWO_WAY:
    RESOLVE_STRATEGY_OPTIONS = {"newest", "local", "remote"}
    resolve_input = config.get('resolve_strategy', "newest").lower()
    RESOLVE_STRATEGY = resolve_input if resolve_input in RESOLVE_STRATEGY_OPTIONS else "newest"
    if not os.path.exists(CONFLICT_DIR):
        os.makedirs(CONFLICT_DIR)

#Test command to see if logged in
logging.info("Testing login status")
result = run_proton_cli(['filesystem', 'info', '/my-files', '-j'],False)
logging.debug(f"Logged in test (filesystem info /my-files) CLI execution results: {result}")
if "You need to login first" in result.stderr:
    if sys.stdin.isatty():
        subprocess.run([PROTON_DRIVE_BINARY, "auth", "login"], check=True)
    else:
        logging.error(f"Proton Drive not logged in. Run 'python3 {sys.argv[0]}' in the terminal to log in")
        sys.exit(1)
elif result.stderr.strip():
    logging.error(f"Unable to connect to Proton Drive\n {result.stderr}")
    sys.exit(1)

# Lock check and locking to prevent two devices syncing at the same time.
logging.info("Locking remote folder")
REMOTE_LOCK_FILE = os.path.join(REMOTE_DIR, ".proton_sync.lock")
local_lock_temp = os.path.join(SCRIPT_DIR, ".proton_sync.lock")

# Maximum time to wait for the other device to finish (e.g., 5 minutes)
MAX_WAIT_SECONDS = 300
POLL_INTERVAL_SECONDS = 15
waited_seconds = 0

# Add a small random jitter (0 to 5 seconds) to prevent simultaneous cron jobs 
# from locking horns repeatedly on every retry.
initial_jitter = random.uniform(0, 5)
logging.debug(f"Sync lock contention check: waiting {initial_jitter:.2f}s jitter...")
time.sleep(initial_jitter)

while waited_seconds < MAX_WAIT_SECONDS:
    # Check if remote lock exists
    result = run_proton_cli(['filesystem', 'info', REMOTE_LOCK_FILE, '-j'], fail=False)
    
    if result.stderr.strip():
        # Lock file does not exist (or parent doesn't exist/error indicating it's free)
        break
        
    logging.info(f"Remote lock file detected. Another device is syncing. Waiting... ({waited_seconds}/{MAX_WAIT_SECONDS}s)")
    time.sleep(POLL_INTERVAL_SECONDS)
    waited_seconds += POLL_INTERVAL_SECONDS
else:
    logging.error(f"Timed out after waiting {MAX_WAIT_SECONDS}s for the remote sync lock to clear. Aborting.")
    sys.exit(1)

# Acquire lock
pathlib.Path(local_lock_temp).touch()
try:
    retry = MAX_WAIT_SECONDS
    while True: #loop this until it breaks
        subresult = run_proton_cli(['filesystem', 'upload', '-f', 'skip', local_lock_temp, REMOTE_DIR, '-j'], fail=True)
        logging.debug(f"Remote lock acquisition result: {subresult}")
        if json.loads(subresult.stdout).get('skippedItems') > 0: #if the lock file was uploaded between checking and locking (should never happen)
            if retry > 0:
                logging.warning(f"Race condition. File created by other device between check and creation. Retrying for {retry} seconds.")
                retry -= POLL_INTERVAL_SECONDS
                time.sleep(POLL_INTERVAL_SECONDS)
            else:
                raise FileExistsError('Race Condition. File created by other device between check and creation.')
        else:
            break
except Exception as e:
    # If the remote directory doesn't exist yet on a first run, ignore the node not found error
    if "Node not found" in str(e):
        logging.warning("Remote target directory does not exist yet. Skipping lock upload for initial setup.")
    else:
        logging.error(f"Failed to upload remote lock file: {e}")
        pathlib.Path(local_lock_temp).unlink(missing_ok=True)
        sys.exit(1)
finally:
    pathlib.Path(local_lock_temp).unlink(missing_ok=True)

def cleanup_remote_lock():
    logging.info("Removing remote sync lock...")
    run_proton_cli(['filesystem', 'trash', REMOTE_LOCK_FILE, '-j'], fail=False)

# Register it down here after definition
atexit.register(cleanup_remote_lock)


# Run hook script before running bakup
BEFORE_RUN_SCRIPT = os.path.join(SCRIPT_DIR, "before_run.sh")

# Check if the file exists
if os.path.exists(BEFORE_RUN_SCRIPT):
    logging.info(f"Found pre-backup hook script. Executing: {BEFORE_RUN_SCRIPT}")
    try:
        # Run the script
        hook_result = subprocess.run([BEFORE_RUN_SCRIPT], capture_output=True, text=True)
        logging.debug(f"Pre-backup hook execution results: {hook_result}")
        
        # If the hook script fails (exit code not 0) halt sync to protect data!
        if hook_result.returncode != 0:
            logging.error(
                f"Pre-backup hook failed (Exit Code {hook_result.returncode}). Aborting sync execution!\n"
                f"Stderr: {hook_result.stderr}"
            )
            sys.exit(1)
            
        logging.info("Pre-backup hook completed successfully. Proceeding with sync.")
    except PermissionError:
        logging.error(
            f"Permission denied executing pre-backup hook: '{BEFORE_RUN_SCRIPT}'. "
            f"Ensure the script has executable permissions (run 'chmod +x {BEFORE_RUN_SCRIPT}'). Aborting sync!"
        )
        sys.exit(1)
    except Exception as e:
        logging.error(f"Failed to execute pre-backup hook due to an unexpected error: {e}. Aborting sync!")
        sys.exit(1)
        


FIRST_RUN = False
# Load state file
if os.path.exists(STATE_FILE):
    with open(STATE_FILE, "r", encoding="utf-8") as f:
        old_meta = {line.strip() for line in f if line.strip()}
    logging.debug(f"Loading old metadata from: {STATE_FILE}.")
else:
    FIRST_RUN = True
    logging.info("No state file found. Attempt to download remote state file")
    result = run_proton_cli(['filesystem', 'download', os.path.join(REMOTE_DIR,STATE_FILE_NAME), os.path.dirname(STATE_FILE)], False)
    if os.path.exists(STATE_FILE):
        logging.info("Remote state file found")
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            old_meta = {line.strip() for line in f if line.strip()}
    else:
        # Catch Node not found or any other download glitch safely
        error_msg = result.stderr.strip()
        if "Node not found" in error_msg:
            logging.info("ℹRemote state file does not exist yet (First run). Initializing fresh baseline.")
        else:
            logging.warning(f"Failed to download remote state file ({error_msg}). Falling back to fresh sync baseline.")
        
        old_meta = set()  # First run fallback

logging.info(f"Starting walk of {LOCAL_DIR}")
start_walk = time.perf_counter()
meta, local_filenames = local_walk()
end_walk = time.perf_counter()
duration = end_walk - start_walk
logging.debug(f"Local directory walk completed in {duration:.4f} seconds.")
logging.debug(f"Scanned {len(local_filenames)} local items into the live meta set.")

if FIRST_RUN and TWO_WAY: 
    # For two-way assume that the first run uploads and downloads every missing file.
    # Otherwise make remote folder an exact copy of the local folder, deleting files if necessary.
    logging.info("Two way run: Cleaning non-existent files from downloaded sync state file")
    old_meta = old_meta.intersection(meta)

# Preparation. Check if REMOTE_DIR exists and creating if necessary
result = run_proton_cli(['filesystem', 'info', REMOTE_DIR, '-j'],False) #this is double, but I need the output this time
logging.debug(f"REMOTE_DIR existence check CLI execution results: {result}")
if result.stderr.strip():
    logging.info(f"Creating {REMOTE_DIR}")
    baddir = result.stderr.strip().split(": ")[1]
    parts = REMOTE_DIR.strip("/").split("/")
    create = False
    path = "/"
    for d in parts:
        if d == baddir:
            create = True
        if create:
            logging.info(f"Creating new folder {d} in {path}")
            subresult = run_proton_cli(['filesystem', 'create-folder', path, d, '-j'])
            logging.debug(f"Folder creation CLI execution results: {subresult}")
        path = os.path.join(path, d)

# Calculate the changes to do on proton_drive
upload, uploaded, remote_replaced, remote_delete, remote_deleted = calculate_remote_changes(meta, local_filenames, old_meta)

# Run actions for 2 way sync
remote_meta = set()
if TWO_WAY:
    #preparation. Check if CONFLICT_DIR exists
    result = run_proton_cli(['filesystem', 'info', CONFLICT_DIR_REMOTE, '-j'],False)
    logging.debug(f"CONFLICT_DIR existence check CLI execution results: {result}")
    if result.stderr.strip():
        parent_path = os.path.dirname(CONFLICT_DIR_REMOTE)
        folder_name = os.path.basename(CONFLICT_DIR_REMOTE)
        subresult = run_proton_cli(['filesystem', 'create-folder', parent_path, folder_name, '-j'])
        logging.debug(f"Folder creation CLI execution results: {subresult}")
    logging.info(f"Starting crawl of {REMOTE_DIR}")
    start_crawl = time.perf_counter()
    remote_meta = remote_crawl()
    end_crawl = time.perf_counter()
    duration = end_crawl - start_crawl
    logging.debug(f"Remote directory crawl completed in {duration:.4f} seconds.")
    logging.debug(f"Scanned {len(remote_meta)} remote items into the remote meta set.")

    # Calculate the changes to do in the local folder 
    download, all_downloads, downloaded, local_replaced, local_delete, local_deleted = calculate_local_changes(meta, old_meta, remote_meta)
    num_conflicts = resolve_conflicts(download, upload, local_delete, remote_delete)
else:
    download, all_downloads, downloaded, local_replaced, local_delete, local_deleted = set(), set(), 0, set(), set(), 0
    num_conflicts = 0

# Delete local files
for file in local_delete:
    target = pathlib.Path(file.split("::")[1])
    if target.is_dir():
        shutil.rmtree(target)  # Deletes folder and all its contents
        logging.info(f"Deleted local dir {target}")
    elif target.is_file():
        target.unlink()         # Deletes a single file
        logging.info(f"Deleted local file {target}")

# Delete remote files
for file in remote_delete:
    path = file.split('::')[1]
    server_path = local_path_to_remote(path)
    logging.info(f"Trashing {server_path}")
    result = run_proton_cli(['filesystem', 'trash', server_path, '-j'], False)
    logging.debug(f"File/folder deletion CLI execution results: {result}")

# Uploading the new files

for file in upload:
    path = file.split('::')[1]
    server_path = local_path_to_remote(path)
    target_folder = os.path.split(server_path)[0]
    logging.info(f"Uploading {path} to {target_folder}")
    result = run_proton_cli(['filesystem', 'upload', '-f', 'replace', '-d', 'merge', path, target_folder, '-j', "--skip-thumbnails"])
    logging.debug(f"File/folder upload CLI execution results: {result}")

# Downloading new files

for file in download:
    path = file.split('::')[1]
    server_path = local_path_to_remote(path)
    target_folder = os.path.split(path)[0]
    logging.info(f"Downloading {server_path} to {target_folder}")
    result = run_proton_cli(['filesystem', 'download', '-f', 'remove', '-d', 'merge', server_path, target_folder, "-j"])
    logging.debug(f"File/folder download CLI execution results: {result}")

#Restore mtimes for downloaded files
logging.debug("Restoring modification time for downloaded files")
for file in all_downloads:
    parts = file.split('::')
    if parts[0] == 'f':
        path = parts[1]
        mtime = float(parts[2])
        local_file_path = os.path.join(LOCAL_DIR, path)
        
        if os.path.exists(local_file_path):
            try:
                os.utime(local_file_path, (mtime, mtime))
                logging.debug(f"Restored mtime for {path} to {mtime}")
            except Exception as e:
                logging.error(f"Failed to restore mtime for {path}: {e}")


if TWO_WAY:
    #rescan local after downloads
    logging.debug(f"Starting post download walk of {LOCAL_DIR}")
    start_walk = time.perf_counter()
    meta, local_filenames = local_walk()
    end_walk = time.perf_counter()
    duration = end_walk - start_walk
    logging.debug(f"Local directory walk completed in {duration:.4f} seconds.")
    logging.debug(f"Scanned {len(local_filenames)} local items into the live meta set.")

# Save and upload the state file. This also works as a keepalive
logging.info(f"Saving metadata in {STATE_FILE}")
with open(STATE_FILE, "w", encoding="utf-8") as f:
   f.write("\n".join(meta))

result = run_proton_cli(['filesystem', 'upload', '-f', 'replace', '-d', 'merge', STATE_FILE, REMOTE_DIR, '-j'])
logging.debug(f"State file upload CLI execution results: {result}")

trash_emptied = False
if EMPTY_TRASH:
    result = run_proton_cli(['filesystem', 'empty-trash'], False)
    logging.debug(f"Empty trash CLI execution results: {result}")
    
    if result.stderr.strip():
        # Log it as an error, but don't crash the script since the sync itself succeeded!
        logging.error(f"Failed to empty Proton Drive trash:\n{result.stderr}")
    else:
        # Only turn on the print text string if it succeeded cleanly
        trash_emptied = True



timestamp = time.strftime('%Y-%m-%d %H:%M:%S')
if TWO_WAY:
    print(
        f"{timestamp} [STATUS] Synced: {uploaded} uploaded, {downloaded} downloaded, {remote_replaced} replaced in remote folder, {local_replaced} replaced in local folder, {remote_deleted} deleted from remote folder, {local_deleted} deleted from folder, {num_conflicts} conflicts{', trash emptied' if trash_emptied else ''}."
    )
else:
    print(
        f"{timestamp} [STATUS] Synced: {uploaded} uploaded, {remote_replaced} replaced, {remote_deleted} deleted{', trash emptied' if trash_emptied else ''}."
    )