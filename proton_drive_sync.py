#!/usr/bin/env python3
import os
import subprocess
import sys
import tomllib
import logging
import time
import json
import shutil
import atexit
import random
import tempfile
from pathlib import Path, PurePosixPath
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
    
    

def is_path_readonly(path):
    """Check if a given directory path is read-only by attempting a temporary write test."""
    if not path.exists():
        return False
        
    test_file_path = path / ".sync_write_test"
    try:
        with open(test_file_path, "w") as f:
            f.write("test")
        test_file_path.unlink()
        return False  # Writable
    except (PermissionError, OSError):
        return True   # Caught read-only filesystem block

def run_proton_cli(args, fail=True):
    """Helper to execute Proton Drive CLI commands, log results, and exit on error."""
    cmd = [str(PROTON_DRIVE_BINARY)] + args
    logging.debug(f"Executing CLI command: {' '.join(cmd)}")
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.stderr.strip() and fail:
        logging.error(f"Unexpected CLI error for command '{' '.join(cmd)}':\n{result.stderr}")
        raise ProtonCliError(cmd, result.stderr)
    return result 

def local_path_to_remote(path):
    relative_path = path.relative_to(LOCAL_DIR)
    return REMOTE_DIR/ relative_path.as_posix()

def remote_path_to_local(path):
    relative_path = path.relative_to(REMOTE_DIR)
    return LOCAL_DIR / relative_path.as_posix()

#local walk
def local_walk():
    meta = dict()
    for root, dirs, files in LOCAL_DIR.walk():
        for name in files:
            if name == STATE_FILE_NAME or name == ".proton_sync.lock":
                continue  # Skip tracking the state file itself!
            path = root / name
            meta[path] = FileNode(type='f', mtime=int(path.stat().st_mtime), size=path.stat().st_size)
        for name in dirs:
            path = root / name
            if path == CONFLICT_DIR:
                continue  # skip tracking conflict dir itself
            meta[path] = FileNode(type='d', mtime=0, size=0)
    return meta

#remote crawl
def remote_crawl(current_remote_path: PurePosixPath = None):
    if current_remote_path is None:
        current_remote_path = REMOTE_DIR
    logging.debug(f"Crawling remote folder {current_remote_path}")
    meta = dict()
    result = run_proton_cli(['filesystem', 'list', str(current_remote_path), '-j'])
    logging.debug(f"Remote crawl CLI execution results: {result}")
    items = json.loads(result.stdout)
    for item in items:
        filename = item['name']['value']
        if filename == STATE_FILE_NAME or filename == ".proton_sync.lock":
            continue
        item_remote_path = current_remote_path / filename
        if item['type'] == "folder":
            item_type = 'd'
            mtime = 0
            size = 0
        else:
            item_type = 'f'
            dt = datetime.fromisoformat(item['activeRevision']['claimedModificationTime'].replace("Z", "+00:00"))
            mtime = int(dt.timestamp())
            size = item['activeRevision']['claimedSize']
        
        # Map remote path to local path for the dictionary key
        local_file_path = remote_path_to_local(item_remote_path)
        if local_file_path != CONFLICT_DIR:
            meta[local_file_path] = FileNode(type=item_type, mtime=mtime, size=size)
            
        if item_type == 'd':
            meta.update(remote_crawl(item_remote_path))
    return meta

def calculate_remote_changes(meta, old_meta): 
    upload_set = set(meta.items()) - set(old_meta.items()) 
    upload = dict(upload_set)
    total_uploaded = len(upload)
    
    # If the parent folder is already uploading, we don't need a separate entry for child files
    for file in list(upload.keys()):
        for parent_dir in file.parents:
            if parent_dir in upload:
                upload.pop(file, None)
                break
            
    total_replaced = 0
    delete_set = set(old_meta.items()) - set(meta.items()) 
    delete = dict(delete_set)
    for file in list(delete.keys()):
        if file in meta:
            if meta[file].type == delete[file].type:
                # Same type! It's a standard modification, not a deletion. Safe to remove.
                total_replaced += 1
                delete.pop(file)  # REMOVE from delete map to prevent ghost conflicts!
            else:
                # Type mismatch! (e.g., file became directory). Keep in delete queue
                # so the engine purges the old type before uploading the new type.
                logging.info(f"Type mismatch detected for {file}. Retention forced in deletion queue.")
                
    total_deleted = len(delete)
    total_uploaded = total_uploaded - total_replaced
    
    # If the parent folder is already deleting, we don't need a separate entry for child items
    for file in list(delete.keys()):
        for parent_dir in file.parents:
            if parent_dir in delete: 
                delete.pop(file, None)
                break
            
    logging.debug(f"Calculated change sets: {len(upload)} items to upload, {len(delete)} items to trash.")
    return upload, total_uploaded, total_replaced, delete, total_deleted


