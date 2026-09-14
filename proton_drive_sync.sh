#!/usr/bin/env bash
# Sync /srv/data/backup to Proton Drive /my-files/backup
# Tracks files and directories locally to detect changes and deletions.
# If you do not have a working keyring / secret store, uncomment the unsafe_file lines below.

# Uncomment if your environment has no usable keyring / secret store:
# export PROTON_DRIVE_CREDENTIALS_STORE=unsafe_file
# export PROTON_DRIVE_CACHE_DIR="${HOME}/.config/proton-drive-cli"

set -u -o pipefail

LOCAL_BACKUP_DIR="/srv/data/backup"
REMOTE_BACKUP_DIR="/my-files/backup"
STATE_FILE="${LOCAL_BACKUP_DIR}/.proton_sync_state"
TEMP_STATE_FILE="${STATE_FILE}.tmp"
PROTON_BIN="/usr/local/bin/proton-drive"

log() {
    echo "[$(date '+%F %T')] $*"
}

folders_created=0
files_uploaded=0
folders_uploaded=0
files_removed=0
folders_removed=0
errors=0

sort_paths_asc() {
    awk '{ print length, $0 }' | sort -n | sed 's/^[0-9][0-9]* //'
}

sort_paths_desc() {
    awk '{ print length, $0 }' | sort -rn | sed 's/^[0-9][0-9]* //'
}

log "=== Starting Proton Drive sync evaluation ==="

if [[ ! -d "$LOCAL_BACKUP_DIR" ]]; then
    log "ERROR: Backup directory does not exist: $LOCAL_BACKUP_DIR" >&2
    exit 1
fi

if [[ ! -x "$PROTON_BIN" ]]; then
    log "ERROR: Proton Drive CLI not found or not executable: $PROTON_BIN" >&2
    exit 1
fi

: > "$TEMP_STATE_FILE"

declare -A old_files=()
declare -A old_dirs=()

if [[ -f "$STATE_FILE" ]]; then
    while IFS='|' read -r kind path stat; do
        [[ -n "${kind:-}" ]] || continue
        case "$kind" in
            F) old_files["$path"]="$stat" ;;
            D) old_dirs["$path"]=1 ;;
        esac
    done < "$STATE_FILE"
fi

declare -A current_files=()
declare -A current_dirs=()
declare -A changed_root_files=()
declare -A changed_dirs=()

to_remote_dir() {
    local local_dir="$1"

    if [[ "$local_dir" == "$LOCAL_BACKUP_DIR" ]]; then
        printf '%s\n' "$REMOTE_BACKUP_DIR"
    else
        local rel_dir="${local_dir#"$LOCAL_BACKUP_DIR"/}"
        printf '%s/%s\n' "$REMOTE_BACKUP_DIR" "$rel_dir"
    fi
}

to_remote_parent_for_upload() {
    local local_dir="$1"

    if [[ "$local_dir" == "$LOCAL_BACKUP_DIR" ]]; then
        printf '%s\n' "$REMOTE_BACKUP_DIR"
    else
        local remote_dir
        remote_dir="$(to_remote_dir "$local_dir")"
        dirname "$remote_dir"
    fi
}

ensure_remote_dir() {
    local remote_dir="$1"

    [[ "$remote_dir" == "$REMOTE_BACKUP_DIR" ]] && return 0

    local rel="${remote_dir#"$REMOTE_BACKUP_DIR"/}"
    local current_parent="$REMOTE_BACKUP_DIR"
    local part current

    IFS='/' read -r -a parts <<< "$rel"
    for part in "${parts[@]}"; do
        current="${current_parent}/${part}"

        if ! "$PROTON_BIN" filesystem info "$current" >/dev/null 2>&1; then
            log "Creating remote folder: $current"
            if "$PROTON_BIN" filesystem create-folder "$current_parent" "$part" >/dev/null; then
                ((folders_created++))
            else
                log "WARN: Failed to create remote folder: $current"
                ((errors++))
            fi
        fi

        current_parent="$current"
    done
}

