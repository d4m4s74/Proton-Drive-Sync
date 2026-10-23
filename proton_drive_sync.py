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
class ProtonConnectionError(Exception):
    """Raised when the Proton CLI encounters an unexpected connection or system error."""
    pass

def local_path_to_remote(path):
    relative_path = path.relative_to(LOCAL_DIR)
    return REMOTE_DIR/ relative_path.as_posix()

def remote_path_to_local(path):
    relative_path = path.relative_to(REMOTE_DIR)
    return LOCAL_DIR / relative_path.as_posix()
    

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
        raise ProtonCliError(cmd, result.stderr)
    return result 

def proton_check_logged_in(cli_runner=run_proton_cli):
    result = run_proton_cli(['filesystem', 'info', '/my-files', '-j'],False)
    logging.debug(f"Logged in test (filesystem info /my-files) CLI execution results: {result}")
    if "You need to login first" in result.stderr:
        return False
    elif result.stderr.strip():
        raise ProtonConnectionError(result.stderr.strip())
    else:
        return True

def proton_upload(src, dest, file_action='replace', dir_action='merge', skip_thumbnails=True, fail=False):
    cmd = ['filesystem', 'upload', '-f', file_action, '-d', dir_action, str(src), str(dest), '-j']
    if skip_thumbnails:
        cmd.append("--skip-thumbnails")
    result = run_proton_cli(cmd, fail)
    logging.debug(f"File/folder upload CLI execution results: {result}")
    if result.stderr.strip():
        return False, result.stderr.strip()
    else:
        try:
            data = json.loads(result.stdout)
            return True, data
        except json.JSONDecodeError:
            return False, result.stdout.strip()

def proton_download(src, dest, file_action='remove', dir_action='merge', fail=False):
    cmd = ['filesystem', 'download', '-f', file_action, '-d', dir_action, str(src), str(dest), '-j']
        
    result = run_proton_cli(cmd, fail)
    logging.debug(f"Download CLI execution results: {result}")
    
    # Check if stderr captured a failure message (like "Node not found" or "not supported")
    if result.stderr.strip():
        return False, result.stderr.strip()
    else:
        try:
            data = json.loads(result.stdout)
            # Optional extra check: sometimes CLI exits 0 with JSON but logs failures inside the JSON
            if data.get('failedItems', 0) > 0:
                return False, data.get('failures', 'Unknown download failure')
            return True, data
        except json.JSONDecodeError:
            return False, result.stdout.strip()

def proton_trash(path, fail=False):
    cmd = ['filesystem', 'trash', str(path), '-j']
    
    result = run_proton_cli(cmd, fail)
    logging.debug(f"Trash CLI execution results: {result}")
    
    if result.stderr.strip():
        if fail:
            raise ProtonCliError(cmd, result.stderr.strip())
        return False
        
    return True

def proton_info(path): #Returns dictionary of file info
    cmd = ['filesystem', 'info', str(path), '-j']
    result = run_proton_cli(cmd, False)
    logging.debug(f"Proton Info CLI execution results: {result}")
    if not result.stderr.strip():
        return json.loads(result.stdout)
    else:
        raise ProtonCliError(cmd, result.stderr.strip())

def proton_file_exists(path): #Returns success boolean and first node not found
    try:
        proton_info(path)
        return True, ""
    except ProtonCliError as e:
        error = str(e)
        if "Node not found" in error:
            node = error.strip().split("Node not found: ")[1]
            return False, node
        else:
            raise


def proton_list(path):
    result = run_proton_cli(['filesystem', 'list', str(path), '-j'])
    logging.debug(f"Proton List execution results: {result}")
    items = json.loads(result.stdout)
    return items

def proton_move_rename(src,dest):
    if src.parent != dest.parent:
        run_proton_cli(['filesystem', 'move', str(src), str(dest.parent), '-j'])
    if src.name != dest.name:
        moved_remote_target = dest.parent / src.name
        run_proton_cli(['filesystem', 'rename', str(moved_remote_target), dest.name, '-j'])

def proton_create_dir(path, name, fail=False):
    try:
        result = run_proton_cli(['filesystem', 'create-folder', str(path), str(name), '-j'])
        logging.debug(f"Folder creation CLI execution results: {result}")
        return True
    except ProtonCliError:
        if fail:
            raise
        return False

def proton_create_remote_path(remote_path):
    dir_exists, baddir = proton_file_exists(remote_path) #this is double, but I need the output this time
    if not dir_exists:
        logging.info(f"Creating {remote_path}")
        parts = remote_path.parts
        create = False
        path = "/"
        for d in parts:
            if d == baddir:
                create = True
            if create:
                logging.info(f"Creating new folder {d} in {path}")
                proton_create_dir(path, d, True)
            path = os.path.join(path, d)