def calculate_local_changes(meta, old_meta, remote_meta): #calculate things to change in the local folder
    # 1. Perform set math on items and instantly convert to dict
    download_set = set(remote_meta.items()) - set(old_meta.items()) 
    download = dict(download_set)
    total_downloaded = len(download)
    all_downloads = download.copy() 
    for file in list(download.keys()):
        for parent_dir in file.parents:
            if parent_dir in download:
                download.pop(file, None)
                break
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
        for parent_dir in file.parents:
            if parent_dir in local_delete:
                local_delete.pop(file, None)
                break
    logging.debug(f"Calculated change sets: {len(download)} items to download, {len(local_delete)} items to trash.")
    return download, all_downloads, total_downloaded, total_replaced, local_delete, total_deleted


def resolve_conflicts(download, upload, local_delete, remote_delete): #note, updates the given dicts.
    num_conflicts = 0
    new_uploads = dict()
    #upload vs deletion. Upload always wins.
    for name in list(upload.keys()):
        if name in local_delete and name not in download:
            num_conflicts += 1
            logging.info(f"Conflict deleted file or folder {name} uploaded to remote")
            local_delete.pop(name)

    #download vs deletion. download always wins.
    for name in list(download.keys()):
        if name in remote_delete and name not in upload:
            num_conflicts += 1
            logging.info(f"Conflict deleted file or folder {name} redownloaded from remote")
            remote_delete.pop(name)

    def resolve_remote(name):
        old_node = upload[name]
        if upload[name].type == 'f': 
            filename, ext = name.stem, name.suffix
            conflict_filename = f"{filename}_{START_TIME}{ext}"
        else:
            conflict_filename = f"{name.name}_{START_TIME}"
        dst = CONFLICT_DIR / conflict_filename
        shutil.move(name, dst)
        upload.pop(name, None)
        if name in local_delete:
            local_delete.pop(name)
        new_uploads[dst] = old_node
        logging.warning(f"Conflict: Local item {name} moved to conflict folder as {conflict_filename}")

    def resolve_local(name):
        remote_node = download[name]
        download.pop(name, None)
        if remote_node.type == 'f': 
            filename, ext = name.stem, name.suffix
            conflict_filename = f"{filename}_{START_TIME}{ext}"
        else:
            conflict_filename = f"{name.name}_{START_TIME}"
        if name in remote_delete:
            remote_delete.pop(name)
            
        old_remote_path = local_path_to_remote(name)
        run_proton_cli(['filesystem', 'move', str(old_remote_path), str(CONFLICT_DIR_REMOTE), '-j'])
        moved_remote_target = CONFLICT_DIR_REMOTE / name.name
        run_proton_cli(['filesystem', 'rename', str(moved_remote_target), conflict_filename, '-j'])
        
        local_conflict_destination = CONFLICT_DIR / conflict_filename
        download[local_conflict_destination] = remote_node
        logging.warning(f"Conflict: Remote item {name} pushed to conflict folder as {conflict_filename}")

    def get_remote_folder_mtime(name):
        remote_path = local_path_to_remote(name)
        result = run_proton_cli(['filesystem', 'info', str(remote_path), '-j'])
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
            local_mtime = int(name.stat().st_mtime)
            
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
        
    return num_conflicts, new_uploads


