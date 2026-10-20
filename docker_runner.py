import os
import time
import datetime
import subprocess
import logging

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)

def recursive_chown(target_path, uid=1000, gid=1000):
    """Recursively changes ownership of a path to the specified UID and GID."""
    target = os.path.abspath(target_path)
    if not os.path.exists(target):
        logging.warning(f"Path does not exist for chown: {target}")
        return

    logging.info(f"Adjusting permissions for {target} to UID {uid}, GID {gid}...")
    
    # Change ownership of the root target itself
    os.chown(target, uid, gid)
    
    # Walk through all subdirectories and files
    for root, dirs, files in os.walk(target):
        for name in dirs:
            path = os.path.join(root, name)
            try:
                os.chown(path, uid, gid)
            except Exception as e:
                logging.error(f"Failed to chown directory {path}: {e}")
                
        for name in files:
            path = os.path.join(root, name)
            try:
                os.chown(path, uid, gid)
            except Exception as e:
                logging.error(f"Failed to chown file {path}: {e}")
                
    logging.info("✅ Ownership adjustment complete.")

# 1. Gather all runtime parameters from Docker environment variables
INTERVAL_HOURS = int(os.environ.get("INTERVAL_HOURS", 24))
START_HOUR = os.environ.get("START_HOUR", None)
REMOTE_DIR = os.environ.get("REMOTE_DIR", "/my-files/backup")
EMPTY_TRASH_ENV = os.environ.get("EMPTY_TRASH", "false").lower()
TWO_WAY_ENV = os.environ.get("TWO_WAY", "false").lower()
TWO_WAY_TOML = "true" if TWO_WAY_ENV in ["true", "1", "yes"] else "false"
RESOLVE_STRATEGY = os.environ.get("RESOLVE_STRATEGY", "newest").strip('"\'').lower()
LOG_LEVEL = os.environ.get("LOG_LEVEL", "quiet").strip('"\'').lower()
USER = int(os.environ.get("USER",1000))
GROUP = int(os.environ.get("GROUP",1000))

# Normalize boolean mapping for TOML template translation
EMPTY_TRASH_TOML = "true" if EMPTY_TRASH_ENV in ["true", "1", "yes"] else "false"

# Ensure the local workspace and cache target paths are explicitly present
# This guarantees write privileges even if external volume mounts alter the tree layout
os.makedirs("/app/.cache", exist_ok=True)
recursive_chown("/app/.cache", uid=USER, gid=GROUP)
stat_info = os.stat("/data/backup")
if TWO_WAY_ENV == "true" and stat_info.st_uid != USER:
    try:
        os.chown("/data/backup", USER, GROUP)
    except:
        logging.warning(f"Local directory not owned by user {USER}. Two-Way sync not possible")

# 2. FIXED: Point 'proton_drive_binary' directly to the local executable file context
CONFIG_TEMPLATE = f"""local_dir = "/data/backup"
remote_dir = "{REMOTE_DIR}"
proton_drive_binary = "/app/proton-drive"
proton_drive_cache_dir = "./.cache"
unsafe_login = true
empty_trash = {EMPTY_TRASH_TOML}
log_level = "{LOG_LEVEL}"
two_way = {TWO_WAY_TOML}
resolve_strategy = "{RESOLVE_STRATEGY}"
"""

logging.info("Generating system configuration file...")
with open("/app/config.toml", "w", encoding="utf-8") as f:
    f.write(CONFIG_TEMPLATE)

logging.info(f"Container initialized. Sync Cadence: Every {INTERVAL_HOURS} hour(s).")
if START_HOUR is not None:
    START_HOUR = int(START_HOUR)
    logging.info(f"Target initial alignment window locked to: {START_HOUR:02d}:00")

def get_seconds_until_align(start_hour):
    now = datetime.datetime.now()
    target = now.replace(hour=start_hour, minute=0, second=0, microsecond=0)
    if now >= target:
        target += datetime.timedelta(days=1)
    return int((target - now).total_seconds())

# --- MASTER LOGIC PROCESS ENGINE ---
logged_in = True
while True:
    if not logged_in:
        recursive_chown("/app/.cache", uid=USER, gid=GROUP)
    # 3. Interactive Login Safety Gate Checklist
    # Using './proton-drive' explicitly to remain relative to our WORKDIR
    result = subprocess.run(["./proton-drive", "filesystem", "info", "/my-files", "-j"], capture_output=True, text=True, user=USER,group=USER)
    
    if "You need to login first" in result.stderr:
        logged_in = False
        logging.warning("🔐 Credentials missing! Run this command on your host terminal to log in:")
        logging.warning("   docker exec -it proton-sync /app/proton-drive auth login")
        time.sleep(30)
        continue

    # 4. Handle Initial Start-Time Gate Alignment (Fires once on boot)
    if START_HOUR is not None:
        initial_wait = get_seconds_until_align(START_HOUR)
        readable_wait = str(datetime.timedelta(seconds=initial_wait))
        logging.info(f"⏰ Aligning engine timeline. Initial sleep for {readable_wait} until target window ({START_HOUR:02d}:00)...")
        time.sleep(initial_wait)
        START_HOUR = None  # Clear constraint so subsequent passes follow the raw interval cadence

    # 5. Time Gate Cleared. Trigger your native synchronization engine script
    logging.info("🚀 Sync interval window reached. Executing backup synchronization pass...")
    try:
        # Runs your pristine core script—which reads the generated config.toml natively!
        subprocess.run(["python3", "proton_drive_sync.py"], check=True, user=USER,group=USER)
        logging.info("✅ Synchronization pass finished cleanly.")
    except subprocess.CalledProcessError as e:
        logging.error(f"❌ Synchronization routine failed with exit code: {e.returncode}")
        time.sleep(60)

    # 6. Process Cadence Interval Sleep
    logging.info(f"💤 Cadence complete. Sleeping for {INTERVAL_HOURS} hour(s)...")
    time.sleep(INTERVAL_HOURS * 3600)
