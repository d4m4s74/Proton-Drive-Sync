import os
import subprocess
import sys
import tomllib
import pathlib
import logging
import time

SCRIPT_DIR = str(pathlib.Path(__file__).parent.resolve())
HOME_DIR = os.environ.get("HOME", os.path.expanduser("~"))

try:
    with open(str(SCRIPT_DIR) + "/config.toml", "rb") as f:
        config = tomllib.load(f)
except:
    print("Missing config file")
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

# Run hook script before running bakup
BEFORE_RUN_SCRIPT = os.path.join(SCRIPT_DIR, "before_run.sh")

# Check if the file exists
if os.path.exists(BEFORE_RUN_SCRIPT):
    logging.info(f"Found pre-backup hook script. Executing: {BEFORE_RUN_SCRIPT}")
    
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

LOCAL_DIR = config['local_dir'].replace("${SCRIPT_DIR}", SCRIPT_DIR).replace("${HOME}", HOME_DIR)
PROTON_DRIVE_BINARY = config['proton_drive_binary'].replace("${SCRIPT_DIR}", SCRIPT_DIR).replace("${HOME}", HOME_DIR)
REMOTE_DIR = config['remote_dir']
if 'proton_drive_cache_dir' in config:
    os.environ["PROTON_DRIVE_CACHE_DIR"] = config['proton_drive_cache_dir'].replace("${SCRIPT_DIR}",SCRIPT_DIR)
if 'unsafe_login' in config and config['unsafe_login'] == True:
    os.environ['PROTON_DRIVE_CREDENTIALS_STORE'] = "unsafe_file"
STATE_FILE_NAME = ".proton_sync_state"

# Check for the explicit Docker image environment flag
if os.environ.get("AM_I_DOCKER") == "true":
    STATE_FILE = os.path.join("./.cache", STATE_FILE_NAME) # Locked to persistent container volume
else:
    STATE_FILE = os.path.join(LOCAL_DIR, STATE_FILE_NAME)  # Clean native host filesystem fallback

EMPTY_TRASH = config.get('empty_trash', False)

#Test command to see if logged in
result = subprocess.run([PROTON_DRIVE_BINARY, 'filesystem', 'info', '/my-files', '-j'],capture_output=True, text=True)
logging.debug(f"Logged in test (filesystem info /my-files) CLI execution results: {result}")
if "You need to login first" in result.stderr:
    if sys.stdin.isatty():
        subprocess.run([PROTON_DRIVE_BINARY, "auth", "login"], check=True)
    else:
        logging.error(f"Proton Drive not logged in. Run 'python3 {sys.argv[0]}' in the terminal to log in")
        sys.exit(1)
elif result.stderr.strip():
    logging.error(f"Unable to connect to Proton Drive\n {result.stderr}")



# Load state file
if os.path.exists(STATE_FILE):
    with open(STATE_FILE, "r", encoding="utf-8") as f:
        old_meta = {line.strip() for line in f if line.strip()}
    logging.debug(f"Loading old metadata from: {STATE_FILE}.")
else:
    old_meta = set()  # First run fallback
    logging.info("First run. Uploading everything.")

filenames = set()
meta = set()
logging.debug(f"Starting walk of {LOCAL_DIR}")
start_walk = time.perf_counter()
for root, dirs, files in os.walk(LOCAL_DIR):
    for name in files:
        if name == STATE_FILE_NAME:
            continue  # Skip tracking the state file itself!
        path = os.path.join(root,name)
        filenames.add(path)
        meta.add(f"f::{path}::{int(os.path.getmtime(path))}::{os.path.getsize(path)}")
    for name in dirs:
        path = os.path.join(root,name)
        filenames.add(path)
        meta.add(f"d::{path}")
end_walk = time.perf_counter()
duration = end_walk - start_walk
logging.debug(f"Local directory walk completed in {duration:.4f} seconds.")
logging.debug(f"Scanned {len(filenames)} local items into the live meta set.")
upload = meta - old_meta #every file/metadata combo that is in the new set but not in the old set. Contains inefficiencies.
uploaded = len(upload)
temp = upload.copy()
for file in temp:
    path = file.split('::')[1]
    if f"d::{os.path.split(path)[0]}" in temp: #If the folder is new too you don't have to upload separately
        upload.remove(file)