def main():
    global CONFIG_PATH, CONFLICT_DIR, CONFLICT_DIR_REMOTE, EMPTY_TRASH, FIRST_RUN
    global HOME_DIR, LEVEL_MAP, LOCAL_DIR, MAX_WAIT_SECONDS, POLL_INTERVAL_SECONDS
    global PROTON_DRIVE_BINARY, REMOTE_DIR, REMOTE_LOCK_FILE, RESOLVE_STRATEGY
    global RESOLVE_STRATEGY_OPTIONS, SCRIPT_DIR, START_TIME, STATE_FILE, TWO_WAY
    global STATE_FILE_NAME


    SCRIPT_DIR = Path(__file__).parent.resolve()
    CONFIG_PATH = SCRIPT_DIR / "config.toml"
    HOME_DIR = Path.home()

    try:
        with CONFIG_PATH.open("rb") as f:
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

    LOCAL_DIR = Path(
        config['local_dir']
        .replace("${SCRIPT_DIR}", str(SCRIPT_DIR))
        .replace("${HOME}", str(HOME_DIR))
    )
    PROTON_DRIVE_BINARY = Path(
        config['proton_drive_binary']
        .replace("${SCRIPT_DIR}", str(SCRIPT_DIR))
        .replace("${HOME}", str(HOME_DIR))
    )
    REMOTE_DIR = PurePosixPath(config['remote_dir'])
    
    if 'proton_drive_cache_dir' in config:
        cache_dir = config['proton_drive_cache_dir'].replace("${SCRIPT_DIR}", str(SCRIPT_DIR))
        os.environ["PROTON_DRIVE_CACHE_DIR"] = str(Path(cache_dir))
    if 'unsafe_login' in config and config['unsafe_login'] == True:
        os.environ['PROTON_DRIVE_CREDENTIALS_STORE'] = "unsafe_file"
    STATE_FILE_NAME = ".proton_sync_state"
    CONFLICT_DIR = LOCAL_DIR / "conflicts"
    CONFLICT_DIR_REMOTE = REMOTE_DIR / "conflicts"
    TWO_WAY = config.get('two_way', False)
    if TWO_WAY and is_path_readonly(LOCAL_DIR):
        logging.error(f"{LOCAL_DIR} mounted as read-only. Unable to run Two-Way sync. Exited to protect remote files.")
        sys.exit(1)

    # Check for the explicit Docker image environment flag
    if os.environ.get("AM_I_DOCKER") == "true":
        STATE_FILE = SCRIPT_DIR / ".cache" /  STATE_FILE_NAME # Locked to persistent container volume
    else:
        STATE_FILE = LOCAL_DIR / STATE_FILE_NAME  # Clean native host filesystem fallback

    EMPTY_TRASH = config.get('empty_trash', False)

    START_TIME = datetime.now().strftime("%Y%m%d%H%M%S") #used for conflicts

    # Prepare local conflict folders for 2-way sync
    if TWO_WAY:
        RESOLVE_STRATEGY_OPTIONS = {"newest", "local", "remote"}
        resolve_input = config.get('resolve_strategy', "newest").lower()
        RESOLVE_STRATEGY = resolve_input if resolve_input in RESOLVE_STRATEGY_OPTIONS else "newest"
        CONFLICT_DIR.mkdir(parents=True, exist_ok=True)

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
    REMOTE_LOCK_FILE = REMOTE_DIR / ".proton_sync.lock"
    local_lock_temp = SCRIPT_DIR / ".proton_sync.lock"

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
        result = run_proton_cli(['filesystem', 'info', str(REMOTE_LOCK_FILE), '-j'], fail=False)
        
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
    local_lock_temp.touch()
    try:
        retry = MAX_WAIT_SECONDS
        while True: #loop this until it breaks
            subresult = run_proton_cli(['filesystem', 'upload', '-f', 'skip', str(local_lock_temp), str(REMOTE_DIR), '-j'], fail=True)
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
            Path(local_lock_temp).unlink(missing_ok=True)
            sys.exit(1)
    finally:
        Path(local_lock_temp).unlink(missing_ok=True)

    def cleanup_remote_lock():
        logging.info("Removing remote sync lock...")
        run_proton_cli(['filesystem', 'trash', str(REMOTE_LOCK_FILE), '-j'], fail=False)

    # Register it down here after definition
    atexit.register(cleanup_remote_lock)


    # Run hook script before running bakup
    BEFORE_RUN_SCRIPT = SCRIPT_DIR / "before_run.sh"

    # Check if the file exists
    if BEFORE_RUN_SCRIPT.exists():
        logging.info(f"Found pre-backup hook script. Executing: {BEFORE_RUN_SCRIPT}")
        try:
            # Run the script
            hook_result = subprocess.run([str(BEFORE_RUN_SCRIPT)], capture_output=True, text=True)
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
    if not STATE_FILE.exists():
        FIRST_RUN = True
        logging.info("No state file found. Attempt to download remote state file")
        result = run_proton_cli(['filesystem', 'download', str(REMOTE_DIR / STATE_FILE_NAME), str(STATE_FILE.parent)], False)

    if STATE_FILE.exists():
        logging.info(f"Loading {'remote ' if FIRST_RUN else ''}state file")
        with STATE_FILE.open("r", encoding="utf-8") as f:
            old_meta = dict()
            file_local_dir = ""
            override_local_dir = False
            first_line = f.readline()
            if first_line:
                first_line_parts = first_line.strip().split("::")
                if len(first_line_parts) > 1 and first_line_parts[0] == "i" and first_line_parts[1] != str(LOCAL_DIR):
                    file_local_dir = first_line_parts[1]
                    override_local_dir = True
                    logging.warning(f"Folder location changed from {file_local_dir} to {LOCAL_DIR}")
                elif len(first_line_parts) > 1 and first_line_parts[0] != "i":
                    # Fallback if the file didn't start with an info header block
                    path_key = first_line_parts[1]
                    if first_line_parts[0] == 'f':
                        old_meta[Path(path_key)] = FileNode(type=first_line_parts[0], mtime=int(first_line_parts[2]), size=int(first_line_parts[3]))
                    else:
                        old_meta[Path(path_key)] = FileNode(type=first_line_parts[0], mtime=0, size=0)
            for line in f:
                parts = line.strip().split("::")
                if len(parts) < 2: 
                    continue
                path_key = parts[1]
                if override_local_dir:
                    path_key = path_key.replace(file_local_dir, str(LOCAL_DIR))
                if parts[0] == 'f':
                    old_meta[Path(path_key)] = FileNode(type=parts[0], mtime=int(parts[2]), size=int(parts[3]))
                else:
                    old_meta[Path(path_key)] = FileNode(type=parts[0], mtime=0, size=0)
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
        if old_meta:
            logging.info("Two way run: Cleaning non-existent files from downloaded sync state file")
        valid_keys = set(old_meta.keys()) & set(meta.keys())
        old_meta = {path: node for path, node in old_meta.items() if path in valid_keys}


    # Preparation. Check if REMOTE_DIR exists and creating if necessary
    result = run_proton_cli(['filesystem', 'info', str(REMOTE_DIR), '-j'],False) #this is double, but I need the output this time
    logging.debug(f"REMOTE_DIR existence check CLI execution results: {result}")
    if result.stderr.strip():
        logging.info(f"Creating {REMOTE_DIR}")
        baddir = result.stderr.strip().split(": ")[1]
        parts = REMOTE_DIR.parts
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
    upload, uploaded, remote_replaced, remote_delete, remote_deleted = calculate_remote_changes(meta, old_meta)

    # Run actions for 2 way sync
    remote_meta = set()
    if TWO_WAY:
        #preparation. Check if CONFLICT_DIR exists
        result = run_proton_cli(['filesystem', 'info', str(CONFLICT_DIR_REMOTE), '-j'],False)
        logging.debug(f"CONFLICT_DIR existence check CLI execution results: {result}")
        if result.stderr.strip():
            parent_path = CONFLICT_DIR_REMOTE.parent
            folder_name = CONFLICT_DIR_REMOTE.name
            subresult = run_proton_cli(['filesystem', 'create-folder', str(parent_path), folder_name, '-j'])
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
        num_conflicts, new_uploads = resolve_conflicts(download, upload, local_delete, remote_delete)
        upload |= new_uploads
    else:
        download, all_downloads, downloaded, local_replaced, local_delete, local_deleted = dict(), dict(), 0, dict(), dict(), 0
        num_conflicts = 0

    # DELETE LOCAL FILES
    for target in list(local_delete.keys()):
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
        result = run_proton_cli(['filesystem', 'trash', str(server_path), '-j'], False) 
        logging.debug(f"File/folder deletion CLI execution results: {result}")

    # UPLOADING THE NEW FILES
    for file in list(upload.keys()):
        server_path = local_path_to_remote(file)
        target_folder = server_path.parent
        logging.info(f"Uploading {file} to {target_folder}")
        result = run_proton_cli(['filesystem', 'upload', '-f', 'replace', '-d', 'merge', str(file), str(target_folder), '-j', "--skip-thumbnails"])
        logging.debug(f"File/folder upload CLI execution results: {result}")

    for file in list(download.keys()):
        server_path = local_path_to_remote(file)
        # If the file's parent is the root REMOTE_DIR, target is LOCAL_DIR. Otherwise, map it.
        if file.parent == REMOTE_DIR:
            target_folder = LOCAL_DIR
        else:
            target_folder = file.parent
        logging.info(f"Downloading {server_path} to {target_folder}")
        result = run_proton_cli(['filesystem', 'download', '-f', 'remove', '-d', 'merge', str(server_path), str(target_folder), "-j"])
        logging.debug(f"File/folder download CLI execution results: {result}")

    # Restore mtimes for downloaded files
    logging.debug("Restoring modification time for downloaded files")
    for file in all_downloads.keys():
        if all_downloads[file].type == 'f':
            mtime = all_downloads[file].mtime
            local_file_path = LOCAL_DIR / file
            if local_file_path.exists():
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
        f"f::{path}::{node.mtime}::{node.size}" if node.type == "f" else f"d::{path}"
        for path, node in meta.items()
    ]

    with STATE_FILE.open("w", encoding="utf-8") as f:
        f.write(f"i::{LOCAL_DIR}\n")
        f.write("\n".join(state_file_lines))


    result = run_proton_cli(['filesystem', 'upload', '-f', 'replace', '-d', 'merge', str(STATE_FILE), str(REMOTE_DIR), '-j'])
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
