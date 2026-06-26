#!/usr/bin/env bash
# ══════════════════════════════════════════════════════════════════════════════
# nas_migrate.sh — Copy Documents & Photos from a drive to local path or
#                  Google Drive via 10 GB rotating batch staging
#
# Usage:
#   ./nas_migrate.sh
#   (all inputs are prompted interactively)
#
# What it does:
#   • Scans the drive for documents and photos (skips system/junk files)
#   • Copies into <dest>/Documents/ and <dest>/Photos/
#   • MD5-deduplicates: identical content copied once, extras skipped
#   • Filename conflicts: appends creation date, then -DUPLICATE-YYYY-DD-MM-N
#
# Google Drive mode:
#   • Stages files locally in a temp batch folder (default 10 GB limit)
#   • When the batch hits 10 GB, moves it all to your Google Drive folder
#     and clears the staging area — so local disk never holds more than ~10 GB
#   • Hash database stays local the whole time for speed
#   • Requires Google Drive for Desktop (stream mode recommended so GDrive
#     clears local copies after upload — not mirror mode)
#
# Requirements: macOS built-ins only (md5, mdls, stat, find)
# Compatible with default macOS bash (3.2+)
# ══════════════════════════════════════════════════════════════════════════════

BATCH_LIMIT_GB=10
BATCH_LIMIT=$(( BATCH_LIMIT_GB * 1024 * 1024 * 1024 ))  # bytes

# ══════════════════════════════════════════════════════════════════════════════
# INTERACTIVE PROMPTS
# ══════════════════════════════════════════════════════════════════════════════
echo ""
echo "┌─────────────────────────────────────────────────────┐"
echo "│                   NAS Migration                     │"
echo "└─────────────────────────────────────────────────────┘"
echo ""

# Show available volumes
echo "  Mounted volumes:"
ls /Volumes/ 2>/dev/null | sed 's/^/    /'
echo ""

# Source drive
while true; do
    read -r -p "  Source drive name or path: " DEVICE_INPUT
    [[ -n "$DEVICE_INPUT" ]] && break
    echo "  Please enter a drive name or path."
done

echo ""

# Destination mode: Google Drive or local path
USE_GDRIVE=false
GD_ROOT=""

# Detect Google Drive for Desktop mount
detect_google_drive() {
    # Modern Google Drive for Desktop (macOS 12+)
    local gd
    gd=$(ls -d "$HOME/Library/CloudStorage/GoogleDrive-"*/My\ Drive 2>/dev/null | head -1)
    if [[ -n "$gd" && -d "$gd" ]]; then
        echo "$gd"
        return
    fi
    # Legacy location
    if [[ -d "$HOME/Google Drive/My Drive" ]]; then
        echo "$HOME/Google Drive/My Drive"
        return
    fi
    if [[ -d "$HOME/Google Drive" ]]; then
        echo "$HOME/Google Drive"
        return
    fi
}

GD_DETECTED=$(detect_google_drive)

if [[ -n "$GD_DETECTED" ]]; then
    echo "  Google Drive detected: $GD_DETECTED"
    read -r -p "  Upload to Google Drive? [y/N]: " gd_answer
    if [[ "$gd_answer" =~ ^[Yy]$ ]]; then
        USE_GDRIVE=true
        GD_ROOT="$GD_DETECTED"
        echo ""
        read -r -p "  Google Drive subfolder name [NAS Migration]: " GD_SUBFOLDER
        GD_SUBFOLDER="${GD_SUBFOLDER:-NAS Migration}"
    fi
    echo ""
fi

if [[ "$USE_GDRIVE" == false ]]; then
    while true; do
        read -r -p "  Output path: " OUTPUT_PATH
        [[ -n "$OUTPUT_PATH" ]] && break
        echo "  Please enter an output path."
    done
    echo ""
fi

# ══════════════════════════════════════════════════════════════════════════════
# RESOLVE SOURCE
# ══════════════════════════════════════════════════════════════════════════════
if [[ -d "$DEVICE_INPUT" ]]; then
    SOURCE_PATH="$DEVICE_INPUT"