def proton_empty_trash():
    result = run_proton_cli(['filesystem', 'empty-trash'])
    logging.debug(f"Empty trash CLI execution results: {result}")
    
def load_state_file(path, remote=False, fail=False):
    if path.exists():
        try:
            with path.open("r", encoding="utf-8") as f:
                meta = dict()
                file_local_dir = ""
                override_local_dir = False
                first_line = f.readline()
                if first_line:
                    first_line_parts = first_line.strip().split("::")
                    if len(first_line_parts) > 1 and first_line_parts[0] == "i" and first_line_parts[1] != str(LOCAL_DIR):
                        file_local_dir = first_line_parts[1]
                        override_local_dir = True
                        if not remote:
                            logging.warning(f"Folder location changed from {file_local_dir} to {LOCAL_DIR}")
                    elif len(first_line_parts) > 1 and first_line_parts[0] != "i":
                        # Fallback if the file didn't start with an info header block
                        path_key = first_line_parts[1]
                        if first_line_parts[0] == 'f':
                            meta[Path(path_key)] = FileNode(type=first_line_parts[0], mtime=int(first_line_parts[2]), size=int(first_line_parts[3]))
                        else:
                            meta[Path(path_key)] = FileNode(type=first_line_parts[0], mtime=0, size=0)
                for line in f:
                    parts = line.strip().split("::")
                    if len(parts) < 2: 
                        continue
                    path_key = parts[1]
                    if override_local_dir:
                        path_key = path_key.replace(file_local_dir, str(LOCAL_DIR))
                    if parts[0] == 'f':
                        meta[Path(path_key)] = FileNode(type=parts[0], mtime=int(parts[2]), size=int(parts[3]))
                    else:
                        meta[Path(path_key)] = FileNode(type=parts[0], mtime=0, size=0)
            return meta
        except Exception as e:
            if not fail:
                return None
            else:
                raise
    else:
        return None

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
            if path == CONFLICT_DIR or path == WATCH_DIR:
                continue  # skip tracking conflict dir or watch dir itself
            meta[path] = FileNode(type='d', mtime=0, size=0)
    return meta

#remote crawl
def remote_crawl(current_remote_path: PurePosixPath = None):
    if current_remote_path is None:
        current_remote_path = REMOTE_DIR
    logging.debug(f"Crawling remote folder {current_remote_path}")
    meta = dict()
    items = proton_list(current_remote_path)
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
                # Type mismatch. Keep in delete queue
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
    false_conflicts = 0
    #upload vs deletion. Upload always wins.
    for name in list(upload.keys()):
        if name in local_delete and name not in download:
            num_conflicts += 1
            logging.info(f"Conflict deleted file or folder {name} reuploaded to remote")
            local_delete.pop(name)

    #download vs deletion. download always wins.
    for name in list(download.keys()):
        if name in remote_delete and name not in upload:
            num_conflicts += 1
            logging.info(f"Conflict deleted file or folder {name} redownloaded from remote")
            remote_delete.pop(name)

    def resolve_remote(name):
        nonlocal num_conflicts, false_conflicts
        if upload[name].type == 'f' and upload[name] == download[name]: #Unfortunately I am not able to detect if a directory is a false conflict
            logging.info(f"No conflict: local file {name} equal to remote")
            false_conflicts += 1
            download.pop(name)
            upload.pop(name)
            return
        num_conflicts += 1
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
        upload[dst] = old_node
        logging.warning(f"Conflict: Local item {name} moved to conflict folder as {conflict_filename}")

    def resolve_local(name):
        nonlocal num_conflicts, false_conflicts
        if upload[name].type == 'f' and upload[name] == download[name]: #Unfortunately I am not able to detect if a directory is a false conflict
            logging.info(f"No conflict: local item {name} equal to remote")
            false_conflicts += 1
            download.pop(name)
            upload.pop(name)
            return
        num_conflicts += 1
        remote_node = download[name]
        download.pop(name, None)
        if remote_node.type == 'f': 
            filename, ext = name.stem, name.suffix
            conflict_filename = f"{filename}_{START_TIME}{ext}"
        else:
            conflict_filename = f"{name.name}_{START_TIME}"
        if name in remote_delete:
            remote_delete.pop(name)
            
        proton_move_rename(local_path_to_remote(name),CONFLICT_DIR_REMOTE/conflict_filename)
        
        local_conflict_destination = CONFLICT_DIR / conflict_filename
        download[local_conflict_destination] = remote_node
        logging.warning(f"Conflict: Remote item {name} pushed to conflict folder as {conflict_filename}")

    def get_remote_folder_mtime(name):
        remote_path = local_path_to_remote(name)
        item = proton_info(remote_path)
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
            resolve_remote(name)
            
    elif RESOLVE_STRATEGY == "local":
        for name in list(set(download.keys()) & set(upload.keys())):
            resolve_local(name)
            
    else: # RESOLVE_STRATEGY == "newest"
        conflicting_paths = list(set(download.keys()) & set(upload.keys()))
        for name in conflicting_paths:
            if is_local_newest(name):
                resolve_local(name)
            else:
                resolve_remote(name)
        
    return num_conflicts, false_conflicts


