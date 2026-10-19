#!/usr/bin/env python3
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
from collections import namedtuple
from datetime import datetime


FileNode = namedtuple('FileNode', ['type', 'mtime', 'size'])

class ProtonCliError(Exception):
    """Raised when a Proton Drive CLI command fails with stderr output."""
    def __init__(self, cmd, stderr):
        self.cmd = cmd
        self.stderr = stderr
        super().__init__(f"Command '{' '.join(cmd)}' failed:\n{stderr}")

IS_WINDOWS = os.name == 'nt'
def fix_local_path(path):
    if IS_WINDOWS:
        return str(path).replace('/','\\')
    else:
        return str(path).replace('\\','/')
            
def fix_remote_path(path):
    return str(path).replace('\\','/')
    
    

def is_path_readonly(path: str) -> bool:
    """Check if a given directory path is read-only by attempting a temporary write test."""
    abs_path = os.path.abspath(path)
    if not os.path.exists(abs_path):
        return False
        
    test_file_path = os.path.join(abs_path, ".sync_write_test")
    try:
        with open(fix_local_path(test_file_path), "w") as f:
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
    return fix_remote_path(os.path.join(REMOTE_DIR,os.path.relpath(path, LOCAL_DIR)))

def remote_path_to_local(path):
    return fix_local_path(os.path.join(LOCAL_DIR,os.path.relpath(path, REMOTE_DIR)))

#local walk
def local_walk():
    meta = dict()
    for root, dirs, files in os.walk(LOCAL_DIR):
        for name in files:
            if name == STATE_FILE_NAME or name == ".proton_sync.lock":
                continue  # Skip tracking the state file itself!
            path = os.path.join(root,name)
            meta[fix_remote_path(path)] = FileNode(type='f', mtime = int(os.path.getmtime(path)), size=os.path.getsize(path))
        for name in dirs:
            path = os.path.join(root,name)
            if fix_remote_path(path) == fix_remote_path(CONFLICT_DIR):
                continue #skip tracking conflict dir itself
            meta[fix_remote_path(path)] = FileNode(type='d', mtime=0, size=0)
    return meta

#remote crawl
def remote_crawl(path=""):
    logging.debug(f"Crawling remote folder {fix_remote_path(os.path.join(REMOTE_DIR,path))}")
    meta = dict()
    result = run_proton_cli(['filesystem', 'list', fix_remote_path(os.path.join(REMOTE_DIR,path)), '-j'])
    logging.debug(f"Remote crawl CLI execution results: {result}")
    items = json.loads(result.stdout)
    for item in items:
        filename = item['name']['value']
        if filename == STATE_FILE_NAME or filename == ".proton_sync.lock":
            continue
        if item['type'] == "folder":
            type='d'
            mtime = 0
            size = 0
        else:
            type='f'
            dt = datetime.fromisoformat(item['activeRevision']['claimedModificationTime'].replace("Z", "+00:00"))
            mtime = int(dt.timestamp())
            size = item['activeRevision']['claimedSize']
        file_path = fix_remote_path(os.path.join(LOCAL_DIR,path,filename))
        if fix_remote_path(str(file_path)) != fix_remote_path(str(CONFLICT_DIR)):  #skip adding conflict dir because times never match
            meta[file_path] = FileNode(type=type, mtime=mtime, size=size)
        if type == 'd':
            meta.update(remote_crawl(fix_remote_path(os.path.join(path,filename))))
    return meta