is_under_uploaded_parent() {
    local dir="$1"
    local parent
    for parent in "${uploaded_dirs[@]}"; do
        if [[ "$dir" == "$parent" || "$dir" == "$parent"/* ]]; then
            return 0
        fi
    done
    return 1
}

while IFS= read -r -d '' path; do
    [[ "$path" == "$STATE_FILE" ]] && continue
    [[ "$path" == "$TEMP_STATE_FILE" ]] && continue

    if [[ -f "$path" ]]; then
        relpath="${path#"$LOCAL_BACKUP_DIR"/}"
        current_files["$relpath"]=1

        current_stat="$(stat -c '%Y:%s' "$path")"
        stored_stat="${old_files[$relpath]:-}"

        if [[ "$current_stat" != "$stored_stat" ]]; then
            log "Changed file: $relpath"

            parent_dir="$(dirname "$path")"
            if [[ "$parent_dir" == "$LOCAL_BACKUP_DIR" ]]; then
                changed_root_files["$path"]=1
            else
                changed_dirs["$parent_dir"]=1
            fi
        fi

        printf 'F|%s|%s\n' "$relpath" "$current_stat" >> "$TEMP_STATE_FILE"
    elif [[ -d "$path" ]]; then
        relpath="${path#"$LOCAL_BACKUP_DIR"/}"
        [[ -z "$relpath" ]] && continue
        current_dirs["$relpath"]=1
        printf 'D|%s|\n' "$relpath" >> "$TEMP_STATE_FILE"
    fi
done < <(find "$LOCAL_BACKUP_DIR" -mindepth 1 -print0)

declare -a deleted_dirs=()
for relpath in "${!old_dirs[@]}"; do
    if [[ -z "${current_dirs[$relpath]+x}" ]]; then
        deleted_dirs+=("$relpath")
    fi
done

if [[ "${#deleted_dirs[@]}" -gt 0 ]]; then
    mapfile -t deleted_dirs_sorted < <(
        printf '%s\n' "${deleted_dirs[@]}" | sort_paths_desc
    )

    for relpath in "${deleted_dirs_sorted[@]}"; do
        remote_path="${REMOTE_BACKUP_DIR}/${relpath}"
        log "Trashing remote directory: $remote_path"
        if "$PROTON_BIN" filesystem trash "$remote_path" >/dev/null; then
            ((folders_removed++))
        else
            log "WARN: Failed to trash remote directory: $remote_path"
            ((errors++))
        fi
    done
fi

declare -a deleted_files=()
for relpath in "${!old_files[@]}"; do
    if [[ -z "${current_files[$relpath]+x}" ]]; then
        skip_file=0
        for deleted_dir in "${deleted_dirs[@]}"; do
            if [[ "$relpath" == "$deleted_dir" || "$relpath" == "$deleted_dir"/* ]]; then
                skip_file=1
                break
            fi
        done

        if [[ "$skip_file" -eq 0 ]]; then
            deleted_files+=("$relpath")
        fi
    fi
done

if [[ "${#deleted_files[@]}" -gt 0 ]]; then
    mapfile -t deleted_files_sorted < <(
        printf '%s\n' "${deleted_files[@]}" | sort_paths_desc
    )

    for relpath in "${deleted_files_sorted[@]}"; do
        remote_path="${REMOTE_BACKUP_DIR}/${relpath}"
        log "Trashing remote file: $remote_path"
        if "$PROTON_BIN" filesystem trash "$remote_path" >/dev/null; then
            ((files_removed++))
        else
            log "WARN: Failed to trash remote file: $remote_path"
            ((errors++))
        fi
    done
fi

for file in "${!changed_root_files[@]}"; do
    log "Uploading root-level file: $file"
    if "$PROTON_BIN" filesystem upload -f replace "$file" "$REMOTE_BACKUP_DIR" >/dev/null; then
        ((files_uploaded++))
    else
        log "WARN: Upload failed for file: $file"
        ((errors++))
    fi
done

uploaded_dirs=()
if [[ "${#changed_dirs[@]}" -gt 0 ]]; then
    mapfile -t dirs_sorted < <(
        printf '%s\n' "${!changed_dirs[@]}" | sort_paths_asc
    )

    for local_dir in "${dirs_sorted[@]}"; do
        if is_under_uploaded_parent "$local_dir"; then
            continue
        fi

        remote_upload_parent="$(to_remote_parent_for_upload "$local_dir")"
        ensure_remote_dir "$remote_upload_parent"

        log "Uploading directory: $local_dir -> $remote_upload_parent"
        if "$PROTON_BIN" filesystem upload -f replace -d merge "$local_dir" "$remote_upload_parent" >/dev/null; then
            ((folders_uploaded++))
        else
            log "WARN: Upload failed for directory: $local_dir"
            ((errors++))
        fi

        uploaded_dirs+=("$local_dir")
    done
fi

if [[ "$errors" -eq 0 ]]; then
    if mv -f "$TEMP_STATE_FILE" "$STATE_FILE"; then
        :
    else
        log "ERROR: Failed to save state file"
        ((errors++))
    fi
else
    log "WARN: Sync had errors; keeping previous state so failed items will retry next run"
    rm -f "$TEMP_STATE_FILE"
fi

if [[ "$folders_created" -eq 0 && "$files_uploaded" -eq 0 && "$folders_uploaded" -eq 0 && "$files_removed" -eq 0 && "$folders_removed" -eq 0 && "$errors" -eq 0 ]]; then
    "$PROTON_BIN" filesystem list /my-files >/dev/null 2>&1 || true
fi

log "Summary: ${folders_created} folders created, ${files_uploaded} files uploaded, ${folders_uploaded} folders uploaded, ${files_removed} files removed, ${folders_removed} folders removed, ${errors} errors"
log "=== Proton Drive sync evaluation complete ==="

exit 0
