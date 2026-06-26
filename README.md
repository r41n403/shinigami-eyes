# Shinigami Eyes
### File Migration System
*developed by r41n403*

A macOS desktop app for migrating files from flash drives and external storage to a NAS or Google Drive. Deduplicates by MD5 hash across multiple drives, resumes safely after interruptions, and uploads directly to Google Drive via rclone with no local cache buildup.

---

## Features

- **Deduplication** — MD5 hash check on every file. Duplicates are skipped regardless of filename. Hash database persists across drives and sessions, so a file seen on Drive A won't be copied again from Drive B.
- **Resume** — If a run is interrupted (crash, sleep, abort), restart the app and choose Resume. Already-processed files are skipped instantly.
- **Google Drive via rclone** — Uploads directly to Google Drive through the API. Files never sit in `~/Library/CloudStorage`, so the local drive doesn't fill up.
- **Orphan cleanup** — On startup, any leftover staging folders from crashed runs are detected and removed automatically.
- **Safe abort** — Hitting ABORT mid-batch leaves staged files unconfirmed. On next run they're reprocessed from source cleanly.
- **Single-pass walk** — Scans the source drive once, routing documents and photos simultaneously. No double traversal.
- **Hash-while-copy** — Files are streamed once to staging while computing the MD5. No double-read from the (potentially slow) source drive.

---

## Requirements

- macOS
- Python 3 with tkinter (`brew install python` or `brew install python-tk`)
- **For Google Drive uploads:** rclone (`brew install rclone`)

---

## Setup

### 1. Install Python
```bash
brew install python
```

### 2. Install rclone (Google Drive mode only)
```bash
brew install rclone
rclone config
```

During `rclone config`:
- Choose `n` for new remote
- Name it `gdrive`
- Choose `drive` as the type
- Leave client ID and secret blank
- Choose scope `1` (full access)
- Follow the browser auth flow

### 3. Run the app
Double-click **Shinigami Eyes.app** on your Desktop.

Or from Terminal:
```bash
python3 nas_migrate_gui.py
```

---

## Usage

1. **Source drive** — Browse to the external drive or folder you want to migrate from (`/Volumes/...`)
2. **Destination** — Choose Local folder or Google Drive
   - Local: pick an output folder
   - Google Drive: pick your rclone remote and subfolder name (default: `NAS Migration`)
3. **Execute** — The app scans the drive and copies Documents and Photos into separate subfolders
4. **Resume** — If a previous run exists for the same destination, the app asks whether to resume or start fresh

---

## Output Structure

```
NAS Migration/
├── Documents/     # pdf, docx, xlsx, pptx, pages, numbers, keynote, txt, etc.
└── Photos/        # jpg, png, heic, raw, dng, cr2, tiff, psd, and all major raw formats
```

Filename conflicts are resolved by appending the file's creation date, then a counter:
```
photo.jpg
photo-2024-15-03.jpg
photo-DUPLICATE-2024-15-03-1.jpg
```

---

## State Files

All state is stored locally at `~/.shinigami_eyes/` — never on Google Drive or the NAS.

| File | Purpose |
|------|---------|
| `hashes.db` | Persistent MD5 hash database across all runs and drives |
| `progress_<hash>.db` | Per-destination record of completed source paths (enables resume) |
| `migration_log_<timestamp>.txt` | Log file saved after each run |

To start completely fresh (wipes dedup history and resume state):
```bash
rm -rf ~/.shinigami_eyes/
```

To clear resume state for one destination only, delete its `progress_<hash>.db` file.

---

## Google Drive Batch Behavior

Files are staged locally in 10 GB batches in a temp folder (`$TMPDIR/nas_migrate_stage_*/`). When a batch reaches 10 GB, rclone uploads the entire batch to Google Drive. Files are only marked as done in the progress DB after rclone confirms a successful upload. If rclone fails or the app is killed mid-upload, the batch is not confirmed and will be retried from source on next resume.

---

## Troubleshooting

**rclone auth times out on startup**
Run `rclone about gdrive:` in Terminal. If it prompts you to open a browser, complete the auth flow there first.

**App shows nothing when Execute is pressed**
Make sure the source path exists and, for Google Drive mode, that rclone is installed and configured.

**Leftover staging folders filling disk**
The app cleans these up on startup. You can also remove them manually:
```bash
rm -rf "$TMPDIR"/nas_migrate_stage_*/
```

**Files showing as duplicates they shouldn't be**
The hash DB at `~/.shinigami_eyes/hashes.db` persists forever. If a file was incorrectly hashed or you want to force a re-migration, delete that file and the relevant `progress_*.db`.

---

## Files

| File | Description |
|------|-------------|
| `nas_migrate_gui.py` | Main app — Python/tkinter GUI |
| `nas_migrate.sh` | Original bash CLI version (no GUI, no rclone) |
| `Shinigami Eyes.app` | macOS app bundle (lives on Desktop) |