def calculate_remote_changes(meta, old_meta): 
    upload_set = set(meta.items()) - set(old_meta.items()) 
    upload = dict(upload_set)
    total_uploaded = len(upload)
    for file in list(upload.keys()):
        current = file
        while True:
            parent_dir = os.path.dirname(current)
            if not parent_dir or parent_dir == current:
                break
            if parent_dir in upload:
                upload.pop(file, None)
                break
            current = parent_dir
            
    total_replaced = 0
    delete_set = set(old_meta.items()) - set(meta.items()) 
    delete = dict(delete_set)
    for file in list(delete.keys()):
        if file in meta:
            if  meta[file].type == delete[file].type:
                # Same type! It's a standard modification, not a deletion. Safe to remove.
                total_replaced += 1
                delete.pop(file)  # <--- REMOVE from delete map to prevent ghost conflicts!
            else:
                # Type mismatch! (e.g., file became directory). Keep in delete queue
                # so the engine purges the old type before uploading the new type.
                logging.info(f"Type mismatch detected for {file}. Retention forced in deletion queue.")
    total_deleted = len(delete)
    total_uploaded = total_uploaded - total_replaced
    for file in list(delete.keys()):
        current = file
        while True:
            parent_dir = os.path.dirname(current)
            if not parent_dir or parent_dir == current:
                break
            if parent_dir in delete: 
                delete.pop(file, None)
                break
            current = parent_dir
            
    logging.debug(f"Calculated change sets: {len(upload)} items to upload, {len(delete)} items to trash.")
    return upload, total_uploaded, total_replaced, delete, total_deleted


def calculate_local_changes(meta, old_meta, remote_meta): #calculate things to change in the local folder
    # 1. Perform set math on items and instantly convert to dict
    download_set = set(remote_meta.items()) - set(old_meta.items()) 
    download = dict(download_set)
    total_downloaded = len(download)
    all_downloads = download.copy() 
    for file in list(download.keys()):
        current = file
        while True:
            parent_dir = os.path.dirname(current)
            if not parent_dir or parent_dir == current:
                break
            if parent_dir in download: 
                download.pop(file, None)
                break
            current = parent_dir
    local_delete_set = set(old_meta.items()) - set(remote_meta.items()) 
    local_delete = dict(local_delete_set)
    total_replaced = 0
    for file in list(local_delete.keys()):
        if file in remote_meta: 
            if remote_meta[file].type == local_delete[file].type:
                # Same type! It's a standard modification, not a deletion. Safe to remove.
                total_replaced += 1
                local_delete.pop(file)  # <--- REMOVE from delete map to prevent ghost conflicts!
            else:
                # Type mismatch! (e.g., file became directory). Keep in delete queue
                # so the engine purges the old type before uploading the new type.
                logging.info(f"Type mismatch detected for {file}. Retention forced in deletion queue.")

    total_deleted = len(local_delete)
    total_downloaded -= total_replaced
    for file in list(local_delete.keys()):
        current = file
        while True:
            parent_dir = os.path.dirname(current)
            if not parent_dir or parent_dir == current:
                break
            if parent_dir in local_delete: 
                local_delete.pop(file, None)
                break
            current = parent_dir
    logging.debug(f"Calculated change sets: {len(download)} items to download, {len(local_delete)} items to trash.")
    return download, all_downloads, total_downloaded, total_replaced, local_delete, total_deleted