elif [[ -d "/Volumes/$DEVICE_INPUT" ]]; then
    SOURCE_PATH="/Volumes/$DEVICE_INPUT"
else
    echo "  ERROR: Cannot find volume '$DEVICE_INPUT'"
    echo ""
    echo "  Mounted volumes:"
    ls /Volumes/ 2>/dev/null | sed 's/^/    /'
    echo ""
    exit 1
fi

# ══════════════════════════════════════════════════════════════════════════════
# SET UP DIRECTORIES & LOGGING
# ══════════════════════════════════════════════════════════════════════════════
TIMESTAMP=$(date '+%Y%m%d_%H%M%S')

if [[ "$USE_GDRIVE" == true ]]; then
    # Local staging area — files accumulate here then get mv'd to GDrive
    STAGE_DIR=$(mktemp -d /tmp/nas_migrate_stage.XXXXXX)
    STAGE_DOCS="$STAGE_DIR/Documents"
    STAGE_PHOTOS="$STAGE_DIR/Photos"
    mkdir -p "$STAGE_DOCS" "$STAGE_PHOTOS"

    # Final Google Drive destination
    GD_DEST="$GD_ROOT/$GD_SUBFOLDER"
    GD_DOCS="$GD_DEST/Documents"
    GD_PHOTOS="$GD_DEST/Photos"
    mkdir -p "$GD_DOCS" "$GD_PHOTOS" || {
        echo "  ERROR: Cannot create folders in Google Drive at '$GD_DEST'"
        exit 1
    }

    # Log goes locally alongside the hash DB
    LOG_DIR=$(mktemp -d /tmp/nas_migrate_log.XXXXXX)
    LOG_FILE="$LOG_DIR/migration_log_${TIMESTAMP}.txt"

    OUTPUT_PATH="$GD_DEST"   # used in summary display only
    DOCS_DIR="$STAGE_DOCS"
    PHOTOS_DIR="$STAGE_PHOTOS"
else
    DOCS_DIR="$OUTPUT_PATH/Documents"
    PHOTOS_DIR="$OUTPUT_PATH/Photos"
    LOG_FILE="$OUTPUT_PATH/migration_log_${TIMESTAMP}.txt"
    mkdir -p "$DOCS_DIR" "$PHOTOS_DIR" || {
        echo "  ERROR: Cannot create output directories in '$OUTPUT_PATH'"
        exit 1
    }
fi

# ── Hash database — always local for speed ─────────────────────────────────────
# Format per line: MD5HASH|/full/source/path
HASH_DB=$(mktemp /tmp/nas_migrate_hashes.XXXXXX)

cleanup() {
    rm -f "$HASH_DB"
    if [[ "$USE_GDRIVE" == true ]]; then
        rm -rf "$STAGE_DIR" "$LOG_DIR" 2>/dev/null || true
    fi
}
trap cleanup EXIT

# ── Counters & batch tracker ───────────────────────────────────────────────────
COPIED=0
SKIPPED_DUPE=0
SKIPPED_SYSTEM=0
ERRORS=0
BATCHES_FLUSHED=0
BATCH_BYTES=0   # running byte total for current staging batch

# ══════════════════════════════════════════════════════════════════════════════
# HELPERS
# ══════════════════════════════════════════════════════════════════════════════

# should_skip <filepath> → 0=skip, 1=keep
should_skip() {
    local filepath="$1"
    local filename
    filename=$(basename "$filepath")

    [[ "$filename" == .* ]] && return 0

    case "$filename" in
        .DS_Store|Thumbs.db|desktop.ini|.localized|"Icon"$'\r') return 0 ;;
        ~\$*|*.tmp|*.temp|*.crdownload|*.part|*.swp|*.swo)      return 0 ;;
    esac

    local path
    path=$(dirname "$filepath")
    while [[ "$path" != "/" && "$path" != "$SOURCE_PATH" ]]; do
        local dname
        dname=$(basename "$path")
        [[ "$dname" == .* ]] && return 0
        case "$dname" in
            .Spotlight-V100 | .Trashes      | .fseventsd              | .TemporaryItems |\
            __MACOSX        | RECYCLER       | "\$RECYCLE.BIN"         | "System Volume Information" |\
            Caches          | tmp            | temp                    | ".cache"        |\
            "Windows"       | "WINDOWS"     | "Program Files"         | "Program Files (x86)" )
                return 0 ;;
        esac
        path=$(dirname "$path")
    done

    return 1
}

