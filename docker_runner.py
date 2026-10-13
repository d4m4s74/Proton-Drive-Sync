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

# 1. Gather all runtime parameters from Docker environment variables
INTERVAL_HOURS = int(os.environ.get("INTERVAL_HOURS", 24))
START_HOUR = os.environ.get("START_HOUR", None)
REMOTE_DIR = os.environ.get("REMOTE_DIR", "/my-files/backup")
EMPTY_TRASH_ENV = os.environ.get("EMPTY_TRASH", "false").lower()

# Normalize boolean mapping for TOML template translation
EMPTY_TRASH_TOML = "true" if EMPTY_TRASH_ENV in ["true", "1", "yes"] else "false"

# Ensure the local workspace and cache target paths are explicitly present
# This guarantees write privileges even if external volume mounts alter the tree layout
os.makedirs("/app/.cache", exist_ok=True)

# 2. FIXED: Point 'proton_drive_binary' directly to the local executable file context
CONFIG_TEMPLATE = f"""local_dir = "/data/backup"
remote_dir = "{REMOTE_DIR}"
proton_drive_binary = "./proton-drive"
proton_drive_cache_dir = "./.cache"
unsafe_login = true
empty_trash = {EMPTY_TRASH_TOML}
log_level = "quiet"
"""

logging.info("Generating system configuration file...")
with open("config.toml", "w", encoding="utf-8") as f:
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
while True:
    # 3. Interactive Login Safety Gate Checklist
    # Using './proton-drive' explicitly to remain relative to our WORKDIR
    result = subprocess.run(["./proton-drive", "filesystem", "info", "/my-files", "-j"], capture_output=True, text=True)
    
    if "You need to login first" in result.stderr:
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
        subprocess.run(["python3", "proton_drive_sync.py"], check=True)
        logging.info("✅ Synchronization pass finished cleanly.")
    except subprocess.CalledProcessError as e:
        logging.error(f"❌ Synchronization routine failed with exit code: {e.returncode}")
        time.sleep(60)

    # 6. Process Cadence Interval Sleep
    logging.info(f"💤 Cadence complete. Sleeping for {INTERVAL_HOURS} hour(s)...")
    time.sleep(INTERVAL_HOURS * 3600)