delete = old_meta - meta #every file/metadata that is in the old set but not the new set. Contains false positives.
temp = delete.copy()
for file in temp:
    path = file.split('::')[1]
    if path in filenames: #If the file is being replaced, not removed, remove from the delete list.
        delete.remove(file)
deleted = len(delete)
replaced = len(temp) - deleted
uploaded = uploaded - replaced
temp = delete.copy()
for file in temp:
    path = file.split('::')[1]
    if f"d::{os.path.split(path)[0]}" in temp: #If the folder is to be deleted too you don't have to delete separately
        delete.remove(file)
logging.debug(f"Calculated change sets: {len(upload)} items to upload, {len(delete)} items to trash.")

# Preparation for upload. Check if REMOTE_DIR exists

result = subprocess.run([PROTON_DRIVE_BINARY, 'filesystem', 'info', REMOTE_DIR, '-j'],capture_output=True, text=True)
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
            subresult = subprocess.run([PROTON_DRIVE_BINARY, 'filesystem', 'create-folder', path, d, '-j'],capture_output=True, text=True)
            logging.debug(f"Folder creation CLI execution results: {subresult}")
            if subresult.stderr.strip():
                logging.error(f"Unexpected error\n {subresult.stderr}")
                sys.exit(1)
        path = os.path.join(path, d)

# Uploading the new files

for file in upload:
    path = file.split('::')[1]
    server_path = os.path.join(REMOTE_DIR,os.path.relpath(path, LOCAL_DIR))
    target_folder = os.path.split(server_path)[0]
    logging.info(f"Uploading {path} to {target_folder}")
    result = subprocess.run([PROTON_DRIVE_BINARY, 'filesystem', 'upload', '-f', 'replace', '-d', 'merge', path, target_folder, '-j'],capture_output=True, text=True)
    logging.debug(f"File/folder upload CLI execution results: {result}")
    if result.stderr.strip():
        logging.error(f"Unexpected error\n {result.stderr}")
        sys.exit(1)

for file in delete:
    path = file.split('::')[1]
    server_path = os.path.join(REMOTE_DIR,os.path.relpath(path, LOCAL_DIR))
    logging.info(f"Trashing {server_path}")
    result = subprocess.run([PROTON_DRIVE_BINARY, 'filesystem', 'trash', server_path, '-j'],capture_output=True, text=True)
    logging.debug(f"File/folder deletion CLI execution results: {result}")
    if result.stderr.strip():
        logging.error(f"Unexpected error\n {result.stderr}")
        sys.exit(1)

# Save and upload the state file. This also works as a keepalive
logging.info(f"Saving metadata in {STATE_FILE}")
with open(STATE_FILE, "w", encoding="utf-8") as f:
   f.write("\n".join(meta))

result = subprocess.run([PROTON_DRIVE_BINARY, 'filesystem', 'upload', '-f', 'replace', '-d', 'merge', STATE_FILE, REMOTE_DIR, '-j'],capture_output=True, text=True)
logging.debug(f"State file upload CLI execution results: {result}")
if result.stderr.strip():
    logging.error(f"Unexpected error\n {result.stderr}")
    sys.exit(1)

trash_emptied = False
if EMPTY_TRASH:
    result = subprocess.run([PROTON_DRIVE_BINARY, 'filesystem', 'empty-trash'], capture_output=True, text=True)
    logging.debug(f"Empty trash CLI execution results: {result}")
    
    if result.stderr.strip():
        # Log it as an error, but don't crash the script since the sync itself succeeded!
        logging.error(f"Failed to empty Proton Drive trash:\n{result.stderr}")
    else:
        # Only turn on the print text string if it succeeded cleanly
        trash_emptied = True

timestamp = time.strftime('%Y-%m-%d %H:%M:%S')
print(f"{timestamp} [SUMMARY] Synced: {uploaded} uploaded, {replaced} replaced, {deleted} deleted{', trash emptied' if trash_emptied else ''}.")