def main():
    global CONFIG_PATH, CONFLICT_DIR, CONFLICT_DIR_REMOTE, EMPTY_TRASH, FIRST_RUN
    global HOME_DIR, LEVEL_MAP, LOCAL_DIR, MAX_WAIT_SECONDS, POLL_INTERVAL_SECONDS
    global PROTON_DRIVE_BINARY, REMOTE_DIR, REMOTE_LOCK_FILE, RESOLVE_STRATEGY
    global RESOLVE_STRATEGY_OPTIONS, SCRIPT_DIR, START_TIME, STATE_FILE, TWO_WAY
    global STATE_FILE_NAME, TRUST_REMOTE_STATE_FILE, WATCH_DIR


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
        IS_CRON = False
    else:
        default_log_level = "quiet"
        log_handlers = [logging.StreamHandler(sys.stderr)]
        IS_CRON = True

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
    if not isinstance(TWO_WAY, bool):
        TWO_WAY = False
    if TWO_WAY and is_path_readonly(LOCAL_DIR):
        logging.error(f"{LOCAL_DIR} mounted as read-only. Unable to run Two-Way sync. Exited to protect remote files.")
        sys.exit(1)
    TRUST_REMOTE_STATE_FILE = config.get('trust_remote_state_file', False)
    if not isinstance(TRUST_REMOTE_STATE_FILE, bool):
        TRUST_REMOTE_STATE_FILE = True

    WATCH_DIR = None
    if 'watch_dir' in config and config['watch_dir'] != "":
        path = PurePosixPath(config['watch_dir'].replace("${REMOTE_DIR}", str(REMOTE_DIR)))
        if path.is_relative_to(REMOTE_DIR):
            WATCH_DIR = path

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
    if not proton_check_logged_in():
        if sys.stdin.isatty():
            subprocess.run([PROTON_DRIVE_BINARY, "auth", "login"], check=True)
        else:
            logging.error(f"Proton Drive not logged in. Run 'python3 {sys.argv[0]}' in the terminal to log in")
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
    if TWO_WAY and IS_CRON:
        initial_jitter = random.uniform(0, 5)
        logging.debug(f"Sync lock contention check: waiting {initial_jitter:.2f}s jitter...")
        time.sleep(initial_jitter)

    while waited_seconds < MAX_WAIT_SECONDS:
        # Check if remote lock exists
        file_exists, _ = proton_file_exists(REMOTE_LOCK_FILE)
        if not file_exists:
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
            success, output = proton_upload(local_lock_temp, REMOTE_DIR, file_action="skip", fail=True)
            if not success or output.get('skippedItems') > 0: #if the lock file was uploaded between checking and locking (should never happen)
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
        proton_trash(REMOTE_LOCK_FILE)

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
            


    # Load state file
    logging.info(f"Loading old state from {STATE_FILE}")
    old_meta = load_state_file(STATE_FILE)
    if old_meta:
        FIRST_RUN = False
    else:
        FIRST_RUN = True
        logging.info(f"State file empty or not found, attempting to load remote state file")
        file_exists, _ = proton_file_exists(REMOTE_DIR / STATE_FILE_NAME)
        if file_exists:
            proton_download(REMOTE_DIR / STATE_FILE_NAME, LOCAL_DIR)
            old_meta = load_state_file(STATE_FILE)
    if not old_meta:
        logging.info("Unable to load state file. Initializing fresh baseline")
        old_meta = dict()

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
    proton_create_remote_path(REMOTE_DIR)

    # Calculate the changes to do on proton_drive
    upload, uploaded, remote_replaced, remote_delete, remote_deleted = calculate_remote_changes(meta, old_meta)

    # Run actions for 2 way sync
    remote_meta = dict()
    if TWO_WAY:
        proton_create_remote_path(CONFLICT_DIR_REMOTE) #Create conflict dir if necessary
        if WATCH_DIR: #Create watch dir if necessary
            proton_create_remote_path(WATCH_DIR)
        if TRUST_REMOTE_STATE_FILE:
            logging.info("Attempting to load remote state file")
            with tempfile.TemporaryDirectory() as tmpdir_str:
            # Convert the string path to a Path object
                tmpdir = Path(tmpdir_str)
                temp_state_path = tmpdir / STATE_FILE_NAME
                
                # 2. Download your remote file to the temp path
                proton_download(REMOTE_DIR / STATE_FILE_NAME, tmpdir)
                if temp_state_path.exists():
                    remote_meta = load_state_file(temp_state_path, remote=True)
                    if remote_meta:
                        logging.debug(f"Loaded {len(remote_meta)} items from remote state file.")
                else:
                    logging.info("No remote state file exists. Crawling remote directory.")
        if not remote_meta:
            logging.info(f"Starting crawl of {REMOTE_DIR}")
            start_crawl = time.perf_counter()
            remote_meta = remote_crawl()
            logging.info(f"Finished crawl of {REMOTE_DIR}")
            logging.debug(f"Remote crawl results {remote_meta}")
            end_crawl = time.perf_counter()
            duration = end_crawl - start_crawl
            logging.debug(f"Remote directory crawl completed in {duration:.4f} seconds.")
            logging.debug(f"Scanned {len(remote_meta)} remote items into the remote meta set.")
        elif WATCH_DIR:
            logging.info(f"Starting crawl of {WATCH_DIR}")
            start_crawl = time.perf_counter()
            watch_meta = remote_crawl(WATCH_DIR)
            logging.info(f"Finished crawl of {WATCH_DIR}")
            logging.debug(f"Remote crawl results {watch_meta}")
            end_crawl = time.perf_counter()
            duration = end_crawl - start_crawl
            logging.debug(f"Watch directory crawl completed in {duration:.4f} seconds.")
            logging.debug(f"Scanned {len(watch_meta)} more items into the remote meta set.")
            remote_meta.update(watch_meta)
            # Remove files deleted from the watch dir after last sync from remote meta
            for path in list(remote_meta.keys()):
                if local_path_to_remote(path).is_relative_to(WATCH_DIR) and path not in watch_meta:
                    del remote_meta[path]
            

        # Calculate the changes to do in the local folder 
        download, all_downloads, downloaded, local_replaced, local_delete, local_deleted = calculate_local_changes(meta, old_meta, remote_meta)
        num_conflicts, false_conflicts = resolve_conflicts(download, upload, local_delete, remote_delete)
        uploaded -= false_conflicts
        downloaded -= false_conflicts
        
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
        if not proton_trash(server_path):
            logging.info(f"Trashing {server_path} failed, file moved or already trashed")

    # UPLOADING THE NEW FILES
    for file in list(upload.keys()):
        server_path = local_path_to_remote(file)
        target_folder = server_path.parent
        logging.info(f"Uploading {file} to {target_folder}")
        proton_upload(file, target_folder, fail=True)

    for file in list(download.keys()):
        server_path = local_path_to_remote(file)
        # If the file's parent is the root REMOTE_DIR, target is LOCAL_DIR. Otherwise, map it.
        target_folder = file.parent
        logging.info(f"Downloading {server_path} to {target_folder}")
        result = proton_download(server_path, target_folder, fail=True)

    # Restore mtimes for downloaded files
    logging.debug("Restoring modification time for downloaded files")
    for file in all_downloads.keys():
        if all_downloads[file].type == 'f':
            mtime = all_downloads[file].mtime
            if file.exists():
                try:
                    os.utime(file, (mtime, mtime))
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

    # Save and upload the state file.
    logging.info(f"Saving metadata in {STATE_FILE}")
    state_file_lines = [
        f"f::{path}::{node.mtime}::{node.size}" if node.type == "f" else f"d::{path}"
        for path, node in meta.items()
    ]
    payload = f"i::{LOCAL_DIR}\n" + "\n".join(state_file_lines)

    state_dir = STATE_FILE.parent
    try:
        with tempfile.NamedTemporaryFile("w", dir=state_dir, delete=False, encoding="utf-8") as tf:
            tf.write(payload)
            temp_path = tf.name
        
        os.replace(temp_path, STATE_FILE)
        logging.debug("State file updated atomically.")
        
    except Exception as e:
        logging.error(f"Failed to safely save state file: {e}")
        if 'temp_path' in locals() and os.path.exists(temp_path):
            os.unlink(temp_path) # Clean up the loose temp file if it failed
        raise

    logging.info("Uploading new state file")
    proton_upload(STATE_FILE, REMOTE_DIR, fail=True)

    trash_emptied = False
    if EMPTY_TRASH:
        try:
            proton_empty_trash()
        except ProtonCliError as e:
            # Log it as an error, but don't crash the script since the sync itself succeeded!
            logging.error(f"Failed to empty Proton Drive trash:\n {e}")
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