def resolve_conflicts(download, upload, local_delete, remote_delete): #note, updates the given dicts.
    num_conflicts = 0

    #upload vs deletion. Upload always wins.
    for name in list(upload.keys()):
        if name in local_delete and name not in remote_meta:
            num_conflicts += 1
            logging.info(f"Conflict deleted file or folder {name} uploaded to remote")
            local_delete.pop(name)

    #download vs deletion. download always wins.
    for name in list(download.keys()):
        if name in remote_delete and name not in meta:
            num_conflicts += 1
            logging.info(f"Conflict deleted file or folder {name} redownloaded from remote")
            remote_delete.pop(name)

    def resolve_remote(name):
        basename = os.path.basename(name)
        if upload[name].type == 'f': 
            filename, ext = os.path.splitext(basename)
            conflict_filename = f"{filename}_{START_TIME}{ext}"
        else:
            conflict_filename = f"{basename}_{START_TIME}"
        src = fix_local_path(name)
        dst = fix_local_path(os.path.join(CONFLICT_DIR, conflict_filename))
        shutil.move(src, dst)
        upload.pop(name, None)
        logging.warning(f"Conflict: Local item {src} moved to conflict folder as {conflict_filename}")

    def resolve_local(name):
        remote_node = download[name]
        download.pop(name, None)
        basename = os.path.basename(name)
        if remote_node.type == 'f': 
            filename, ext = os.path.splitext(basename)
            conflict_filename = f"{filename}_{START_TIME}{ext}"
        else:
            conflict_filename = f"{basename}_{START_TIME}"
            
        old_remote_path = local_path_to_remote(name)
        run_proton_cli(['filesystem', 'move', fix_remote_path(old_remote_path), fix_remote_path(CONFLICT_DIR_REMOTE), '-j'])
        moved_remote_target = os.path.join(CONFLICT_DIR_REMOTE, basename)
        run_proton_cli(['filesystem', 'rename', fix_remote_path(moved_remote_target), conflict_filename, '-j'])
        
        local_conflict_destination = os.path.join(CONFLICT_DIR, conflict_filename)
        download[local_conflict_destination] = remote_node
        logging.warning(f"Conflict: Remote item {fix_local_path(name)} pushed to conflict folder as {conflict_filename}")

    def get_remote_folder_mtime(name):
        remote_path = local_path_to_remote(name)
        result = run_proton_cli(['filesystem', 'info', fix_remote_path(remote_path), '-j'])
        item = json.loads(result.stdout)
        dt = datetime.fromisoformat(item['modificationTime'].replace("Z", "+00:00"))
        return int(dt.timestamp())

    def is_local_newest(name):
        """Compares local vs remote timestamps. Returns True if local wins."""
        local_node = upload[name]
        remote_node = download[name]
        if local_node.type == 'f':
            local_mtime = local_node.mtime
        else:
            local_mtime = int(os.path.getmtime(fix_local_path(name)))
            
        if remote_node.type == 'f':
            remote_mtime = remote_node.mtime
        else:
            logging.info(f"Fetching missing remote directory timestamp for: {name}")
            remote_mtime = get_remote_folder_mtime(name)
        return local_mtime > remote_mtime


    if RESOLVE_STRATEGY == "remote":
        for name in list(set(download.keys()) & set(upload.keys())):
            num_conflicts += 1
            resolve_remote(name)
            
    elif RESOLVE_STRATEGY == "local":
        for name in list(set(download.keys()) & set(upload.keys())):
            num_conflicts += 1
            resolve_local(name)
            
    else: # RESOLVE_STRATEGY == "newest"
        conflicting_paths = list(set(download.keys()) & set(upload.keys()))
        for name in conflicting_paths:
            num_conflicts += 1
            if is_local_newest(name):
                resolve_local(name)
            else:
                resolve_remote(name)

    if RESOLVE_STRATEGY in ("local", "newest") and num_conflicts > 0:
        result = run_proton_cli(['filesystem', 'upload', '-f', 'replace', '-d', 'merge', fix_local_path(CONFLICT_DIR), fix_remote_path(REMOTE_DIR), '-j', "--skip-thumbnails"])
        logging.debug(f"File/folder upload CLI execution results: {result}")
        
    return num_conflicts