# get_md5 <filepath> → MD5 hex string
get_md5() {
    md5 -q "$1" 2>/dev/null || true
}

# get_file_size <filepath> → size in bytes
get_file_size() {
    stat -f %z "$1" 2>/dev/null || echo 0
}

# get_creation_date <filepath> → YYYY-DD-MM
get_creation_date() {
    local filepath="$1"
    local raw=""

    raw=$(mdls -raw -name kMDItemContentCreationDate "$filepath" 2>/dev/null \
        | grep -oE '[0-9]{4}-[0-9]{2}-[0-9]{2}' | head -1)

    if [[ -z "$raw" || "$raw" == "(null)" ]]; then
        raw=$(stat -f "%SB" -t "%Y-%m-%d" "$filepath" 2>/dev/null || true)
    fi

    [[ -z "$raw" ]] && raw=$(date "+%Y-%m-%d")

    # YYYY-MM-DD → YYYY-DD-MM
    echo "${raw:0:4}-${raw:8:2}-${raw:5:2}"
}

# build_dest_path <dest_dir> <filename> <source_file> → full path
# 1. filename.ext
# 2. filename-YYYY-DD-MM.ext
# 3. filename-DUPLICATE-YYYY-DD-MM-1.ext  (then -2, -3 …)
build_dest_path() {
    local dest_dir="$1"
    local filename="$2"
    local source_file="$3"

    local base ext
    if [[ "$filename" == *.* ]]; then
        base="${filename%.*}"
        ext=".${filename##*.}"
    else
        base="$filename"
        ext=""
    fi

    local dest="$dest_dir/${base}${ext}"
    [[ ! -e "$dest" ]] && echo "$dest" && return

    local cdate
    cdate=$(get_creation_date "$source_file")
    dest="$dest_dir/${base}-${cdate}${ext}"
    [[ ! -e "$dest" ]] && echo "$dest" && return

    local n=1
    while true; do
        dest="$dest_dir/${base}-DUPLICATE-${cdate}-${n}${ext}"
        [[ ! -e "$dest" ]] && echo "$dest" && return
        n=$((n + 1))
    done
}

# log <message>
log() {
    echo "$1"
    echo "$1" >> "$LOG_FILE"
}

# ── flush_batch ────────────────────────────────────────────────────────────────
# Moves all staged files into Google Drive and resets the staging area.
# Called automatically when BATCH_BYTES >= BATCH_LIMIT, and once at the end.
flush_batch() {
    local staged_docs staged_photos
    staged_docs=$(find "$STAGE_DOCS" -type f 2>/dev/null | wc -l | tr -d ' ')
    staged_photos=$(find "$STAGE_PHOTOS" -type f 2>/dev/null | wc -l | tr -d ' ')
    local total=$(( staged_docs + staged_photos ))

    [[ $total -eq 0 ]] && return

    BATCHES_FLUSHED=$((BATCHES_FLUSHED + 1))
    local gb_label
    gb_label=$(echo "scale=1; $BATCH_BYTES / 1073741824" | bc 2>/dev/null || echo "?")
    log ""
    log "  ── Flushing batch #${BATCHES_FLUSHED} (${gb_label} GB, ${total} files) → Google Drive"

    # Move documents
    if [[ $staged_docs -gt 0 ]]; then
        find "$STAGE_DOCS" -type f -print0 2>/dev/null | while IFS= read -r -d '' f; do
            local dest
            dest=$(build_dest_path "$GD_DOCS" "$(basename "$f")" "$f")
            mv "$f" "$dest" 2>/dev/null \
                && log "  →GD  $(basename "$dest")" \
                || log "  ERR  mv failed: $(basename "$f")"
        done
    fi

    # Move photos
    if [[ $staged_photos -gt 0 ]]; then
        find "$STAGE_PHOTOS" -type f -print0 2>/dev/null | while IFS= read -r -d '' f; do
            local dest
            dest=$(build_dest_path "$GD_PHOTOS" "$(basename "$f")" "$f")
            mv "$f" "$dest" 2>/dev/null \
                && log "  →GD  $(basename "$dest")" \
                || log "  ERR  mv failed: $(basename "$f")"
        done
    fi

    BATCH_BYTES=0
    log "  ── Batch #${BATCHES_FLUSHED} handed to Google Drive ✓"
    log ""
}

