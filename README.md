# Shinigami Eyes

### Multi-Drive File Migration System
*developed by r41n403*

[![Build Executables](https://github.com/r41n403/shinigami-eyes/actions/workflows/build-executables.yml/badge.svg)](https://github.com/r41n403/shinigami-eyes/actions/workflows/build-executables.yml)
[![Latest Release](https://img.shields.io/github/v/release/r41n403/shinigami-eyes?include_prereleases&label=release)](https://github.com/r41n403/shinigami-eyes/releases/latest)
![Windows](https://img.shields.io/badge/Windows-10%20%2F%2011-0078D6?logo=windows11&logoColor=white)
![macOS](https://img.shields.io/badge/macOS-supported-000000?logo=apple&logoColor=white)
![Python](https://img.shields.io/badge/Python-3.9%2B-3776AB?logo=python&logoColor=white)
![rclone](https://img.shields.io/badge/rclone-Google%20Drive%20%7C%20B2-3B3B3B?logo=rclone&logoColor=white)

A desktop app for migrating files from flash drives and external storage to a local folder, Google Drive, or Backblaze B2 — **on Mac or Windows, from one shared codebase.** Runs up to 54 drives in parallel, deduplicates by MD5 hash across all drives *and* machines, resumes safely after interruptions, and uploads to cloud destinations in batches via rclone.

---

## Supported platforms

| Platform | Status | Notes |
|---|---|---|
| **Windows 10 / 11** | ✅ Supported | Drive metadata via built-in PowerShell cmdlets, no admin rights needed |
| **macOS** | ✅ Supported | Drive metadata via `diskutil`/`system_profiler`, Time Machine snapshot handling |
| **Linux** | ⚠️ Untested | Core logic is platform-guarded and should run, but drive metadata falls back to basics |

Both the Windows `.exe` and the macOS `.app` are built automatically from the **same `nas_migrate_gui.py`** by the CI workflow above — see [Download prebuilt builds](#download-prebuilt-builds).

---

## Features

- **Multi-drive parallel processing** — Connect multiple drives at once. Each drive gets its own worker thread. A configurable concurrency limit (default: 3) controls how many run simultaneously.
- **Hot-swap** — Add new drives mid-run. They start automatically without interrupting drives already in progress.
- **Deduplication** — MD5 hash computed during the copy pass (single read from source). Duplicates are skipped regardless of filename. The hash is registered in memory the moment a file is staged — other drives catch it as a dupe immediately, before the upload even confirms.
- **Multi-machine dedup sync (B2)** — Run the app on a Mac and a Windows machine at the same time, both migrating to the same B2 bucket. Each machine pushes its hash registry to the bucket after every confirmed batch and pulls the other machine's registry on startup, so neither re-uploads what the other already has.
- **Collision-safe naming** — Files are prefixed with the volume name (spaces removed) before staging. `photo.jpg` from two different drives becomes `DriveA_photo.jpg` and `DriveB_photo.jpg`. A per-drive name registry tracks every filename used across all batches in a run, so the same name can never appear twice in B2 regardless of how many batches flush.
- **Windows-safe filenames** — Source drives (exFAT/HFS+/APFS) can contain filenames or volume labels with characters that are illegal on NTFS (`: * ? " < > |`). These are automatically sanitized before staging on Windows so a migration never crashes partway through on a bad filename.
- **Resume** — If a run is interrupted, restart and choose Resume. Already-confirmed files are skipped instantly. The hash registry is always preserved across sessions.
- **Write integrity check** — After each file is copied to staging, the staged file size is verified against bytes read from source. Silent write failures are caught and logged as errors immediately.
- **Backblaze B2 via rclone** — Credentials entered in-app (no pre-configured rclone remote required). Uploads use `--checksum` so files already in B2 with a matching MD5 are never re-uploaded. Windows can auto-install rclone via `winget` with one click if it's missing.
- **Google Drive via rclone** — Uploads via a configured rclone remote in batches.
- **Automatic rclone retry** — Failed batches are retried up to 3 times with 30s/60s backoff. Staging dirs are preserved on failure so files can be retried from source on next run.
- **Safe abort** — Pressing ABORT kills the in-flight rclone process immediately. Unconfirmed files are not marked done and will be retried from source on next run.
- **Staged data counter** — A live indicator shows how many GB are currently staged locally and waiting to upload, updated every 5 seconds.
- **Push notifications via ntfy** — Optional ntfy.sh topic for start, completion, first error, and drive-disconnect events. Topic is saved between sessions.
- **Junk filter** — Skips macOS/Windows system files, tiny images (< 10 KB), `.app` bundles, executables and installers, software documentation, temp files, and build artifacts. Only recognized document/photo types are ever migrated — everything else is explicitly skipped, never silently miscategorized.
- **Orphan cleanup** — Leftover staging folders from crashed runs are removed on startup.

---

## Download prebuilt builds

Every push to `main` builds fresh Windows and macOS executables via GitHub Actions from the same source — see the [Actions tab](https://github.com/r41n403/shinigami-eyes/actions/workflows/build-executables.yml) for the latest run, or the [Releases page](https://github.com/r41n403/shinigami-eyes/releases) for tagged versions (`v*.*.*`) with both builds attached.

Neither build is code-signed, so first launch will trigger a one-time warning:
- **Windows:** SmartScreen — click "More info" → "Run anyway"
- **macOS:** Gatekeeper — right-click the app → "Open" (only needed the first time)

---

## Requirements

| | Windows | macOS |
|---|---|---|
| OS | Windows 10 or later | macOS (any recent version) |
| Python | [python.org](https://www.python.org/downloads/) installer (tkinter included by default) | `brew install python-tk` or `brew install python` |
| rclone (for Google Drive / B2) | Auto-installable in-app via `winget`, or [rclone.org/downloads](https://rclone.org/downloads/) | `brew install rclone` |

---

## Setup from source

### Windows
```powershell
python nas_migrate_gui.py
```
Or build a standalone `.exe`:
```powershell
pip install pyinstaller
pyinstaller --onefile --windowed --name "Shinigami Eyes" nas_migrate_gui.py
```
The executable lands in `dist\Shinigami Eyes.exe`.

### macOS
```bash
python3 nas_migrate_gui.py
```
Or build a standalone `.app`:
```bash
pip3 install pyinstaller
pyinstaller --windowed --name "Shinigami Eyes" nas_migrate_gui.py
```
The bundle lands in `dist/Shinigami Eyes.app`.

### Configuring rclone (Google Drive)

```bash
rclone config
```
- Choose `n` → new remote
- Name it (e.g. `gdrive`)
- Type: `drive`
- Leave client ID and secret blank, scope `1` (full access)
- Follow the browser auth flow

Enter that remote name in the app's **Remote name** field.

**For Backblaze B2**, no rclone remote needed — enter Key ID, App Key, and bucket name directly in the app.

---

## Usage

1. **Add drives** — Click **Add Drives** and select one or more volumes (drive letters on Windows, `/Volumes` entries on Mac). Drive metadata (size, serial, bus type) is shown per row.
2. **Destination** — Choose a mode:
   - **Local folder** — pick an output directory
   - **Google Drive** — select your rclone remote and set a subfolder name
   - **Backblaze B2** — enter Key ID, App Key, bucket name, and optional subfolder
3. **Max parallel** — Number of drives to process simultaneously (default: 3).
4. **ntfy topic** — Optional. Enter an ntfy.sh topic for push notifications. Saved between sessions.
5. **Execute** — Workers start. Each drive row shows live stats: copied, dupes, skipped, errors, and bytes.
6. **Resume** — If a previous run exists for the same source + destination pair, the app asks whether to resume or start fresh.
7. **Hot-add** — Click **Add Drives** at any time during a run to add more drives.
8. **Abort** — Stops all workers and kills the in-flight rclone upload immediately.

---

## Multi-machine setup (B2)

Running Shinigami Eyes on a Mac and a Windows machine at the same time, both uploading to the same B2 bucket:

1. Enter the **same bucket, Key ID, and App Key** in the B2 panel on both machines.
2. Each machine gets a stable, random machine ID (saved in `config.json`) and pushes its hash database to `<bucket>/.shinigami_eyes/hashes_<machine_id>.db` after every confirmed upload batch.
3. On startup, each machine pulls every *other* machine's hash database and merges it in — both in memory and into its own local SQLite copy — so a file one machine already uploaded is recognized and skipped by the other.

This is eventually-consistent, not real-time: a machine only pulls at the *start* of a run, not continuously during one. If both machines happen to stage the same file before either has pulled the other's hashes, rclone's `--checksum` flag still catches it as already present in B2 and skips the redundant upload — nothing gets double-stored, you just don't get the instant in-memory skip mid-run.

---

## Output Structure

```
Destination/
├── Documents/    # pdf, docx, xlsx, pptx, txt, zip, mp4, mp3, psd, etc.
└── Photos/       # jpg, png, heic, gif, bmp, tiff, webp, raw, dng, cr2, svg, etc.
```

Files are prefixed with the source volume name (spaces stripped, illegal characters replaced with `_` on Windows):

```
Photos/
├── MyDrive_photo.jpg
├── ClientDrive_photo.jpg       ← same filename, different drive, no collision
├── MyDrive_IMG_0001.jpg
├── MyDrive_IMG_0001_1.jpg      ← same name, same drive, different folder
```

Files that don't match a recognized document or photo extension (executables, installers, system files, etc.) are skipped entirely rather than being copied.

---

## Cloud Batch Behavior

Files are staged locally (`%TEMP%\se_stage_*\` on Windows, `/tmp/se_stage_*/` on Mac) in up to 10 GB batches. When a batch fills, it's handed to the upload queue and the worker immediately starts filling the next batch — reads and uploads run in parallel.

rclone uploads with `--checksum`, so files already in B2/Drive with a matching MD5 are skipped rather than re-uploaded. If rclone fails, the batch retries up to 3 times with backoff. If all retries fail, the staging folder is preserved and the files will be retried from source on next resume.

Files are only confirmed (added to SQLite and the progress file) after rclone exits 0. Their hashes are added to the in-memory dedup set the moment they're staged, so other drives — and other machines, once synced — skip them immediately.

---

## State Files

All state is stored locally — never on the cloud destination.

| File | Location | Purpose |
|------|----------|---------|
| `hashes.db` | `~/.shinigami_eyes/` (`%USERPROFILE%\.shinigami_eyes\` on Windows) | SQLite — persistent MD5 hash registry across all runs, drives, and synced machines |
| `progress_<hash>.db` | same | Per source+destination record of confirmed source paths (enables resume) |
| `b2_config.json` | same | Saved B2 credentials (Key ID, App Key, bucket, subfolder) |
| `config.json` | same | App settings (ntfy topic, machine ID for multi-machine sync) |

To wipe all dedup history and resume state:
```bash
rm -rf ~/.shinigami_eyes/          # macOS
```
```powershell
Remove-Item -Recurse -Force "$env:USERPROFILE\.shinigami_eyes\"   # Windows
```

To clear resume state for one drive+destination pair only, delete its `progress_*.db` file.

---

## Troubleshooting

**B2 auth fails**
```bash
rclone lsd :b2: --b2-account=<key_id> --b2-key=<app_key>
```
The key must have read/write access to the specified bucket.

**Multi-machine sync not finding the other machine's hashes**
Confirm both machines are using the *exact same bucket name*. The app logs `Found N hash DB(s) in B2` on startup when B2 mode is active — if that shows 0 from other machines, verify the other machine has actually completed at least one confirmed batch (the push happens after batch confirmation, not before).

**Drive shows "aborted" after a run was stopped**
Expected — add the drive again and Execute. It will resume from confirmed files.

**Leftover staging folders filling disk**
Cleaned on startup automatically. To remove manually:
```bash
rm -rf /tmp/se_stage_*/                                  # macOS
Remove-Item -Recurse -Force "$env:TEMP\se_stage_*"        # Windows (PowerShell)
```

**Too many false duplicates**
The hash registry persists forever by design. To force a file to be re-copied, delete `hashes.db` (affects all drives/machines) or the relevant `progress_*.db` (affects one source+destination pair only).

**macOS: "asks for permission to access drives on first launch"**
Accept all permission prompts. The app waits up to ~12 seconds for a volume to remount after a TCC permission dialog before concluding it was disconnected.

**Windows: drive metadata shows "?" for manufacturer/serial**
Pulled via PowerShell's `Get-PhysicalDisk`/`Get-Disk`, which requires the drive to be exposed as a normal physical disk — some USB card readers and RAID enclosures don't report this. Doesn't affect migration, only the informational display.

**Windows: SmartScreen / macOS: Gatekeeper warnings**
Both prebuilt binaries are unsigned. Windows: "More info" → "Run anyway". macOS: right-click the app → "Open".

---

## CI / Building both platforms

[`.github/workflows/build-executables.yml`](.github/workflows/build-executables.yml) builds both platforms from the single `nas_migrate_gui.py` on every push to `main`:

- **Windows** — `actions/setup-python` + PyInstaller → `Shinigami Eyes.exe`
- **macOS** — Homebrew's `python-tk` (GitHub's hosted macOS Python doesn't include tkinter) + PyInstaller → `Shinigami Eyes.app`

Pushing a version tag (`git tag v2.2.0 && git push --tags`) additionally publishes a GitHub Release with both builds attached as downloadable assets.

---

## Files

| File | Description |
|------|-------------|
| `nas_migrate_gui.py` | Main application — Python/tkinter GUI, runs on both Mac and Windows |
| `.github/workflows/build-executables.yml` | CI: builds Windows `.exe` and macOS `.app` on every push |
| `nas_migrate.sh` | Legacy bash CLI — macOS only, single drive, no B2/multi-machine support |
