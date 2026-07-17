<p align="center">
  <img src="shinigami_eyes_logo.png" alt="Shinigami Eyes" width="480">
</p>

### Multi-Drive File Migration System
*developed by r41n403*

[![Build Executables](https://github.com/r41n403/shinigami-eyes/actions/workflows/build-executables.yml/badge.svg)](https://github.com/r41n403/shinigami-eyes/actions/workflows/build-executables.yml)
[![Latest Release](https://img.shields.io/github/v/release/r41n403/shinigami-eyes?include_prereleases&label=release)](https://github.com/r41n403/shinigami-eyes/releases/latest)
![Windows](https://img.shields.io/badge/Windows-10%20%2F%2011-0078D6?logo=windows11&logoColor=white)
![macOS](https://img.shields.io/badge/macOS-supported-000000?logo=apple&logoColor=white)

Point it at a pile of external drives and it moves everything worth keeping into one place — a local folder, Google Drive, or Backblaze B2. Multiple drives run in parallel, every file is deduplicated by content hash so the same photo on five drives uploads once, and interrupted runs resume where they left off. Runs on Mac and Windows — even both at once against the same bucket, sharing one dedup registry.

---

## Install

Grab the latest from the [Releases page](https://github.com/r41n403/shinigami-eyes/releases/latest):

- **macOS** — download `Shinigami Eyes-macOS.dmg`, open it, drag the app to Applications.
- **Windows** — download `Shinigami Eyes-Windows.exe` and run it.

Both are code-signed and notarized — no security warnings, no dependencies to install. The app checks for updates on launch and offers a one-click download when a new version is out.

Cloud uploads are powered by [rclone](https://rclone.org) — it's built into the Mac app, and on Windows the app offers to install it for you with one click.

---

## Usage

1. **Add Drives** — select one or more connected drives.
2. **Pick a destination** — local folder, Google Drive, or Backblaze B2 (enter Key ID, App Key, and bucket right in the app).
3. **Execute** — each drive shows live progress: copied, dupes, skipped, errors.
4. Add more drives mid-run any time. **Abort** stops safely — nothing is marked done that didn't finish, and the next run resumes automatically.

Everything lands sorted into two folders, with filenames prefixed by the drive they came from:

```
Destination/
├── Documents/    ← pdf, docx, xlsx, txt, zip, video, audio, …
│     ClientDrive_invoice.pdf
└── Photos/       ← jpg, png, heic, and ~90 formats incl. RAW (arw, nef, dng, …)
      MyDrive_IMG_0001.jpg
```

Junk never makes the trip: system files, caches, app installers, tiny thumbnail images, and anything without a recognized document/photo extension are skipped — with a per-drive summary of what was skipped and why.

---

## What makes it safe

- **Content-hash dedup** — files are identified by MD5, not filename. Already-migrated files are skipped instantly, across every drive, run, and machine.
- **Nothing is confirmed until it's really uploaded** — a file only counts as done after rclone verifies it in the cloud (checksum-matched). Abort or crash mid-run and it's retried next time.
- **Write integrity check** — every staged copy is size-verified against the source.
- **Failed uploads retry automatically** — 3 attempts with backoff; still-failed files retry from source on the next run.

## Two machines, one bucket

Run the app on a Mac and a Windows machine at the same time, pointed at the same B2 bucket with the same credentials. Each machine publishes its hash registry to the bucket and pulls the other's on startup, so neither re-uploads what the other already handled. (Sync happens at run start — mid-run overlaps are still caught by rclone's checksum check, so nothing gets double-stored.)

---

## Handy to know

- **State lives in `~/.shinigami_eyes/`** (`%USERPROFILE%\.shinigami_eyes\` on Windows). Delete the folder to reset all dedup/resume history.
- **Optional push notifications** — enter an [ntfy.sh](https://ntfy.sh) topic to get notified on completion, errors, or drive disconnects.
- **macOS permission prompts** — accept them on first launch; the app waits out the brief volume remount they cause.
- **Time Machine drives** (macOS) — APFS snapshots are detected and mounted automatically so backup contents get migrated too.

---

## Running from source

```bash
# macOS (needs brew install python-tk rclone)
python3 nas_migrate_gui.py

# Windows (python.org Python includes tkinter)
python nas_migrate_gui.py
```

Everything is one file: `nas_migrate_gui.py`. Prebuilt executables for both platforms are compiled from it automatically by [CI](.github/workflows/build-executables.yml) — publishing a GitHub Release builds, signs, notarizes, and attaches both installers. The workflow itself is covered by unit tests (`python3 -m unittest discover -s tests`).