def main():
    global CONFIG_PATH, CONFLICT_DIR, CONFLICT_DIR_REMOTE, EMPTY_TRASH, FIRST_RUN
    global HOME_DIR, LEVEL_MAP, LOCAL_DIR, MAX_WAIT_SECONDS, POLL_INTERVAL_SECONDS
    global PROTON_DRIVE_BINARY, REMOTE_DIR, REMOTE_LOCK_FILE, RESOLVE_STRATEGY
    global RESOLVE_STRATEGY_OPTIONS, SCRIPT_DIR, START_TIME, STATE_FILE, TWO_WAY
    global STATE_FILE_NAME


    SCRIPT_DIR = fix_local_path(str(pathlib.Path(__file__).parent.resolve()))
    CONFIG_PATH = fix_local_path(os.path.join(SCRIPT_DIR,"config.toml"))
    HOME_DIR = fix_local_path(os.environ.get("HOME", os.path.expanduser("~")))

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
        "quiet": logging.WARNING,
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

    LOCAL_DIR = fix_local_path(config['local_dir'].replace("${SCRIPT_DIR}", SCRIPT_DIR).replace("${HOME}", HOME_DIR))
    PROTON_DRIVE_BINARY = fix_local_path(config['proton_drive_binary'].replace("${SCRIPT_DIR}", SCRIPT_DIR).replace("${HOME}", HOME_DIR))
    REMOTE_DIR = fix_remote_path(config['remote_dir'])
    if 'proton_drive_cache_dir' in config:
        os.environ["PROTON_DRIVE_CACHE_DIR"] = fix_local_path(config['proton_drive_cache_dir'].replace("${SCRIPT_DIR}",SCRIPT_DIR))
    if 'unsafe_login' in config and config['unsafe_login'] == True:
        os.environ['PROTON_DRIVE_CREDENTIALS_STORE'] = "unsafe_file"
    STATE_FILE_NAME = ".proton_sync_state"
    CONFLICT_DIR = fix_local_path(os.path.join(LOCAL_DIR,"conflicts"))
    CONFLICT_DIR_REMOTE = fix_remote_path(os.path.join(REMOTE_DIR,"conflicts"))
    TWO_WAY = config.get('two_way', False)
    if TWO_WAY and is_path_readonly(LOCAL_DIR):
        logging.error(f"{LOCAL_DIR} mounted as read-only. Unable to run Two-Way sync. Exited to protect remote files.")
        sys.exit(1)

    # Check for the explicit Docker image environment flag
    if os.environ.get("AM_I_DOCKER") == "true":
        STATE_FILE = os.path.join("./.cache", STATE_FILE_NAME) # Locked to persistent container volume
    else:
        STATE_FILE = fix_local_path(os.path.join(LOCAL_DIR, STATE_FILE_NAME))  # Clean native host filesystem fallback

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
    REMOTE_LOCK_FILE = fix_remote_path(os.path.join(REMOTE_DIR, ".proton_sync.lock"))
    local_lock_temp = fix_local_path(os.path.join(SCRIPT_DIR, ".proton_sync.lock"))

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
    BEFORE_RUN_SCRIPT = fix_local_path(os.path.join(SCRIPT_DIR, "before_run.sh"))

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
    if os.path.exists(STATE_FILE) == False:
        FIRST_RUN = True
        logging.info("No state file found. Attempt to download remote state file")
        result = run_proton_cli(['filesystem', 'download', fix_remote_path(os.path.join(REMOTE_DIR, STATE_FILE_NAME)), fix_local_path(os.path.dirname(STATE_FILE))], False)

    if os.path.exists(STATE_FILE):
        logging.info(f"Loading {'remote ' if FIRST_RUN else ''}state file")
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            old_meta = dict()
            file_local_dir = ""
            override_local_dir = False
            first_line = f.readline()
            if first_line:
                first_line_parts = first_line.strip().split("::")
                if len(first_line_parts) > 1 and first_line_parts[0] == "i" and first_line_parts[1] != fix_remote_path(LOCAL_DIR):
                    file_local_dir = first_line_parts[1]
                    override_local_dir = True
                    logging.warning(f"Folder location changed from {file_local_dir} to {LOCAL_DIR}")
                elif len(first_line_parts) > 1 and first_line_parts[0] != "i":
                    # Fallback if the file didn't start with an info header block
                    path_key = first_line_parts[1]
                    if first_line_parts[0] == 'f':
                        old_meta[path_key] = FileNode(type=first_line_parts[0], mtime=int(first_line_parts[2]), size=int(first_line_parts[3]))
                    else:
                        old_meta[path_key] = FileNode(type=first_line_parts[0], mtime=0, size=0)
            for line in f:
                parts = line.strip().split("::")
                if len(parts) < 2: 
                    continue
                path_key = parts[1]
                if override_local_dir:
                    path_key = path_key.replace(file_local_dir, LOCAL_DIR)
                if parts[0] == 'f':
                    old_meta[path_key] = FileNode(type=parts[0], mtime=int(parts[2]), size=int(parts[3]))
                else:
                    old_meta[path_key] = FileNode(type=parts[0], mtime=0, size=0)
    else:
        error_msg = result.stderr.strip()
        if "Node not found" in error_msg:
            logging.info("Remote state file does not exist yet (First run). Initializing fresh baseline.")
        else:
            logging.warning(f"Failed to download remote state file ({error_msg}). Falling back to fresh sync baseline.")
        
        old_meta = dict()  # First run fallback

    logging.info(f"Starting walk of {LOCAL_DIR}")
    start_walk = time.perf_counter()
    meta = local_walk()
    end_walk = time.perf_counter()
    duration = end_walk - start_walk
    logging.debug(f"Local directory walk completed in {duration:.4f} seconds.")
    logging.debug(f"Scanned {len(meta)} local items into the live meta set.")

    if FIRST_RUN and TWO_WAY: 
        # For two-way assume that the first run uploads and downloads every missing file.
        # Otherwise make remote folder an exact copy of the local folder, deleting files if necessary.
        logging.info("Two way run: Cleaning non-existent files from downloaded sync state file")
        valid_keys = set(old_meta.keys()) & set(meta.keys())
        old_meta = {path: node for path, node in old_meta.items() if path in valid_keys}


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
                subresult = run_proton_cli(['filesystem', 'create-folder', fix_remote_path(path), d, '-j'])
                logging.debug(f"Folder creation CLI execution results: {subresult}")
            path = os.path.join(path, d)

    # Calculate the changes to do on proton_drive
    upload, uploaded, remote_replaced, remote_delete, remote_deleted = calculate_remote_changes(meta, old_meta)

    # Run actions for 2 way sync
    remote_meta = set()
    if TWO_WAY:
        #preparation. Check if CONFLICT_DIR exists
        result = run_proton_cli(['filesystem', 'info', fix_remote_path(CONFLICT_DIR_REMOTE), '-j'],False)
        logging.debug(f"CONFLICT_DIR existence check CLI execution results: {result}")
        if result.stderr.strip():
            parent_path = os.path.dirname(CONFLICT_DIR_REMOTE)
            folder_name = os.path.basename(CONFLICT_DIR_REMOTE)
            subresult = run_proton_cli(['filesystem', 'create-folder', fix_remote_path(parent_path), folder_name, '-j'])
            logging.debug(f"Folder creation CLI execution results: {subresult}")
        logging.info(f"Starting crawl of {REMOTE_DIR}")
        start_crawl = time.perf_counter()
        remote_meta = remote_crawl()
        logging.debug(f"Remote crawl results {remote_meta}")
        end_crawl = time.perf_counter()
        duration = end_crawl - start_crawl
        logging.debug(f"Remote directory crawl completed in {duration:.4f} seconds.")
        logging.debug(f"Scanned {len(remote_meta)} remote items into the remote meta set.")

        # Calculate the changes to do in the local folder 
        download, all_downloads, downloaded, local_replaced, local_delete, local_deleted = calculate_local_changes(meta, old_meta, remote_meta)
        num_conflicts = resolve_conflicts(download, upload, local_delete, remote_delete)
    else:
        download, all_downloads, downloaded, local_replaced, local_delete, local_deleted = dict(), dict(), 0, dict(), dict(), 0
        num_conflicts = 0

    # DELETE LOCAL FILES
    for file in list(local_delete.keys()):
        target = pathlib.Path(fix_local_path(file))
        if target.is_dir():
            shutil.rmtree(target)  # Deletes folder and all its contents
            logging.info(f"Deleted local dir {target}")
        elif target.is_file():
            target.unlink()         # Deletes a single file
            logging.info(f"Deleted local file {target}")
        # elif target doesn't exist do nothing (handled safely)

    # DELETE REMOTE FILES
    for file in list(remote_delete.keys()):
        server_path = local_path_to_remote(file)
        logging.info(f"Trashing {server_path}")
        # Set fail to False in case the file was deleted both locally and remotely
        result = run_proton_cli(['filesystem', 'trash', server_path, '-j'], False) 
        logging.debug(f"File/folder deletion CLI execution results: {result}")

    # UPLOADING THE NEW FILES
    for file in list(upload.keys()):
        server_path = local_path_to_remote(file)
        target_folder = os.path.split(server_path)[0]
        logging.info(f"Uploading {fix_local_path(file)} to {target_folder}")
        result = run_proton_cli(['filesystem', 'upload', '-f', 'replace', '-d', 'merge', fix_local_path(file), target_folder, '-j', "--skip-thumbnails"])
        logging.debug(f"File/folder upload CLI execution results: {result}")

    # DOWNLOADING NEW FILES
    for file in list(download.keys()):
        server_path = local_path_to_remote(file)
        target_folder = fix_local_path(os.path.split(file)[0])
        logging.info(f"Downloading {server_path} to {target_folder}")
        result = run_proton_cli(['filesystem', 'download', '-f', 'remove', '-d', 'merge', server_path, fix_local_path(target_folder), "-j"])
        logging.debug(f"File/folder download CLI execution results: {result}")


    # Restore mtimes for downloaded files
    logging.debug("Restoring modification time for downloaded files")
    for file in all_downloads.keys():
        if all_downloads[file].type == 'f':
            mtime = all_downloads[file].mtime
            local_file_path = fix_local_path(os.path.join(LOCAL_DIR, file))
            if os.path.exists(local_file_path):
                try:
                    os.utime(local_file_path, (mtime, mtime))
                    logging.debug(f"Restored mtime for {file} to {mtime}")
                except Exception as e:
                    logging.error(f"Failed to restore mtime for {file}: {e}")



    if TWO_WAY:
        #rescan local after downloads
        logging.debug(f"Starting post download walk of {LOCAL_DIR}")
        start_walk = time.perf_counter()
        meta = local_walk()
        end_walk = time.perf_counter()
        duration = end_walk - start_walk
        logging.debug(f"Local directory walk completed in {duration:.4f} seconds.")
        logging.debug(f"Scanned {len(meta)} local items into the new state file")

    # Save and upload the state file. This also works as a keepalive
    logging.info(f"Saving metadata in {STATE_FILE}")

    # Clean, single-line list comprehension replacing the loop structure completely
    # Atomic dictionary evaluation pass
    state_file_lines = [
        f"f::{fix_remote_path(path)}::{node.mtime}::{node.size}" if node.type == "f" else f"d::{fix_remote_path(path)}"
        for path, node in meta.items()
    ]

    with open(STATE_FILE, "w", encoding="utf-8") as f:
        f.write(f"i::{fix_remote_path(LOCAL_DIR)}\n")
        f.write("\n".join(state_file_lines))


    result = run_proton_cli(['filesystem', 'upload', '-f', 'replace', '-d', 'merge', fix_local_path(STATE_FILE), fix_remote_path(REMOTE_DIR), '-j'])
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
            f"{timestamp} [STATUS] Synced: {uploaded} uploaded, {downloaded} downloaded, {remote_replaced} replaced in remote folder, {local_replaced} replaced in local folder, {remote_deleted} deleted from remote folder, {local_deleted} deleted from local folder, {num_conflicts} conflicts{', trash emptied' if trash_emptied else ''}."
        )
    else:
        print(
            f"{timestamp} [STATUS] Synced: {uploaded} uploaded, {remote_replaced} replaced, {remote_deleted} deleted{', trash emptied' if trash_emptied else ''}."
        )

if __name__ == '__main__':
    main()