# ── process_file <filepath> <dest_dir> ────────────────────────────────────────
process_file() {
    local filepath="$1"
    local dest_dir="$2"

    if should_skip "$filepath"; then
        SKIPPED_SYSTEM=$((SKIPPED_SYSTEM + 1))
        return 0
    fi

    local hash
    hash=$(get_md5 "$filepath")

    if [[ -z "$hash" ]]; then
        log "  WARN  Cannot hash — skipping: $(basename "$filepath")"
        ERRORS=$((ERRORS + 1))
        return 0
    fi

    # Duplicate check
    local existing_line existing_path
    existing_line=$(grep "^${hash}|" "$HASH_DB" 2>/dev/null | head -1 || true)
    if [[ -n "$existing_line" ]]; then
        existing_path="${existing_line#*|}"
        log "  SKIP  $(basename "$filepath")  ← dup of $(basename "$existing_path")"
        SKIPPED_DUPE=$((SKIPPED_DUPE + 1))
        return 0
    fi

    printf "%s|%s\n" "$hash" "$filepath" >> "$HASH_DB"

    local dest
    dest=$(build_dest_path "$dest_dir" "$(basename "$filepath")" "$filepath")

    if cp "$filepath" "$dest" 2>/dev/null; then
        log "  OK    $(basename "$dest")"
        COPIED=$((COPIED + 1))

        # Track batch size and flush if over the limit (Google Drive mode only)
        if [[ "$USE_GDRIVE" == true ]]; then
            local fsize
            fsize=$(get_file_size "$dest")
            BATCH_BYTES=$((BATCH_BYTES + fsize))
            if [[ $BATCH_BYTES -ge $BATCH_LIMIT ]]; then
                flush_batch
            fi
        fi
    else
        log "  ERR   Cannot copy: $filepath"
        ERRORS=$((ERRORS + 1))
    fi
}

# ══════════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════════

MODE_LABEL="Local path"
[[ "$USE_GDRIVE" == true ]] && MODE_LABEL="Google Drive (${BATCH_LIMIT_GB} GB batches)"

echo "┌─────────────────────────────────────────────────────┐"
printf "│  Source : %-41s│\n" "$SOURCE_PATH"
printf "│  Output : %-41s│\n" "$OUTPUT_PATH"
printf "│  Mode   : %-41s│\n" "$MODE_LABEL"
printf "│  Log    : %-41s│\n" "$(basename "$LOG_FILE")"
echo "└─────────────────────────────────────────────────────┘"
echo ""

{
    echo "NAS Migration Log — $(date)"
    echo "Source : $SOURCE_PATH"
    echo "Output : $OUTPUT_PATH"
    echo "Mode   : $MODE_LABEL"
    echo ""
} >> "$LOG_FILE"

# ── Documents ──────────────────────────────────────────────────────────────────
echo "── Documents ──────────────────────────────────────────"
log "── Documents ──────────────────────────────────────────"

while IFS= read -r -d '' file; do
    process_file "$file" "$DOCS_DIR"
done < <(find "$SOURCE_PATH" -type f \( \
    -iname "*.pdf"     -o -iname "*.doc"      -o -iname "*.docx"    \
    -o -iname "*.txt"  -o -iname "*.rtf"      -o -iname "*.odt"     \
    -o -iname "*.xls"  -o -iname "*.xlsx"     -o -iname "*.xlsm"    \
    -o -iname "*.csv"  -o -iname "*.ppt"      -o -iname "*.pptx"    \
    -o -iname "*.pptm" -o -iname "*.pages"    -o -iname "*.numbers"  \
    -o -iname "*.keynote" -o -iname "*.md"    -o -iname "*.epub"    \
    -o -iname "*.wpd"  -o -iname "*.dotx"     -o -iname "*.docm"    \
\) -print0 2>/dev/null)

# ── Photos ─────────────────────────────────────────────────────────────────────
echo ""
echo "── Photos ─────────────────────────────────────────────"
log ""
log "── Photos ─────────────────────────────────────────────"

while IFS= read -r -d '' file; do
    process_file "$file" "$PHOTOS_DIR"
done < <(find "$SOURCE_PATH" -type f \( \
    -iname "*.jpg"   -o -iname "*.jpeg"  -o -iname "*.png"   -o -iname "*.gif"   \
    -o -iname "*.bmp"   -o -iname "*.tiff"  -o -iname "*.tif"   -o -iname "*.heic"  \
    -o -iname "*.heif"  -o -iname "*.webp"  -o -iname "*.svg"   -o -iname "*.psd"   \
    -o -iname "*.raw"   -o -iname "*.cr2"   -o -iname "*.cr3"   -o -iname "*.nef"   \
    -o -iname "*.arw"   -o -iname "*.dng"   -o -iname "*.orf"   -o -iname "*.rw2"   \
    -o -iname "*.raf"   -o -iname "*.x3f"   -o -iname "*.erf"   -o -iname "*.mos"   \
    -o -iname "*.mef"   -o -iname "*.rwl"   -o -iname "*.srw"   -o -iname "*.srf"   \
    -o -iname "*.sr2"   -o -iname "*.nrw"   -o -iname "*.fff"   -o -iname "*.iiq"   \
    -o -iname "*.3fr"   -o -iname "*.cap"   -o -iname "*.ptx"   -o -iname "*.pef"   \
    -o -iname "*.kdc"   -o -iname "*.mdc"   -o -iname "*.mrw"   -o -iname "*.rwz"   \
\) -print0 2>/dev/null)

# ── Final batch flush (Google Drive mode) ──────────────────────────────────────
if [[ "$USE_GDRIVE" == true ]]; then
    flush_batch
    # Copy log file into Google Drive destination for the client record
    cp "$LOG_FILE" "$GD_DEST/migration_log_${TIMESTAMP}.txt" 2>/dev/null || true
fi

# ── Summary ────────────────────────────────────────────────────────────────────
if [[ "$USE_GDRIVE" == true ]]; then
    DEST_DISPLAY="$GD_DEST"
else
    DEST_DISPLAY="$OUTPUT_PATH"
fi

SUMMARY=$(cat <<EOF

┌─────────────────────────────────────────────────────┐
│  Summary                                            │
├─────────────────────────────────────────────────────┤
│  Files copied         : $(printf "%-24s" "$COPIED")│
│  Duplicates skipped   : $(printf "%-24s" "$SKIPPED_DUPE (same MD5)")│
│  System/junk skipped  : $(printf "%-24s" "$SKIPPED_SYSTEM")│
│  Errors               : $(printf "%-24s" "$ERRORS")│
$(  [[ "$USE_GDRIVE" == true ]] && printf "│  Batches sent to GDrive: %-23s│\n" "$BATCHES_FLUSHED")
├─────────────────────────────────────────────────────┤
│  Documents → $DEST_DISPLAY/Documents
│  Photos    → $DEST_DISPLAY/Photos
└─────────────────────────────────────────────────────┘
EOF
)

echo "$SUMMARY"
echo "$SUMMARY" >> "$LOG_FILE"

if [[ "$USE_GDRIVE" == true ]]; then
    echo "  Note: Google Drive is uploading files in the background."
    echo "        Use stream mode (not mirror) to avoid filling local disk."
    echo ""
fi

if [[ $ERRORS -gt 0 ]]; then
    echo "  ⚠  $ERRORS error(s) — check the log for details:"
    echo "     $LOG_FILE"
    echo ""
fi
