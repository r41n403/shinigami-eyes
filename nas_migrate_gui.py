#!/usr/bin/env python3
"""
Shinigami Eyes v2 — Multi-Drive NAS Migration Tool
Parallel, resumable migration to local folder, Google Drive, or Backblaze B2.

Architecture:
  • One DriveWorker thread per source drive (configurable concurrency limit)
  • Shared HashRegistry: all hashes in a Python set (O(1) lockless reads),
    batched SQLite writes in background — no Redis, no setup, scales to 50M+ files
  • UploadCoordinator: serialises rclone batches so cloud rate limits aren't hit
  • Workers don't block on uploads — disk-space back-pressure slows them if needed
  • Hot-swap: OSError from a removed drive is caught per-file; others keep running
"""
from __future__ import annotations
import os, sys, shutil, hashlib, threading, tempfile, time
import json, plistlib, subprocess, queue, sqlite3, re, glob, platform, string, uuid
import urllib.request, urllib.parse
from pathlib import Path
from datetime import datetime
from dataclasses import dataclass, field
from typing import Callable, Optional
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

# ══════════════════════════════════════════════════════════════════════════════
# PLATFORM
# ══════════════════════════════════════════════════════════════════════════════

IS_MAC     = platform.system() == 'Darwin'
IS_WINDOWS = platform.system() == 'Windows'
IS_LINUX   = platform.system() == 'Linux'

# Menlo is macOS-only — fall back to what Windows/Linux ship with.
MONO_FONT = 'Menlo' if IS_MAC else ('Consolas' if IS_WINDOWS else 'Monospace')

RCLONE_INSTALL_HINT = (
    'brew install rclone' if IS_MAC else
    'download from rclone.org/downloads, unzip, and add it to PATH' if IS_WINDOWS else
    'install rclone via your package manager'
)

# ══════════════════════════════════════════════════════════════════════════════
# CONSTANTS
# ══════════════════════════════════════════════════════════════════════════════

VERSION            = '3.0.0'
BATCH_LIMIT_GB     = 10
BATCH_LIMIT_BYTES  = BATCH_LIMIT_GB * 1024 ** 3
MAX_DISK_USE_PCT   = 0.90           # pause staging when disk >90% full
MIN_FREE_BYTES     = 5 * 1024 ** 3  # always keep 5 GB free
DEFAULT_WORKERS    = 3
CHUNK              = 1_048_576      # 1 MiB read/hash chunk — better for USB 3 / Thunderbolt
DB_FLUSH_N         = 200            # flush SQLite after this many new hashes
DB_FLUSH_SECS      = 30

STATE_DIR      = Path.home() / '.shinigami_eyes'
HASH_DB_FILE   = STATE_DIR / 'hashes.db'
B2_CONFIG_FILE  = STATE_DIR / 'b2_config.json'
APP_CONFIG_FILE = STATE_DIR / 'config.json'


def resource_path(filename: str) -> str:
    """Resolve a bundled asset (e.g. the logo PNG) whether running from
    source or from a PyInstaller-frozen executable. PyInstaller unpacks
    --add-data files next to the script inside a temp dir exposed as
    sys._MEIPASS at runtime; when running from source, they just sit next
    to this .py file."""
    base = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parent))
    return str(base / filename)


LOGO_FILE = resource_path('shinigami_eyes_logo.png')

# ── Colours (Matrix theme) ────────────────────────────────────────────────────
BG      = '#000000'
FG      = '#b740a8'
SURFACE = '#2c2a26'
BORDER  = '#1a4d1a'
GREEN   = '#ffc23c'
BTN_BG  = '#00ff41'  # kept for the two green action-button backgrounds (not text)
YELLOW  = '#ffe100'
RED     = '#ac9ae5'
MUTED   = '#2d7a2d'
TEAL    = '#00e5cc'
WHITE   = '#ffffff'

# ══════════════════════════════════════════════════════════════════════════════
# JUNK-FILE FILTER
# ══════════════════════════════════════════════════════════════════════════════

MIN_IMAGE_BYTES = 5 * 1024    # skip image files under 5 KB (true icons/thumbnails only)

SKIP_NAMES = {
    '.ds_store', 'thumbs.db', 'desktop.ini', '.localized', 'autorun.inf',
    '._.ds_store', 'icon\r', '.trashes', '.fseventsd', '.spotlight-v100',
    'hiberfil.sys', 'pagefile.sys', 'swapfile.sys', '.volumeicon.icns',
    'ehthumbs.db', 'ehthumbs_vista.db', '.com.apple.timemachine.donotpresent',
}

SKIP_EXTENSIONS = {
    # macOS / Windows system junk
    '.ds_store', '.tmp', '.temp', '.part', '.crdownload',
    '.bup', '.ifo',            # DVD metadata
    '.pkginfo', '.pbdevelopment',
    # Compiled / build artifacts
    '.pyc', '.pyo', '.pycache',
    '.class',                  # Java bytecode
    '.o', '.obj', '.lo', '.la', '.a', '.so', '.dylib',
    # Logs & databases (system/app internals)
    '.log', '.sst', '.ldb', '.mdb', '.ldf',
    # Config / property files (not user content)
    '.plist', '.ini', '.cfg', '.conf', '.reg',
    # Code & web assets
    '.js', '.jsx', '.ts', '.tsx', '.css', '.scss', '.less',
    '.py', '.rb', '.php', '.sh', '.bat', '.ps1', '.vbs',
    '.c', '.cpp', '.h', '.hpp', '.java', '.swift', '.kt', '.go', '.rs',
    '.vue', '.svelte',
    # Project / IDE files
    '.workspace', '.xcworkspace', '.xcodeproj', '.pbxproj',
    '.sln', '.vcxproj', '.csproj', '.gradle',
    '.lock', '.map', '.sum',
    # Calendar / contacts (not migrating personal data stores)
    '.ics', '.vcf', '.abcdp', '.abcdg',
    # Web browser data
    '.webbookmark', '.webhistory', '.webloc',
    # Lightroom cache (previews, not originals)
    '.lrprev', '.lrmprev', '.lrtemplate',
    # Source control
    '.svn-base',
    # App resources / UI
    '.nib', '.strings', '.helpindex', '.help', '.scpt',
    # Misc system
    '.lockfile', '.ipmeta', '.mcdb', '.wflow',
    '.dat', '.data', '.db', '.ims',
    # Cache / index
    '.cache', '.idx', '.pack', '.manifest', '.babelrc',
}

IMAGE_EXTENSIONS = {
    # JPEG variants
    '.jpg', '.jpeg', '.jpe', '.jfif', '.jif',
    # JPEG 2000
    '.jp2', '.j2k', '.jpf', '.jpx', '.jpm', '.jpg2', '.j2c', '.jpc',
    # Modern formats
    '.png', '.apng',
    '.gif',
    '.webp',
    '.avif', '.avifs',
    '.jxl',                                  # JPEG XL
    '.jxr', '.hdp', '.wdp',                  # JPEG XR / HD Photo
    # HEIF/HEIC (Apple, modern cameras)
    '.heic', '.heif', '.heics', '.heifs', '.hif',
    # Bitmap variants
    '.bmp', '.dib',
    '.tiff', '.tif',
    '.tga', '.icb', '.vda', '.vst',          # Targa
    '.pcx',                                  # PC Paintbrush
    '.ppm', '.pgm', '.pbm', '.pnm', '.pfm', # Netpbm
    '.xbm', '.xpm',                          # X11 bitmap
    '.wbmp',                                 # WAP bitmap
    # Vector / graphics
    '.svg', '.svgz',
    # HDR / scientific / cinema
    '.hdr', '.rgbe',
    '.exr',                                  # OpenEXR
    '.dpx', '.cin',                          # Cineon / DPX (film scanning)
    # Icons
    '.ico', '.icns', '.cur',
    # Legacy formats
    '.pic', '.pict', '.pct',                 # Mac PICT
    '.sgi', '.rgb', '.rgba', '.bw',          # SGI
    '.ilbm', '.iff', '.lbm',                 # Amiga IFF
    '.mng',                                  # Multiple-image Network Graphics
    '.mpo', '.mpf',                          # Multi-Picture Object (3D JPEG / Fuji)
    '.pcd',                                  # Kodak Photo CD
    '.yuv',                                  # Raw YUV data
    # Layered / paint files
    '.xcf',                                  # GIMP
    '.ora',                                  # OpenRaster
    '.kra',                                  # Krita
    '.psb',                                  # Photoshop large document
    # Camera video → Photos (camera-originated, not internet downloads)
    '.mts', '.m2ts', '.m2t',                 # AVCHD (Sony, Panasonic)
    '.mod', '.tod',                          # JVC / Panasonic camcorder
    '.dv', '.dif',                           # DV camcorder
    '.hdv',                                  # HDV camcorder
    '.3gp', '.3g2',                          # Mobile camera video
    '.mxf',                                  # Professional camera container
    '.braw',                                 # Blackmagic RAW
    '.r3d',                                  # RED camera RAW
    '.ari',                                  # ARRI camera RAW
    # RAW camera formats
    '.raw', '.dng',                          # Generic / Adobe DNG
    '.cr2', '.cr3', '.crw', '.ciff',         # Canon
    '.nef', '.nrw',                          # Nikon
    '.arw', '.srf', '.sr2', '.sraw',         # Sony
    '.orf',                                  # Olympus
    '.rw2', '.rw1',                          # Panasonic / Leica variant
    '.raf',                                  # Fujifilm
    '.3fr', '.fff', '.eff',                  # Hasselblad
    '.iiq', '.cap', '.eip',                  # Phase One
    '.mrw', '.mdc',                          # Minolta / Konica-Minolta / Agfa
    '.erf',                                  # Epson
    '.kdc', '.dcr', '.dc2', '.dcs', '.drf', '.k25',  # Kodak
    '.rwl', '.rwz',                          # Leica
    '.pef', '.ptx',                          # Pentax
    '.x3f', '.sd0', '.sd1', '.sdc',         # Sigma
    '.srw',                                  # Samsung
    '.mef', '.mos', '.mfw',                  # Mamiya / Leaf
    '.bay',                                  # Casio
    '.cs1',                                  # CaptureShop RAW
    '.pxn',                                  # Logitech RAW
    '.pxr',                                  # Pixar image
    '.qtk',                                  # Apple QuickTake
    '.sti', '.stk',                          # Sinar
    '.ra2',                                  # Rawker
    # Apple Aperture library files
    '.apmaster', '.apversion',               # Aperture originals and versions
    '.apdetected', '.apalbum', '.apfolder',  # Aperture metadata
}

SKIP_PATH_PARTS = {
    '.git', '__pycache__', 'node_modules', '.cargo',
    '$recycle.bin', 'system volume information', 'recycler',
    '.spotlight-v100', '.fseventsd', '.trashes', 'lost+found',
    '.temporaryitems', '.vol', 'frameworks', 'headers',
}

SOFTWARE_DOC_STEMS = {
    'readme', 'license', 'licence', 'changelog', 'changes', 'history',
    'install', 'contributing', 'authors', 'copying', 'notice',
    'patents', 'version', 'release_notes', 'todo', 'hacking',
}

SOFTWARE_PARENT_DIRS = {
    'usr', 'lib', 'bin', 'share', 'opt', 'applications', 'library',
    'frameworks', 'plugins', 'extensions', 'resources',
}

DOC_EXTENSIONS = {
    '.pdf', '.doc', '.docx', '.xls', '.xlsx', '.ppt', '.pptx',
    '.pages', '.numbers', '.key', '.txt', '.rtf', '.odt', '.ods',
    '.csv', '.json', '.xml', '.html', '.htm', '.md',
    '.zip', '.rar', '.7z', '.tar', '.gz', '.dmg', '.iso',
    '.mp4', '.mov', '.avi', '.mkv', '.m4v',
    '.mp3', '.aac', '.flac', '.wav', '.aiff', '.m4a',
    '.psd', '.ai', '.eps', '.indd', '.sketch', '.fig', '.xd',
    '.prproj', '.aep', '.fcpx', '.ppj', '.drp',
    # Email — Apple Mail and Outlook for Mac
    '.emlx', '.emlxpart',                    # Apple Mail
    '.eml', '.msg',                          # Standard email
    '.olk14message', '.olk14msgsource',      # Outlook for Mac messages
    '.olk14contact', '.olk14folder',         # Outlook contacts / folders
    '.olk14category', '.olk14task',          # Outlook metadata
}


def should_skip(filepath: str, size: int) -> tuple[bool, str]:
    """Return (skip, reason). Cheap string ops only — no Path objects."""
    name    = os.path.basename(filepath)
    name_lo = name.lower()
    dot     = name_lo.rfind('.')
    ext_lo  = name_lo[dot:] if dot != -1 else ''

    if name_lo in SKIP_NAMES:
        return True, 'system file'
    if ext_lo in SKIP_EXTENSIONS:
        return True, 'junk extension'
    if ext_lo in IMAGE_EXTENSIONS and size < MIN_IMAGE_BYTES:
        return True, f'tiny image ({size}B)'

    # Path-parts checks are mostly redundant — os.walk topdown prunes junk dirs
    # and .app bundles already. Only check the immediate parent dir for software docs.
    stem_lo   = name_lo[:dot] if dot != -1 else name_lo
    parent_lo = os.path.basename(os.path.dirname(filepath)).lower()
    if stem_lo in SOFTWARE_DOC_STEMS:
        if parent_lo in SOFTWARE_PARENT_DIRS or any(c.isdigit() for c in parent_lo):
            return True, 'software doc'

    return False, ''


# ══════════════════════════════════════════════════════════════════════════════
# HASH REGISTRY  — thread-safe, SQLite-backed, in-memory lookups
# ══════════════════════════════════════════════════════════════════════════════

class HashRegistry:
    """
    Fast dedup store for MD5 hashes.

    All existing hashes are loaded into a Python set at startup, giving O(1)
    membership tests with no lock required (CPython GIL makes set.__contains__
    safe to call from multiple threads without explicit locking).

    New hashes are added to the in-memory set immediately (with a write lock),
    then flushed to SQLite in batches.

    Legacy text-format hashes.db is auto-migrated to SQLite on first run.
    """

    def __init__(self, db_path: Path):
        self._db_path   = db_path
        self._hashes: set[str] = set()
        self._write_q: list[tuple[str, str]] = []
        self._lock      = threading.Lock()
        self._last_flush = time.monotonic()
        self._conn: Optional[sqlite3.Connection] = None

    def open(self) -> int:
        """Load existing hashes into memory. Returns count loaded."""
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        self._migrate_text_db()
        self._conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
        self._conn.execute('PRAGMA journal_mode=WAL')
        self._conn.execute('PRAGMA synchronous=NORMAL')
        self._conn.execute('''CREATE TABLE IF NOT EXISTS file_hashes (
            hash TEXT NOT NULL,
            source_path TEXT,
            added_at TEXT)''')
        self._conn.execute('CREATE INDEX IF NOT EXISTS idx_hash ON file_hashes(hash)')
        self._conn.commit()
        for (h,) in self._conn.execute('SELECT hash FROM file_hashes'):
            self._hashes.add(h)
        return len(self._hashes)

    def close(self):
        self.flush()
        if self._conn:
            self._conn.close()
            self._conn = None

    def _migrate_text_db(self):
        if not self._db_path.exists():
            return
        with open(self._db_path, 'rb') as f:
            if f.read(16) == b'SQLite format 3\x00':
                return  # already SQLite
        backup = self._db_path.with_suffix('.bak')
        self._db_path.rename(backup)
        conn = sqlite3.connect(str(self._db_path))
        # Speed pragmas — this is a bulk import, durability doesn't matter
        conn.execute('PRAGMA journal_mode=OFF')
        conn.execute('PRAGMA synchronous=OFF')
        conn.execute('PRAGMA cache_size=-131072')   # 128 MB page cache
        conn.execute('''CREATE TABLE file_hashes
                        (hash TEXT NOT NULL, source_path TEXT,
                         added_at TEXT)''')
        # NO index yet — bulk insert first, index after (10-100x faster)
        CHUNK = 50_000
        buf: list[tuple[str, str]] = []
        try:
            with open(backup, 'r', errors='replace') as f:
                for line in f:
                    line = line.strip()
                    if '|' in line:
                        h, *rest = line.split('|')
                        buf.append((h.strip(), rest[0].strip() if rest else ''))
                        if len(buf) >= CHUNK:
                            conn.executemany(
                                'INSERT INTO file_hashes(hash,source_path) VALUES(?,?)', buf)
                            conn.commit()
                            buf.clear()
        except Exception:
            pass
        if buf:
            conn.executemany('INSERT INTO file_hashes(hash,source_path) VALUES(?,?)', buf)
            conn.commit()
        # Build index once after all rows are in — vastly faster than per-row
        conn.execute('CREATE INDEX idx_hash ON file_hashes(hash)')
        conn.commit()
        conn.close()

    # no lock needed for reads — set.__contains__ is atomic under CPython GIL
    def contains(self, h: str) -> bool:
        return h in self._hashes

    def add(self, h: str, source_path: str):
        with self._lock:
            self._hashes.add(h)
            self._write_q.append((h, source_path))
            should = (len(self._write_q) >= DB_FLUSH_N or
                      time.monotonic() - self._last_flush > DB_FLUSH_SECS)
        if should:
            self.flush()

    def flush(self):
        with self._lock:
            if not self._write_q or not self._conn:
                return
            batch = self._write_q[:]
            self._write_q.clear()
            self._last_flush = time.monotonic()
        try:
            self._conn.executemany(
                'INSERT INTO file_hashes(hash,source_path) VALUES(?,?)', batch)
            self._conn.commit()
        except Exception:
            pass

    def checkpoint(self):
        """Fold the WAL sidecar file back into the main .db file. Needed
        before copying hashes.db out-of-band (e.g. to sync it to another
        machine via B2) — under WAL mode, recent commits can sit in a
        separate hashes.db-wal file that a plain file copy would miss."""
        if not self._conn:
            return
        try:
            self._conn.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        except Exception:
            pass


# ══════════════════════════════════════════════════════════════════════════════
# DRIVE INFO
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class DriveInfo:
    path:         str
    label:        str = ''
    total_bytes:  int = 0
    used_bytes:   int = 0
    fs_type:      str = ''
    device:       str = ''
    serial:       str = ''
    manufacturer: str = ''
    bus:          str = ''

    @property
    def label_or_name(self) -> str:
        return self.label or os.path.basename(self.path) or self.path

    @property
    def size_str(self) -> str:
        return _fmt_bytes(self.total_bytes) if self.total_bytes else '?'

    @property
    def used_str(self) -> str:
        return _fmt_bytes(self.used_bytes) if self.used_bytes else '?'

    @property
    def detail_str(self) -> str:
        parts = []
        if self.bus:        parts.append(self.bus)
        if self.manufacturer: parts.append(self.manufacturer)
        if self.serial:     parts.append(f'S/N: {self.serial}')
        if self.fs_type:    parts.append(self.fs_type)
        cap = f'{self.used_str} used / {self.size_str}' if self.total_bytes else ''
        if cap: parts.append(cap)
        return '  |  '.join(parts) if parts else self.path


def get_drive_info(volume_path: str) -> DriveInfo:
    if IS_WINDOWS:
        return _get_drive_info_windows(volume_path)

    info = DriveInfo(path=volume_path, label=os.path.basename(volume_path))
    try:
        r = subprocess.run(['diskutil', 'info', '-plist', volume_path],
                           capture_output=True, timeout=10)
        if r.returncode == 0:
            d = plistlib.loads(r.stdout)
            info.total_bytes = d.get('TotalSize', 0)
            info.fs_type     = d.get('FilesystemName', d.get('Content', ''))
            info.device      = d.get('DeviceIdentifier', '')
            info.label       = d.get('VolumeName', info.label) or info.label
            free = (d.get('FreeSpace') or d.get('VolumeFreeSpace') or
                    d.get('APFSContainerFree') or 0)
            if free and info.total_bytes:
                info.used_bytes = info.total_bytes - free
    except Exception:
        pass

    if info.device:
        m = re.match(r'(disk\d+)', info.device)
        parent = m.group(1) if m else info.device
        try:
            r2 = subprocess.run(
                ['system_profiler', 'SPUSBDataType', 'SPThunderboltDataType', '-json'],
                capture_output=True, timeout=15)
            if r2.returncode == 0:
                _extract_hw_info(r2.stdout, parent, info)
        except Exception:
            pass
    return info


def _get_drive_info_windows(volume_path: str) -> DriveInfo:
    """Windows equivalent of get_drive_info(). Uses shutil.disk_usage() for
    capacity (no WMI/pywin32 dependency needed there) and a single PowerShell
    round-trip (Get-Volume / Get-Partition / Get-PhysicalDisk / Get-Disk,
    all built into Windows) for filesystem, label, manufacturer, serial and
    bus type — no external pip packages required, matching the no-deps goal."""
    drive_letter = volume_path.rstrip('\\')[:1].upper() or 'C'
    info = DriveInfo(path=volume_path, label=f'{drive_letter}:')

    try:
        du = shutil.disk_usage(volume_path)
        info.total_bytes = du.total
        info.used_bytes  = du.used
    except Exception:
        pass

    ps_script = (
        f"$dl = '{drive_letter}'\n"
        "$vol = Get-Volume -DriveLetter $dl -ErrorAction SilentlyContinue\n"
        "$part = Get-Partition -DriveLetter $dl -ErrorAction SilentlyContinue\n"
        "$result = [ordered]@{\n"
        "    FileSystem = [string]$vol.FileSystemType\n"
        "    Label = $vol.FileSystemLabel\n"
        "}\n"
        "if ($part) {\n"
        "    $phys = Get-PhysicalDisk -DeviceNumber $part.DiskNumber -ErrorAction SilentlyContinue\n"
        "    $disk = Get-Disk -Number $part.DiskNumber -ErrorAction SilentlyContinue\n"
        "    $result.Model = $phys.FriendlyName\n"
        "    $result.Manufacturer = $phys.Manufacturer\n"
        "    $result.SerialNumber = $disk.SerialNumber\n"
        "    $result.BusType = [string]$phys.BusType\n"
        "}\n"
        "$result | ConvertTo-Json -Compress"
    )
    try:
        r = subprocess.run(
            ['powershell', '-NoProfile', '-NonInteractive', '-Command', ps_script],
            capture_output=True, text=True, timeout=15)
        if r.returncode == 0 and r.stdout.strip():
            d = json.loads(r.stdout.strip())
            info.fs_type      = d.get('FileSystem') or info.fs_type
            info.label        = d.get('Label') or info.label
            info.device       = f'{drive_letter}:'
            serial            = d.get('SerialNumber')
            info.serial       = str(serial).strip() if serial else ''
            info.manufacturer = (d.get('Manufacturer') or d.get('Model') or '').strip()
            info.bus          = (d.get('BusType') or '').strip()
    except Exception:
        pass
    return info


def _extract_hw_info(profiler_json: bytes, parent_disk: str, info: DriveInfo):
    try:
        data = json.loads(profiler_json)
    except Exception:
        return
    for key, items in data.items():
        bus = ('USB' if 'USB' in key else
               'Thunderbolt' if 'Thunderbolt' in key else key)
        _walk_sp(items, parent_disk, info, bus)


def _walk_sp(nodes, parent_disk: str, info: DriveInfo, bus: str):
    if not isinstance(nodes, list):
        return
    for node in nodes:
        if not isinstance(node, dict):
            continue
        if parent_disk in node.get('bsd_name', ''):
            info.serial       = node.get('serial_num', info.serial)
            info.manufacturer = node.get('manufacturer', info.manufacturer)
            info.bus          = bus
            return
        for media in node.get('Media', []):
            if not isinstance(media, dict):
                continue
            for v in (media.get('volumes', []) or []):
                if isinstance(v, dict) and parent_disk in v.get('bsd_name', ''):
                    info.serial       = node.get('serial_num', info.serial)
                    info.manufacturer = node.get('manufacturer', info.manufacturer)
                    info.bus          = bus
                    return
        _walk_sp(node.get('_items', []), parent_disk, info, bus)


# ══════════════════════════════════════════════════════════════════════════════
# UTILITY FUNCTIONS
# ══════════════════════════════════════════════════════════════════════════════

def _fmt_bytes(n: int) -> str:
    for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
        if n < 1024:
            return f'{n:.1f} {unit}'
        n /= 1024
    return f'{n:.1f} PB'


def _free_bytes() -> int:
    # shutil.disk_usage() is cross-platform (os.statvfs() doesn't exist on
    # Windows — that silently returned 0 free bytes there, which would have
    # permanently blocked _can_stage() from ever staging a cloud upload).
    try:
        return shutil.disk_usage(Path.home()).free
    except Exception:
        return 0


def _can_stage(size: int) -> bool:
    free = _free_bytes()
    return free > max(MIN_FREE_BYTES, size + 512 * 1024 * 1024)


def send_ntfy(topic: str, message: str, title: str = 'Shinigami Eyes'):
    """POST a push notification via ntfy.sh using urllib (stdlib) instead of
    shelling out to curl — curl.exe isn't guaranteed to be present on every
    Windows install, whereas urllib always is. Title is percent-encoded per
    ntfy's own convention since HTTP headers can't carry raw UTF-8/emoji."""
    if not topic:
        return
    try:
        req = urllib.request.Request(
            f'https://ntfy.sh/{topic.strip()}',
            data=message.encode('utf-8'),
            headers={'Title': urllib.parse.quote(title)},
            method='POST',
        )
        urllib.request.urlopen(req, timeout=15)
    except Exception:
        pass


def progress_file_for(output_path: str, source_path: str) -> Path:
    key = hashlib.md5(f'{source_path}|{output_path}'.encode()).hexdigest()[:12]
    return STATE_DIR / f'progress_{key}.db'


def copy_and_hash(src: str, dest_dir: str) -> tuple[str, str, int]:
    """Single-pass: copy + MD5 simultaneously. Returns (hash, tmp_path, bytes).
    Raises ValueError if bytes written don't match bytes read (write integrity check)."""
    try:
        h = hashlib.md5(usedforsecurity=False)   # required on some macOS/Python builds
    except TypeError:
        h = hashlib.md5()                        # older Python without the flag
    tmp = os.path.join(dest_dir, f'.se_{os.getpid()}_{os.urandom(4).hex()}')
    written = 0
    try:
        with open(src, 'rb') as fsrc, open(tmp, 'wb') as fdst:
            for chunk in iter(lambda: fsrc.read(CHUNK), b''):
                h.update(chunk)
                fdst.write(chunk)
                written += len(chunk)
        # Integrity check: staged file must be exactly the right size
        staged = os.path.getsize(tmp)
        if staged != written:
            raise ValueError(f'write integrity fail: read {written}B, staged {staged}B')
        return h.hexdigest(), tmp, written
    except Exception:
        try: os.unlink(tmp)
        except Exception: pass
        raise


def build_dest_path(dest_dir: str, filename: str) -> str:
    """Return a unique destination path, adding _1, _2 … suffix if the name is taken."""
    base, ext = os.path.splitext(filename)
    candidate = os.path.join(dest_dir, filename)
    counter   = 1
    while os.path.exists(candidate):
        candidate = os.path.join(dest_dir, f'{base}_{counter}{ext}')
        counter  += 1
    return candidate


_WINDOWS_INVALID_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def sanitize_for_windows(name: str) -> str:
    """Strip characters that are illegal in Windows filenames/paths. No-op on
    other platforms. Source drives (HFS+/APFS/exFAT) can legally contain
    filenames or volume labels with characters Windows rejects (e.g. ':' was
    valid in classic Mac/HFS filenames) — without this, shutil.move() to a
    Windows destination would raise OSError mid-migration."""
    if not IS_WINDOWS:
        return name
    cleaned = _WINDOWS_INVALID_CHARS.sub('_', name)
    cleaned = cleaned.rstrip(' .')   # trailing dot/space is also invalid on Windows
    return cleaned or '_'


def list_windows_drives() -> list[str]:
    """Return mounted drive letters as ['C:\\\\', 'D:\\\\', ...] (Windows only)."""
    if not IS_WINDOWS:
        return []
    import ctypes
    drives = []
    bitmask = ctypes.windll.kernel32.GetLogicalDrives()
    for i, letter in enumerate(string.ascii_uppercase):
        if bitmask & (1 << i):
            drives.append(f'{letter}:\\')
    return drives


def find_rclone() -> str:
    if IS_WINDOWS:
        candidates = [
            shutil.which('rclone'),   # resolves rclone.exe via PATH
            # winget's "Rclone.Rclone" package links its shim here — this is
            # added to the User PATH at install time, but a process already
            # running when that happens won't see it without a PATH refresh
            # (see _refresh_windows_path_env()), so check it explicitly too.
            os.path.join(os.environ.get('LOCALAPPDATA', ''),
                         'Microsoft', 'WinGet', 'Links', 'rclone.exe'),
            r'C:\rclone\rclone.exe',
            os.path.join(os.environ.get('ProgramFiles', r'C:\Program Files'),
                         'rclone', 'rclone.exe'),
            os.path.join(os.environ.get('LOCALAPPDATA', ''),
                         'rclone', 'rclone.exe'),
            'rclone',
        ]
    else:
        candidates = [resource_path('rclone'),   # bundled inside the .app
                      '/opt/homebrew/bin/rclone', '/usr/local/bin/rclone',
                      shutil.which('rclone'), 'rclone']
    for p in candidates:
        if not p:
            continue
        try:
            if subprocess.run([p, 'version'], capture_output=True, timeout=5).returncode == 0:
                return p
        except Exception:
            pass
    return ''


def _refresh_windows_path_env():
    """Installers like winget update PATH in the registry, but a process
    that's already running inherited its PATH once at startup and won't
    pick up the change on its own. Re-read User + System PATH from the
    registry and merge them into os.environ so shutil.which()/subprocess
    can find a just-installed binary without restarting the app."""
    if not IS_WINDOWS:
        return
    try:
        import winreg
        found = []
        for hive, subkey in [
            (winreg.HKEY_CURRENT_USER, r'Environment'),
            (winreg.HKEY_LOCAL_MACHINE,
             r'SYSTEM\CurrentControlSet\Control\Session Manager\Environment'),
        ]:
            try:
                with winreg.OpenKey(hive, subkey) as key:
                    val, _ = winreg.QueryValueEx(key, 'Path')
                    if val:
                        found.append(val)
            except Exception:
                pass
        if found:
            current = os.environ.get('PATH', '')
            os.environ['PATH'] = ';'.join(found) + (';' + current if current else '')
    except Exception:
        pass


def install_rclone_winget(log_fn=None) -> bool:
    """Install rclone via winget (Windows Package Manager — built into
    Windows 10 1809+ / 11 as part of App Installer). Returns True if rclone
    can be located afterward."""
    log = log_fn or (lambda msg, tag='': None)
    if not IS_WINDOWS:
        return False
    winget = shutil.which('winget')
    if not winget:
        log('  ERR  winget not found — install "App Installer" from the '
            'Microsoft Store, or install rclone manually from rclone.org/downloads.', 'err')
        return False
    try:
        log('  Installing rclone via winget (Rclone.Rclone)…', 'info')
        proc = subprocess.Popen(
            [winget, 'install', '--id', 'Rclone.Rclone', '-e',
             '--silent', '--accept-package-agreements', '--accept-source-agreements'],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
        for line in proc.stdout:
            line = line.rstrip()
            if line:
                log(f'  winget: {line}', 'info')
        proc.wait(timeout=300)
        if proc.returncode != 0:
            # Non-zero can also mean "already installed" or similar —
            # find_rclone() below is the real source of truth either way.
            log(f'  winget exited with code {proc.returncode}', 'warn')
    except subprocess.TimeoutExpired:
        log('  ERR  winget install timed out after 5 minutes', 'err')
        return False
    except Exception as e:
        log(f'  ERR  winget install failed: {e}', 'err')
        return False

    _refresh_windows_path_env()
    found = find_rclone()
    if found:
        log(f'  ✓ rclone found at {found}', 'ok')
    else:
        log('  ✗ rclone still not found after install — you may need to '
            'restart the app for PATH changes to take effect', 'err')
    return bool(found)


def find_gdrive() -> str:
    if IS_WINDOWS:
        # Google Drive for Desktop on Windows mounts "My Drive" either as its
        # own drive letter (streaming mode, e.g. G:\My Drive) or under the
        # user's profile (mirror mode). Only probe mounted letters so we
        # don't stall on empty removable-drive slots.
        for drive in list_windows_drives():
            candidate = Path(drive) / 'My Drive'
            if candidate.is_dir():
                return str(candidate)
        home = Path.home()
        for p in [home / 'Google Drive' / 'My Drive', home / 'My Drive',
                  home / 'Google Drive']:
            if p.exists():
                return str(p)
        return ''

    for base in [Path.home() / 'Library' / 'CloudStorage', Path('/Volumes')]:
        if not base.exists():
            continue
        for p in base.iterdir():
            if 'googledrive' in p.name.lower() or 'google drive' in p.name.lower():
                return str(p)
    return ''


def load_b2_config() -> dict:
    try:
        with open(B2_CONFIG_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def save_b2_config(key_id, app_key, bucket, subfolder):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with open(B2_CONFIG_FILE, 'w') as f:
        json.dump({'key_id': key_id, 'app_key': app_key,
                   'bucket': bucket, 'subfolder': subfolder}, f, indent=2)


def load_app_config() -> dict:
    try:
        with open(APP_CONFIG_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def save_app_config(**kwargs):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    cfg = load_app_config()
    cfg.update(kwargs)
    with open(APP_CONFIG_FILE, 'w') as f:
        json.dump(cfg, f, indent=2)


# ══════════════════════════════════════════════════════════════════════════════
# MULTI-MACHINE HASH SYNC (B2)  — run Shinigami Eyes on two machines at once,
# sharing one dedup registry through the same B2 bucket they already upload to
# ══════════════════════════════════════════════════════════════════════════════

HASH_SYNC_PREFIX = '.shinigami_eyes'   # key prefix in the bucket, separate
                                       # from the Documents/Photos being migrated

def get_machine_id() -> str:
    """Stable short ID for this machine. Namespaces this machine's pushed
    hash DB (hashes_<id>.db) so multiple machines sharing one B2 bucket don't
    stomp on each other's file. Persisted in config.json."""
    cfg = load_app_config()
    if 'machine_id' not in cfg:
        cfg['machine_id'] = uuid.uuid4().hex[:8]
        save_app_config(machine_id=cfg['machine_id'])
    return cfg['machine_id']


def pull_remote_hashes(registry: 'HashRegistry', rclone_path: str,
                       b2_key_id: str, b2_app_key: str, b2_bucket: str,
                       machine_id: str, log_fn=None) -> int:
    """Download every *other* machine's hashes_<id>.db from the shared B2
    bucket and merge those hashes into `registry` — both the in-memory set
    (immediate effect) and this machine's own local SQLite (so the merge
    survives future offline runs too). Returns the count of new hashes merged.

    This only needs to run once per session start, not continuously: even if
    two machines both stage the same file before either has pulled the
    other's hashes, rclone's --checksum flag means the second upload is
    recognized as already present in B2 and skipped — cross-machine dedup at
    the network layer is the safety net under this in-memory one."""
    log = log_fn or (lambda msg, tag='': None)
    if not rclone_path:
        return 0
    remote_dir = f':b2:{b2_bucket}/{HASH_SYNC_PREFIX}/'
    local_tmp  = STATE_DIR / '_remote_hashes'
    local_tmp.mkdir(parents=True, exist_ok=True)
    b2_flags = [f'--b2-account={b2_key_id}', f'--b2-key={b2_app_key}']

    # List what's actually there first. Without this, a failure (wrong
    # bucket, bad credentials, nothing pushed yet) looks identical to "no
    # new hashes to merge" in the log — this makes the difference visible.
    try:
        ls = subprocess.run([rclone_path, 'lsf', remote_dir] + b2_flags,
                            capture_output=True, text=True, timeout=60)
        if ls.returncode != 0:
            log(f'  WARN  could not list {remote_dir}: {ls.stderr.strip()[:300]}', 'warn')
            return 0
        remote_files = [l.strip() for l in ls.stdout.splitlines() if l.strip()]
        others = [f for f in remote_files if f != f'hashes_{machine_id}.db']
        log(f'  Found {len(remote_files)} hash DB(s) in B2 — '
            f'{len(others)} from other machine(s)', 'info')
        if not others:
            return 0
    except Exception as e:
        log(f'  WARN  could not list remote hash DBs: {e}', 'warn')
        return 0

    try:
        cp = subprocess.run(
            [rclone_path, 'copy', remote_dir, str(local_tmp)] + b2_flags + [
             # Order matters: exclude-own must come before include-pattern,
             # since rclone filter rules are first-match-wins.
             '--exclude', f'hashes_{machine_id}.db',
             '--include', 'hashes_*.db'],
            capture_output=True, text=True, timeout=120)
        if cp.returncode != 0:
            log(f'  WARN  rclone copy failed pulling remote hashes: '
                f'{cp.stderr.strip()[:300]}', 'warn')
            return 0
    except Exception as e:
        log(f'  WARN  could not pull remote hash DBs: {e}', 'warn')
        return 0

    merged = 0
    found_files = list(local_tmp.glob('hashes_*.db'))
    if not found_files and others:
        log('  WARN  rclone reported success but no hash DB files landed '
            'locally — check bucket/subfolder match between machines', 'warn')
    for db_file in found_files:
        try:
            conn = sqlite3.connect(str(db_file))
            for (h,) in conn.execute('SELECT hash FROM file_hashes'):
                if not registry.contains(h):
                    registry.add(h, f'remote:{db_file.stem}')
                    merged += 1
            conn.close()
        except Exception as e:
            log(f'  WARN  could not read {db_file.name}: {e}', 'warn')
        finally:
            try: db_file.unlink()
            except Exception: pass
    return merged


def push_local_hashes(rclone_path: str, b2_key_id: str, b2_app_key: str,
                      b2_bucket: str, machine_id: str, log_fn=None):
    """Push this machine's hashes.db up to the shared B2 bucket so other
    machines can pick it up on their next run. Intended to be called from a
    background thread — network I/O, never on the UI thread.

    Uses `rclone copyto`, NOT `rclone copy` — copy always treats the
    destination as a directory and preserves the source's basename, so a
    destination like ".../hashes_<id>.db" would silently become a *folder*
    named "hashes_<id>.db" containing a file called "hashes.db" inside it,
    rather than a flat file at that exact path. copyto does an exact
    source-to-dest file mapping (rename included), which is what a
    machine-specific filename actually needs."""
    log = log_fn or (lambda msg, tag='': None)
    if not rclone_path:
        log('  WARN  cannot push hash DB — rclone not found', 'warn')
        return
    if not HASH_DB_FILE.exists():
        log('  WARN  cannot push hash DB — no local hashes.db yet', 'warn')
        return
    dest = f':b2:{b2_bucket}/{HASH_SYNC_PREFIX}/hashes_{machine_id}.db'
    try:
        r = subprocess.run(
            [rclone_path, 'copyto', str(HASH_DB_FILE), dest,
             f'--b2-account={b2_key_id}', f'--b2-key={b2_app_key}'],
            capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            log(f'  WARN  could not push hash DB to B2: {r.stderr.strip()[:300]}', 'warn')
        else:
            kb = HASH_DB_FILE.stat().st_size / 1024
            log(f'  ↑ Hash DB pushed to B2 ({kb:.0f} KB)', 'info')
    except Exception as e:
        log(f'  WARN  could not push hash DB to B2: {e}', 'warn')


# ══════════════════════════════════════════════════════════════════════════════
# UPDATE CHECK  — GitHub Releases, quiet startup check + one-click download
# ══════════════════════════════════════════════════════════════════════════════

UPDATE_REPO = 'r41n403/shinigami-eyes'


def _parse_version(v: str) -> tuple:
    """'v3.4.1' / '3.4.1' → (3, 4, 1). Non-numeric junk ignored."""
    nums = re.findall(r'\d+', v)
    return tuple(int(n) for n in nums[:3]) if nums else (0,)


def check_latest_release() -> Optional[dict]:
    """Return {'version', 'url', 'asset'} if a newer release with an asset
    for this platform exists, else None. Quiet by design — never raises,
    returns None on any network/API problem."""
    try:
        req = urllib.request.Request(
            f'https://api.github.com/repos/{UPDATE_REPO}/releases/latest',
            headers={'Accept': 'application/vnd.github+json',
                     'User-Agent': f'ShinigamiEyes/{VERSION}'})
        with urllib.request.urlopen(req, timeout=15) as r:
            rel = json.load(r)
        latest = rel.get('tag_name', '')
        if _parse_version(latest) <= _parse_version(VERSION):
            return None
        want = '.dmg' if IS_MAC else ('.exe' if IS_WINDOWS else None)
        if not want:
            return None
        for asset in rel.get('assets', []):
            if asset.get('name', '').lower().endswith(want):
                return {'version': latest,
                        'url':     asset['browser_download_url'],
                        'asset':   asset['name']}
        return None
    except Exception:
        return None


def download_and_open_update(update: dict, on_status=None) -> bool:
    """Download the release asset to ~/Downloads and open it — mounts the
    dmg on macOS (drag to replace), runs the installer exe on Windows.
    Deliberately not a silent self-replace: Gatekeeper/SmartScreen verify
    the signed download and the user stays in control of the swap."""
    status = on_status or (lambda msg: None)
    try:
        dest = Path.home() / 'Downloads' / update['asset']
        status(f'Downloading {update["version"]}…')
        urllib.request.urlretrieve(update['url'], str(dest))
        status('Opening installer…')
        if IS_MAC:
            subprocess.run(['open', str(dest)], timeout=30)
        elif IS_WINDOWS:
            os.startfile(str(dest))          # noqa — Windows-only API
        return True
    except Exception as e:
        status(f'Update failed: {e}')
        return False


def cleanup_orphan_stages():
    for d in glob.glob(os.path.join(tempfile.gettempdir(), 'se_stage_*')):
        try:
            shutil.rmtree(d)
        except Exception:
            pass


# ══════════════════════════════════════════════════════════════════════════════
# UPLOAD COORDINATOR  — serialises rclone batches from all DriveWorkers
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class UploadJob:
    stage_dir:     str
    pending:       list         # [(src_path, md5), ...]
    progress_path: Path
    drive_label:   str
    batch_num:     int
    done_event:    threading.Event = field(default_factory=threading.Event)
    success:       bool = False
    # destination (filled at enqueue time)
    use_b2:        bool = False
    b2_key_id:     str  = ''
    b2_app_key:    str  = ''
    b2_bucket:     str  = ''
    use_rclone:    bool = False
    rclone_path:   str  = ''
    rclone_remote: str  = ''
    gd_subfolder:  str  = ''


class UploadCoordinator:
    """
    One rclone subprocess at a time.
    Workers enqueue UploadJobs and get back a threading.Event to optionally wait on.
    """

    def __init__(self, log_fn: Callable, registry: HashRegistry):
        self._q      = queue.Queue()
        self._log    = log_fn
        self._reg    = registry
        self._thread = threading.Thread(target=self._loop, daemon=True, name='uploader')
        self._proc: Optional[subprocess.Popen] = None   # current rclone process
        self._proc_lock = threading.Lock()

    def start(self): self._thread.start()

    def stop(self):
        """Kill any in-flight rclone process, then drain the coordinator thread."""
        with self._proc_lock:
            if self._proc and self._proc.poll() is None:
                self._log('  [uploader] Killing in-flight rclone on abort…', 'warn')
                self._proc.kill()
        self._q.put(None)
        self._thread.join(timeout=10)

    def enqueue(self, job: UploadJob) -> threading.Event:
        """Non-blocking. Returns an Event set when the job finishes."""
        self._q.put(job)
        return job.done_event

    def _loop(self):
        while True:
            job = self._q.get()
            if job is None:
                break
            try:
                self._run(job)
            except Exception as e:
                self._log(f'  [uploader] FATAL: {e}', 'err')
                job.done_event.set()

    def _run(self, job: UploadJob):
        if not job.use_b2 and not job.use_rclone:
            # Local mode — files already in place, just confirm
            self._confirm(job)
            return
        if job.use_b2:
            dest = f':b2:{job.b2_bucket}/{job.gd_subfolder}'
            extra = [f'--b2-account={job.b2_key_id}', f'--b2-key={job.b2_app_key}',
                     '--b2-chunk-size=96M']
        else:
            dest  = f'{job.rclone_remote}:{job.gd_subfolder}'
            extra = ['--drive-chunk-size=128M']

        self._log(f'\n  ── [{job.drive_label}] Batch #{job.batch_num}'
                  f' ({len(job.pending)} files) → {dest}', 'gd')
        cmd = [
            job.rclone_path, 'copy', job.stage_dir, dest,
            '--no-traverse', '--transfers=8', '--checkers=16',
            '--buffer-size=64M', '-v', '--stats=10s', '--stats-one-line',
            '--checksum',                   # skip files already in B2 with same MD5
            '--retries=5',                  # retry full transfer on error
            '--low-level-retries=10',       # retry individual HTTP ops
        ] + extra

        max_attempts = 3
        for attempt in range(1, max_attempts + 1):
            try:
                proc = subprocess.Popen(
                    cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, bufsize=1)
                with self._proc_lock:
                    self._proc = proc
                for line in proc.stdout:
                    line = line.rstrip()
                    if line: self._log(f'  →  {line}', 'gd')
                proc.wait()
            except Exception as e:
                self._log(f'  ERR  rclone crashed: {e}', 'err')
                job.done_event.set()
                return

            if proc.returncode == 0:
                shutil.rmtree(job.stage_dir, ignore_errors=True)
                self._confirm(job)
                return

            self._log(f'  ERR  [{job.drive_label}] rclone exit {proc.returncode}'
                      f' (attempt {attempt}/{max_attempts})', 'err')
            if attempt < max_attempts:
                time.sleep(30 * attempt)   # back off 30s, 60s before retrying

        self._log(f'  ERR  [{job.drive_label}] batch #{job.batch_num} failed after'
                  f' {max_attempts} attempts — files safe in {job.stage_dir}', 'err')
        job.done_event.set()

    def _confirm(self, job: UploadJob):
        try:
            with open(job.progress_path, 'a') as pf:
                for src_path, h in job.pending:
                    self._reg.add(h, src_path)
                    pf.write(src_path + '\n')
            job.success = True
            self._log(f'  ✓  [{job.drive_label}] batch #{job.batch_num} confirmed'
                      f' ({len(job.pending)} files)', 'ok')
            if job.use_b2:
                # Push the updated hash DB to B2 so another machine running
                # concurrently picks up these hashes on its next run. Async —
                # never blocks the upload pipeline.
                threading.Thread(target=self._push_hashes_async, args=(job,),
                                 daemon=True).start()
        except Exception as e:
            self._log(f'  ERR  confirm: {e}', 'err')
        finally:
            job.done_event.set()

    def _push_hashes_async(self, job: UploadJob):
        self._reg.flush()
        self._reg.checkpoint()
        push_local_hashes(job.rclone_path, job.b2_key_id, job.b2_app_key,
                          job.b2_bucket, get_machine_id(), log_fn=self._log)


# ══════════════════════════════════════════════════════════════════════════════
# DRIVE WORKER  — one thread per source drive
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class DriveStats:
    label:           str
    copied:          int = 0
    skipped_dupe:    int = 0
    skipped_sys:     int = 0
    skipped_resume:  int = 0
    errors:          int = 0
    batches:         int = 0
    bytes_copied:    int = 0
    status:          str = 'queued'   # queued | running | uploading | done | error
    fatal:           str = ''


class DriveWorker:
    def __init__(
        self, *,
        source_path: str, output_path: str, info: DriveInfo,
        registry: HashRegistry, coordinator: UploadCoordinator,
        use_gdrive=False, use_rclone=False, use_b2=False,
        rclone_path='', rclone_remote='', gd_subfolder='',
        b2_key_id='', b2_app_key='', b2_bucket='',
        log_fn=print, on_done=None, on_progress=None,
        ntfy_topic='', resume=False,
        running_ref: Callable = lambda: True,
        batch_limit: int = BATCH_LIMIT_BYTES,
    ):
        self.src          = source_path
        self.out          = output_path
        self.info         = info
        self.registry     = registry
        self.coordinator  = coordinator
        self.use_gdrive   = use_gdrive
        self.use_rclone   = use_rclone
        self.use_b2       = use_b2
        self.rclone_path  = rclone_path
        self.rclone_remote = rclone_remote
        self.gd_subfolder = gd_subfolder
        self.b2_key_id    = b2_key_id
        self.b2_app_key   = b2_app_key
        self.b2_bucket    = b2_bucket
        self._log         = log_fn
        self._on_done        = on_done or (lambda s: None)
        self._on_progress_cb = on_progress or (lambda s: None)
        self.ntfy_topic   = ntfy_topic
        self.resume       = resume
        self._running     = running_ref
        self.batch_limit  = batch_limit
        self.stats        = DriveStats(label=info.label_or_name)
        self._active      = False   # True while _run() is executing
        self._last_progress_t = 0.0  # per-drive throttle timestamp

    def is_alive(self): return self._active

    def _on_progress(self, s: DriveStats):
        """Throttle UI callbacks to ~1 Hz per drive."""
        now = time.monotonic()
        if now - self._last_progress_t < 1.0:
            return
        self._last_progress_t = now
        self._on_progress_cb(s)

    def _run(self):
        self._active      = True
        self.stats.status = 'running'
        try:
            self._migrate()
        except Exception as e:
            self.stats.fatal  = str(e)
            self.stats.status = 'error'
            self._log(f'  [{self.info.label_or_name}] FATAL: {e}', 'err')
        finally:
            self._active = False
            if not self.stats.fatal:
                self.stats.status = 'done' if self._running() else 'aborted'
            self._on_done(self.stats)

    def _migrate(self):
        use_cloud     = self.use_rclone or self.use_b2
        label         = self.info.label_or_name
        progress_path = progress_file_for(self.out, self.src)
        s             = self.stats

        # Prefix for staged filenames — volume name with spaces stripped
        vol_prefix = sanitize_for_windows(re.sub(r'\s+', '', self.info.label_or_name)) + '_'

        # Tracks every destination filename used across ALL batches this run.
        # Keyed by category ('Documents'/'Photos') → set of used names.
        # Prevents cross-batch collisions in B2 where build_dest_path can't see
        # filenames that were in a previous (already-uploaded) staging dir.
        _used_names: dict[str, set[str]] = {'Documents': set(), 'Photos': set()}

        def unique_staged_name(category: str, filename: str) -> str:
            filename = sanitize_for_windows(filename)
            used = _used_names[category]
            base, ext = os.path.splitext(filename)
            candidate = filename
            counter = 1
            while candidate in used:
                candidate = f'{base}_{counter}{ext}'
                counter += 1
            used.add(candidate)
            return candidate

        # ── Resume set ────────────────────────────────────────────────────────
        processed: set[str] = set()
        if self.resume and progress_path.exists():
            try:
                with open(progress_path) as f:
                    for line in f:
                        p = line.strip()
                        if p: processed.add(p)
                self._log(f'  [{label}] Resuming — {len(processed):,} already done')
            except Exception:
                pass

        # ── Set up staging / output dirs ──────────────────────────────────────
        if use_cloud:
            stage = self._new_stage(label)
        else:
            stage = None
            os.makedirs(os.path.join(self.out, 'Documents'), exist_ok=True)
            os.makedirs(os.path.join(self.out, 'Photos'),    exist_ok=True)

        pending: list[tuple[str, str]] = []  # (src_path, md5)
        batch_bytes = 0

        def flush():
            nonlocal batch_bytes, stage
            if not pending: return
            s.batches += 1
            n = s.batches
            job_pending = list(pending)
            pending.clear()
            batch_bytes = 0
            old_stage = stage
            # Create new staging immediately so this worker keeps reading
            stage = self._new_stage(label)

            job = UploadJob(
                stage_dir=old_stage['dir'], pending=job_pending,
                progress_path=progress_path, drive_label=label, batch_num=n,
                use_b2=self.use_b2, b2_key_id=self.b2_key_id,
                b2_app_key=self.b2_app_key, b2_bucket=self.b2_bucket,
                use_rclone=self.use_rclone, rclone_path=self.rclone_path,
                rclone_remote=self.rclone_remote, gd_subfolder=self.gd_subfolder,
            )
            self._log(f'  [{label}] ↑ Batch #{n} queued ({len(job_pending):,} files)', 'info')
            self.coordinator.enqueue(job)
            # Don't wait — keep reading. Disk back-pressure handles flow control.

        # ── Time Machine detection & snapshot mounting (macOS only — tmutil/
        #    diskutil don't exist elsewhere, and non-Mac drives won't have
        #    these directories anyway) ────────────────────────────────────────
        walk_roots = [self.src]   # replaced below for APFS TM
        tm_mounts  = []           # snapshot mount paths to unmount when done

        if IS_MAC:
            tm_apfs = os.path.join(self.src, '.timemachine')
            tm_hfs  = os.path.join(self.src, 'Backups.backupdb')
            if os.path.exists(tm_apfs):
                self._log(f'  [{label}] 🕐 APFS Time Machine detected — mounting snapshots…', 'info')
                mounts = self._mount_tm_snapshots(label)
                if mounts:
                    walk_roots = mounts
                    tm_mounts  = mounts
                    self._log(f'  [{label}] ✓ {len(mounts)} snapshot(s) mounted — '
                              f'walking all (dedup handles repeated files)', 'ok')
                else:
                    self._log(f'  [{label}] ⚠️  Could not mount snapshots — '
                              f'check Full Disk Access in System Settings > Privacy', 'warn')
            elif os.path.exists(tm_hfs):
                self._log(f'  [{label}] ℹ️  HFS+ Time Machine (Backups.backupdb) — '
                          f'walking directly, user files will be extracted', 'info')

        # ── Walk source ───────────────────────────────────────────────────────
        self._log(f'  [{label}] Scanning {self.src}  ({self.info.size_str})', 'head')
        send_ntfy(self.ntfy_topic,
                  f'Started: {label}  ({self.info.size_str})\n{self.info.detail_str}',
                  title='Shinigami Eyes — Drive Started')

        skip_reasons: dict[str, int] = {}   # reason → count, for end-of-walk summary
        files_seen = 0

        for walk_root in walk_roots:
            for root, dirs, files in os.walk(walk_root, topdown=True):
                # Prune junk dirs in-place so os.walk doesn't descend into them
                dirs[:] = [d for d in dirs
                           if d.lower() not in SKIP_PATH_PARTS
                           and not d.endswith('.app')]

                if not self._running(): break

                for fname in files:
                    if not self._running(): break
                    filepath = os.path.join(root, fname)
                    files_seen += 1

                    try:
                        fsize = os.path.getsize(filepath)
                    except Exception:
                        s.skipped_sys += 1
                        skip_reasons['unreadable'] = skip_reasons.get('unreadable', 0) + 1
                        continue

                    skip, reason = should_skip(filepath, fsize)
                    if skip:
                        s.skipped_sys += 1
                        skip_reasons[reason] = skip_reasons.get(reason, 0) + 1
                        self._on_progress(s)
                        continue

                    # Skip non-regular files (aliases, symlinks to dirs,
                    # device nodes, named pipes — unreadable as byte streams)
                    if not os.path.isfile(filepath) or os.path.islink(filepath):
                        s.skipped_sys += 1
                        skip_reasons['non-regular file'] = skip_reasons.get('non-regular file', 0) + 1
                        self._on_progress(s)
                        continue

                    if filepath in processed:
                        s.skipped_resume += 1
                        skip_reasons['already done (resume)'] = skip_reasons.get('already done (resume)', 0) + 1
                        self._on_progress(s)
                        continue

                    # Determine dest category — only files matching a known
                    # document or image extension are migrated at all. Without
                    # this explicit skip, anything with an unrecognized
                    # extension (.exe, .dll, .msi, .sys, ...) fell through to
                    # the Photos bucket by default, which is wrong.
                    ext = os.path.splitext(fname)[1].lower()
                    if ext in DOC_EXTENSIONS:
                        category = 'Documents'
                    elif ext in IMAGE_EXTENSIONS:
                        category = 'Photos'
                    else:
                        s.skipped_sys += 1
                        skip_reasons['unrecognized extension'] = skip_reasons.get(
                            'unrecognized extension', 0) + 1
                        self._on_progress(s)
                        continue

                    if stage:
                        dest_dir = stage['docs'] if category == 'Documents' else stage['photos']
                    else:
                        dest_dir = os.path.join(self.out, category)

                    # Disk space back-pressure (cloud mode only)
                    if use_cloud:
                        waited = False
                        while not _can_stage(fsize):
                            if not self._running(): return
                            if not waited:
                                self._log(f'  [{label}] ⏳ Low disk — waiting for upload to free space...', 'warn')
                                waited = True
                            time.sleep(15)

                    # Copy + hash (single read pass)
                    try:
                        h, tmp, written = copy_and_hash(filepath, dest_dir)
                    except OSError as e:
                        # Drive may have been removed — but macOS TCC permission
                        # dialogs can cause a brief remount that makes the path
                        # transiently unavailable. Retry a few times before giving up.
                        s.errors += 1
                        self._log(f'  [{label}] ERR {fname}: {e}', 'err')
                        if s.errors == 1:
                            send_ntfy(self.ntfy_topic, f'Error on {fname}: {e}',
                                      title=f'Shinigami Eyes — Error on {label}')
                        if not os.path.exists(self.src):
                            drive_gone = True
                            for _ in range(6):          # wait up to ~12s for remount
                                time.sleep(2)
                                if os.path.exists(self.src):
                                    drive_gone = False
                                    self._log(f'  [{label}] Drive re-appeared — resuming', 'info')
                                    break
                            if drive_gone:
                                self._log(f'  [{label}] Drive removed — stopping this worker', 'err')
                                return
                        continue
                    except TypeError:
                        # Unreadable file type (alias, special file, HFS+ compressed)
                        s.skipped_sys += 1
                        skip_reasons['unreadable type'] = skip_reasons.get('unreadable type', 0) + 1
                        self._on_progress(s)
                        continue
                    except Exception as e:
                        s.errors += 1
                        self._log(f'  [{label}] ERR {fname}: {e}', 'err')
                        continue

                    # Dedup (O(1) set lookup, no lock)
                    if self.registry.contains(h):
                        s.skipped_dupe += 1
                        skip_reasons['duplicate (hash match)'] = skip_reasons.get('duplicate (hash match)', 0) + 1
                        self._on_progress(s)
                        try: os.unlink(tmp)
                        except Exception: pass
                        continue

                    # Move tmp to final name — unique across ALL batches this run
                    # (category was already determined above)
                    final_fname = unique_staged_name(category, vol_prefix + fname)
                    dest        = os.path.join(dest_dir, final_fname)
                    try:
                        shutil.move(tmp, dest)
                    except Exception as e:
                        s.errors += 1
                        self._log(f'  [{label}] ERR move {fname}: {e}', 'err')
                        continue

                    s.copied      += 1
                    s.bytes_copied += written
                    self._on_progress(s)

                    if use_cloud:
                        # Add to in-memory set immediately so other drive workers
                        # catch this as a dupe right away. SQLite write is deferred
                        # to _confirm() after rclone exits 0 — so on restart,
                        # unconfirmed files are correctly re-processed from source.
                        self.registry._hashes.add(h)
                        pending.append((filepath, h))
                        batch_bytes += written
                        if batch_bytes >= self.batch_limit:
                            flush()
                    else:
                        # Local: confirm immediately
                        self.registry.add(h, filepath)
                        try:
                            with open(progress_path, 'a') as pf:
                                pf.write(filepath + '\n')
                        except Exception:
                            pass

            if not self._running(): break

        # Final flush
        if pending and self._running():
            flush()

        self.registry.flush()
        if stage and os.path.exists(stage['dir']):
            shutil.rmtree(stage['dir'], ignore_errors=True)

        # ── Unmount any TM snapshots we mounted ───────────────────────────────
        for mount_path in tm_mounts:
            try:
                subprocess.run(['diskutil', 'unmount', mount_path],
                               capture_output=True, timeout=30)
                self._log(f'  [{label}] Unmounted snapshot {mount_path}', 'info')
            except Exception as e:
                self._log(f'  [{label}] WARN could not unmount {mount_path}: {e}', 'warn')

        summary = (f'{s.copied:,} copied · {s.skipped_dupe:,} dupes ·'
                   f' {s.skipped_resume:,} resumed · {s.errors:,} errors ·'
                   f' {_fmt_bytes(s.bytes_copied)}')

        # Walk summary — always log skip breakdown so drive behaviour is diagnosable
        if files_seen == 0:
            self._log(f'  [{label}] ⚠️  Walk found 0 files — drive may be empty, '
                      f'unreadable, or data is in APFS snapshots (Time Machine)', 'warn')
        if skip_reasons:
            reasons_str = '  ·  '.join(f'{v:,} {k}' for k, v in
                                       sorted(skip_reasons.items(), key=lambda x: -x[1]))
            self._log(f'  [{label}] ℹ️  {files_seen:,} seen · skipped: {reasons_str}', 'info')

        self._log(f'  [{label}] ✓ DONE — {summary}', 'ok')
        send_ntfy(
            self.ntfy_topic,
            f'✅ {label} complete — safe to disconnect.\n\n{summary}\n\n{self.info.detail_str}',
            title=f'Shinigami Eyes — Disconnect {label}')

    def _mount_tm_snapshots(self, label: str) -> list[str]:
        """Mount all APFS Time Machine snapshots on self.src.
        Returns list of mount paths, empty list on failure."""
        try:
            # List available snapshots
            r = subprocess.run(
                ['tmutil', 'listlocalsnapshotdates', '-d', self.src],
                capture_output=True, text=True, timeout=30)
            dates = [l.strip() for l in r.stdout.splitlines()
                     if re.match(r'\d{4}-\d{2}-\d{2}-\d{6}', l.strip())]
            if not dates:
                self._log(f'  [{label}] No snapshots found on {self.src}', 'warn')
                return []
            self._log(f'  [{label}] Found {len(dates)} snapshot(s) — mounting…', 'info')

            # Mount all snapshots; tmutil prints one line per mount
            r2 = subprocess.run(
                ['tmutil', 'mountlocalsnapshots', self.src],
                capture_output=True, text=True, timeout=120)

            mounts = []
            for line in (r2.stdout + r2.stderr).splitlines():
                # Handles: "Mounted disk image as <path>" or "mounted at: <path>"
                m = (re.search(r'mounted (?:disk image )?as\s+(.+)', line, re.I) or
                     re.search(r'mounted at:\s*(.+)', line, re.I))
                if m:
                    p = m.group(1).strip()
                    if os.path.isdir(p):
                        mounts.append(p)

            if not mounts:
                self._log(f'  [{label}] tmutil output: {r2.stdout.strip()[:300]}', 'warn')
            return mounts
        except Exception as e:
            self._log(f'  [{label}] ERR mounting TM snapshots: {e}', 'err')
            return []

    @staticmethod
    def _new_stage(label: str) -> dict:
        d     = tempfile.mkdtemp(prefix=f'se_stage_{sanitize_for_windows(label)[:8]}_')
        docs  = os.path.join(d, 'Documents')
        photos = os.path.join(d, 'Photos')
        os.makedirs(docs,   exist_ok=True)
        os.makedirs(photos, exist_ok=True)
        return {'dir': d, 'docs': docs, 'photos': photos}


# ══════════════════════════════════════════════════════════════════════════════
# VOLUME PICKER DIALOG
# ══════════════════════════════════════════════════════════════════════════════

class VolumePicker(tk.Toplevel):
    """Lists available volumes for multi-select (/Volumes on macOS,
    drive letters on Windows)."""

    def __init__(self, parent, already_added: set[str]):
        super().__init__(parent)
        self.title('Select Drives to Add')
        self.configure(bg=BG)
        self.resizable(False, False)
        self.grab_set()
        self.selected: list[str] = []

        # Scan volumes (background)
        volumes = []
        try:
            if IS_WINDOWS:
                system_drive = os.environ.get('SystemDrive', 'C:').rstrip('\\').upper()
                for letter in list_windows_drives():
                    if letter.rstrip('\\').upper() == system_drive:
                        continue   # skip the OS drive by default
                    if letter not in already_added:
                        volumes.append(letter)
            else:
                vols_dir = Path('/Volumes')
                skip_names = {'Macintosh HD', 'Preboot', 'Recovery', 'VM', 'Update', 'Data'}
                for v in sorted(vols_dir.iterdir()):
                    if v.is_symlink() or str(v) in already_added:
                        continue
                    if v.name.startswith('.'):   # hidden system mounts (.timemachine, etc.)
                        continue
                    if v.name in skip_names:
                        continue
                    if v.is_dir():
                        volumes.append(str(v))
        except Exception:
            pass

        scan_location = 'mounted drives' if IS_WINDOWS else '/Volumes'
        tk.Label(self, text='Select volumes to migrate:',
                 font=(MONO_FONT, 12, 'bold'), fg=GREEN, bg=BG).pack(padx=20, pady=(16, 8))

        if not volumes:
            tk.Label(self, text=f'No new volumes found under {scan_location}.',
                     font=(MONO_FONT, 11), fg=MUTED, bg=BG).pack(padx=20, pady=8)
        else:
            frame = tk.Frame(self, bg=BG)
            frame.pack(fill='both', expand=True, padx=20)
            self._vars: list[tuple[tk.BooleanVar, str]] = []
            for v in volumes:
                var = tk.BooleanVar(value=False)
                name = os.path.basename(v.rstrip('\\/')) or v
                cb = tk.Checkbutton(
                    frame, text=name, variable=var,
                    font=(MONO_FONT, 11), fg=FG, bg=BG,
                    selectcolor=SURFACE, activebackground=BG,
                    activeforeground=GREEN, anchor='w')
                cb.pack(fill='x', pady=2)
                # Show size hint (shutil.disk_usage works cross-platform)
                try:
                    du = shutil.disk_usage(v)
                    hint = f'  {_fmt_bytes(du.used)} used / {_fmt_bytes(du.total)}'
                except Exception:
                    hint = ''
                if hint:
                    tk.Label(frame, text=hint, font=(MONO_FONT, 9), fg=GREEN, bg=BG,
                             anchor='w').pack(fill='x', padx=(20, 0))
                self._vars.append((var, v))

        btn_row = tk.Frame(self, bg=BG)
        btn_row.pack(pady=16)
        tk.Button(btn_row, text='Add Selected',
                  font=(MONO_FONT, 11, 'bold'),
                  bg=GREEN, fg='#000', activebackground='#d8a533',
                  relief='flat', padx=14, pady=6,
                  command=self._ok).pack(side='left', padx=8)
        tk.Button(btn_row, text='Browse…',
                  font=(MONO_FONT, 11), bg=SURFACE, fg=FG,
                  relief='flat', padx=14, pady=6,
                  command=self._browse).pack(side='left', padx=8)
        tk.Button(btn_row, text='Cancel',
                  font=(MONO_FONT, 11), bg=SURFACE, fg=GREEN,
                  relief='flat', padx=14, pady=6,
                  command=self.destroy).pack(side='left', padx=8)

    def _ok(self):
        self.selected = [v for var, v in getattr(self, '_vars', []) if var.get()]
        self.destroy()

    def _browse(self):
        p = filedialog.askdirectory(title='Select volume / folder')
        if p:
            self.selected = [p]
            self.destroy()


# ══════════════════════════════════════════════════════════════════════════════
# DRIVE ROW WIDGET
# ══════════════════════════════════════════════════════════════════════════════

class DriveRow(tk.Frame):
    """One row in the drive list for a single source drive."""

    STATUS_COLORS = {
        'queued':    WHITE,
        'running':   GREEN,
        'uploading': YELLOW,
        'done':      GREEN,
        'aborted':   YELLOW,
        'error':     RED,
    }
    STATUS_ICONS = {
        'queued':    '○',
        'running':   '●',
        'uploading': '◉',
        'done':      '✕',   # red X = safe to disconnect
        'aborted':   '◐',
        'error':     '✕',
    }

    def __init__(self, parent, info: DriveInfo, on_remove: Callable, **kwargs):
        # Native Tk has no blur, so the "glow" is faked with concentric
        # frames — each a solid ring stepping from a faint outer violet up
        # through a brighter mid tone into the crisp white edge of the
        # actual content box, approximating a soft bloom around the row.
        super().__init__(parent, bg='#1a0620', **kwargs)
        glow_mid = tk.Frame(self, bg='#4a1450')
        glow_mid.pack(fill='both', expand=True, padx=3, pady=3)
        glow_hot = tk.Frame(glow_mid, bg=FG)
        glow_hot.pack(fill='both', expand=True, padx=2, pady=2)
        content = tk.Frame(glow_hot, bg=SURFACE, relief='flat',
                           highlightbackground=WHITE, highlightthickness=1)
        content.pack(fill='both', expand=True, padx=1, pady=1)

        self.info   = info
        self.stats  = DriveStats(label=info.label_or_name)

        # ── Header row ────────────────────────────────────────────────────────
        hdr = tk.Frame(content, bg=SURFACE)
        hdr.pack(fill='x', padx=10, pady=(8, 2))

        self._icon_lbl = tk.Label(hdr, text='○', font=(MONO_FONT, 14, 'bold'),
                                  fg=WHITE, bg=SURFACE, width=2)
        self._icon_lbl.pack(side='left')

        self._title_lbl = tk.Label(hdr,
                                   text=f'{info.label_or_name}  —  {info.path}',
                                   font=(MONO_FONT, 11, 'bold'), fg=FG, bg=SURFACE)
        self._title_lbl.pack(side='left', padx=(4, 0))

        tk.Button(hdr, text='×', font=(MONO_FONT, 13, 'bold'),
                  fg=GREEN, bg=SURFACE, activeforeground=RED,
                  relief='flat', cursor='hand2', padx=4,
                  command=on_remove).pack(side='right')

        # ── Detail row ────────────────────────────────────────────────────────
        self._detail_lbl = tk.Label(content, text=info.detail_str,
                                    font=(MONO_FONT, 9), fg=WHITE, bg=SURFACE,
                                    anchor='w')
        self._detail_lbl.pack(fill='x', padx=28, pady=(0, 2))

        # ── Stats row ─────────────────────────────────────────────────────────
        self._stats_lbl = tk.Label(content, text='Waiting to start…',
                                   font=(MONO_FONT, 9), fg=WHITE, bg=SURFACE,
                                   anchor='w')
        self._stats_lbl.pack(fill='x', padx=28, pady=(0, 8))

    def update_stats(self, stats: DriveStats):
        self.stats = stats
        color = self.STATUS_COLORS.get(stats.status, MUTED)
        icon  = self.STATUS_ICONS.get(stats.status, '○')
        self._icon_lbl.config(text=icon, fg=color)

        if stats.status == 'done':
            msg = (f'✓ DONE — {stats.copied:,} copied · '
                   f'{stats.skipped_dupe:,} dupes · '
                   f'{stats.errors:,} errors · '
                   f'{_fmt_bytes(stats.bytes_copied)}'
                   f'  — SAFE TO DISCONNECT')
            self._stats_lbl.config(text=msg, fg=color)
            self._icon_lbl.config(fg=RED)  # red X for done
        elif stats.status == 'aborted':
            msg = (f'◐ ABORTED — {stats.copied:,} copied · '
                   f'{stats.skipped_dupe:,} dupes · '
                   f'{stats.errors:,} errors · '
                   f'{_fmt_bytes(stats.bytes_copied)}'
                   f'  — resume on next run')
            self._stats_lbl.config(text=msg, fg=YELLOW)
        elif stats.status == 'error':
            self._stats_lbl.config(text=f'ERROR: {stats.fatal}', fg=RED)
        elif stats.status == 'running':
            msg = (f'{stats.copied:,} copied · '
                   f'{stats.skipped_dupe:,} dupes · '
                   f'{stats.skipped_resume:,} skipped · '
                   f'{stats.errors:,} errors · '
                   f'{_fmt_bytes(stats.bytes_copied)}')
            self._stats_lbl.config(text=msg, fg=color)
        else:
            self._stats_lbl.config(text=stats.status.capitalize() + '…', fg=WHITE)


# ══════════════════════════════════════════════════════════════════════════════
# MAIN APPLICATION
# ══════════════════════════════════════════════════════════════════════════════

class MigrationApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(f'Shinigami Eyes  v{VERSION}')
        self.configure(bg=BG)
        self.resizable(True, True)
        self.minsize(700, 800)
        self.geometry('820x1000')

        self._rclone_path = find_rclone()
        self._gd_path     = find_gdrive()
        self._running     = False
        self._run_cfg:    dict = {}
        self._semaphore:  Optional[threading.Semaphore] = None

        # Shared objects (created on first run)
        self._registry:    Optional[HashRegistry]    = None
        self._coordinator: Optional[UploadCoordinator] = None

        # Drive list: list of (DriveRow widget, DriveInfo, DriveWorker | None)
        self._drives: list[tuple[DriveRow, DriveInfo, Optional[DriveWorker]]] = []
        self._drive_lock = threading.Lock()

        self._build_ui()
        self.after(300, cleanup_orphan_stages)
        threading.Thread(target=self._check_updates_async,
                         daemon=True, name='update-check').start()

    # ── Update check ──────────────────────────────────────────────────────────

    def _check_updates_async(self):
        upd = check_latest_release()
        if upd:
            self.after(0, lambda: self._show_update_banner(upd))

    def _show_update_banner(self, upd: dict):
        banner = tk.Frame(self._outer, bg=SURFACE)
        tk.Label(banner,
                 text=f'⬆  Update available: {upd["version"]}  (you have v{VERSION})',
                 font=(MONO_FONT, 10, 'bold'), fg=YELLOW, bg=SURFACE
                 ).pack(side='left', padx=10, pady=6)
        btn = tk.Button(banner, text='Download & Install',
                        font=(MONO_FONT, 10, 'bold'), bg=YELLOW, fg='#000',
                        activebackground='#ccb400', activeforeground='#000',
                        relief='flat', padx=12, pady=3, cursor='hand2')
        btn.config(command=lambda: self._do_update(upd, btn))
        btn.pack(side='right', padx=10, pady=6)
        # Insert above everything else in the top pane
        children = self._outer.winfo_children()
        if children and children[0] is not banner:
            banner.pack(fill='x', pady=(0, 10), before=children[0])
        else:
            banner.pack(fill='x', pady=(0, 10))

    def _do_update(self, upd: dict, btn: tk.Button):
        btn.config(state='disabled', text='Downloading…')

        def set_status(msg):
            self.after(0, lambda: btn.config(text=msg))

        def work():
            if download_and_open_update(upd, on_status=set_status):
                set_status('Installer opened — quit the app and replace it')

        threading.Thread(target=work, daemon=True, name='update-dl').start()

    # ── UI construction ───────────────────────────────────────────────────────

    def _build_ui(self):
        # Vertical split — drag the sash to resize the log area
        paned = tk.PanedWindow(self, orient=tk.VERTICAL, bg=YELLOW,
                               sashwidth=10, sashrelief='groove',
                               showhandle=True, handlesize=24, handlepad=12)
        paned.pack(fill='both', expand=True)

        outer = tk.Frame(paned, bg=BG, padx=24, pady=16)
        paned.add(outer, stretch='always', minsize=400)
        self._outer = outer   # update banner inserts itself at the top of this

        # NB: Frame padx/pady must be single values — tuple (top, bottom)
        # syntax is only valid in pack()/grid(), and the bundled Tk rejects
        # it with "expected screen distance" (crashed the frozen app once).
        log_frame = tk.Frame(paned, bg=BG, padx=24, pady=8)
        paned.add(log_frame, stretch='always', minsize=120)

        # Title — stylised logo image, falling back to plain text if the
        # asset is missing or this Tk build can't load PNGs.
        try:
            self._logo_img = tk.PhotoImage(file=LOGO_FILE)
            target_w = 460   # PhotoImage only supports integer subsample factors
            if self._logo_img.width() > target_w:
                factor = max(1, self._logo_img.width() // target_w)
                self._logo_img = self._logo_img.subsample(factor, factor)
            tk.Label(outer, image=self._logo_img, bg=BG).pack(anchor='w')
        except Exception:
            tk.Label(outer, text='SHINIGAMI EYES',
                     font=(MONO_FONT, 18, 'bold'), fg=WHITE, bg=BG).pack(anchor='w')
        tk.Label(outer, text='multi-drive NAS migration tool',
                 font=(MONO_FONT, 10), fg=FG, bg=BG).pack(anchor='w', pady=(0, 14))

        # ── Drive list ────────────────────────────────────────────────────────
        self._lbl(outer, 'SOURCE DRIVES', color=GREEN).pack(anchor='w')

        drive_container = tk.Frame(outer, bg=BG)
        drive_container.pack(fill='x', pady=(4, 0))

        # Scrollable area for drive rows — starts collapsed, grows to 220 px max
        canvas = tk.Canvas(drive_container, bg=BG, highlightthickness=0, height=0)
        scrollbar = ttk.Scrollbar(drive_container, orient='vertical', command=canvas.yview)
        self._drive_frame = tk.Frame(canvas, bg=BG)

        def _on_drive_frame_resize(e):
            canvas.configure(scrollregion=canvas.bbox('all'))
            canvas.configure(height=min(e.height, 220))

        self._drive_frame.bind('<Configure>', _on_drive_frame_resize)
        canvas.create_window((0, 0), window=self._drive_frame, anchor='nw')
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side='left', fill='both', expand=True)
        scrollbar.pack(side='right', fill='y')
        self._drive_canvas = canvas

        # Global totals bar (updated by workers)
        self._totals_lbl = tk.Label(outer, text='',
                                    font=(MONO_FONT, 10, 'bold'), fg=TEAL, bg=BG,
                                    anchor='w')
        self._totals_lbl.pack(fill='x', pady=(4, 0))

        # Staged-for-upload counter (polled every 5s from staging dirs)
        self._staged_lbl = tk.Label(outer, text='',
                                    font=(MONO_FONT, 9), fg=YELLOW, bg=BG,
                                    anchor='w')
        self._staged_lbl.pack(fill='x')
        self._poll_staged()

        # Add drive button
        add_row = tk.Frame(outer, bg=BG)
        add_row.pack(fill='x', pady=(8, 14))
        tk.Button(add_row, text='＋  Add Drives',
                  font=(MONO_FONT, 11, 'bold'),
                  bg=SURFACE, fg=GREEN,
                  activebackground='#938f87', activeforeground=GREEN,
                  relief='flat', padx=14, pady=6, cursor='hand2',
                  command=self._add_drives).pack(side='left')

        # Max parallel
        tk.Label(add_row, text='Max parallel:',
                 font=(MONO_FONT, 10), fg=GREEN, bg=BG).pack(side='left', padx=(20, 4))
        self.var_workers = tk.IntVar(value=DEFAULT_WORKERS)
        ttk.Spinbox(add_row, from_=1, to=8, width=4,
                    textvariable=self.var_workers,
                    font=(MONO_FONT, 10)).pack(side='left')

        self._stitch_sep(outer).pack(fill='x', pady=8)

        # ── Destination ───────────────────────────────────────────────────────
        self._lbl(outer, 'DESTINATION', color=GREEN).pack(anchor='w')
        self.var_mode = tk.StringVar(value='local')
        mode_row = tk.Frame(outer, bg=BG)
        mode_row.pack(fill='x', pady=(4, 0))
        rb = dict(font=(MONO_FONT, 11), fg=FG, bg=BG, selectcolor=SURFACE,
                  activebackground=BG, activeforeground=GREEN,
                  variable=self.var_mode, command=self._on_mode)
        tk.Radiobutton(mode_row, text='Local Folder', value='local', **rb).pack(side='left')
        gd_state = 'normal' if (self._gd_path or self._rclone_path) else 'disabled'
        self._gdrive_radio = tk.Radiobutton(mode_row, text='Google Drive', value='gdrive',
                                            state=gd_state, **rb)
        self._gdrive_radio.pack(side='left', padx=(20, 0))
        tk.Radiobutton(mode_row, text='Backblaze B2', value='b2', **rb).pack(side='left', padx=(20, 0))

        # Local panel
        self.panel_local = tk.Frame(outer, bg=BG)
        self.panel_local.pack(fill='x', pady=(8, 4))
        self._lbl(self.panel_local, 'Output folder', color=GREEN).pack(anchor='w')
        loc_row = tk.Frame(self.panel_local, bg=BG)
        loc_row.pack(fill='x', pady=(4, 0))
        self.var_output = tk.StringVar()
        self._entry(loc_row, self.var_output).pack(side='left', fill='x', expand=True)
        tk.Button(loc_row, text='Browse', font=(MONO_FONT, 10),
                  bg=SURFACE, fg=FG, relief='flat', padx=8,
                  command=self._browse_output).pack(side='left', padx=(6, 0))

        # GDrive panel
        self.panel_gdrive = tk.Frame(outer, bg=BG)
        if self._gd_path:
            tk.Label(self.panel_gdrive, text=f'Google Drive: {self._gd_path}',
                     font=(MONO_FONT, 10), fg=MUTED, bg=BG).pack(anchor='w', pady=(8, 2))
        self._gdrive_rclone_status = tk.Frame(self.panel_gdrive, bg=BG)
        self._gdrive_rclone_status.pack(fill='x', pady=(6, 0))

        remote_row = tk.Frame(self.panel_gdrive, bg=BG)
        remote_row.pack(fill='x', pady=(6, 0))
        tk.Label(remote_row, text='Remote name:', font=(MONO_FONT, 10), fg=FG, bg=BG).pack(side='left')
        self.var_rclone_remote = tk.StringVar(value='gdrive')
        self._entry(remote_row, self.var_rclone_remote, width=14).pack(side='left', padx=(8, 0))

        gd_sub_row = tk.Frame(self.panel_gdrive, bg=BG)
        gd_sub_row.pack(fill='x', pady=(8, 4))
        tk.Label(gd_sub_row, text='Subfolder:', font=(MONO_FONT, 10), fg=FG, bg=BG).pack(side='left')
        self.var_gd_sub = tk.StringVar(value='NAS Migration')
        self._entry(gd_sub_row, self.var_gd_sub, width=22).pack(side='left', padx=(8, 0))

        # B2 panel
        _b2 = load_b2_config()
        self.panel_b2 = tk.Frame(outer, bg=BG)
        self.var_b2_key_id    = tk.StringVar(value=_b2.get('key_id', ''))
        self.var_b2_app_key   = tk.StringVar(value=_b2.get('app_key', ''))
        self.var_b2_bucket    = tk.StringVar(value=_b2.get('bucket', ''))
        self.var_b2_subfolder = tk.StringVar(value=_b2.get('subfolder', 'NAS Migration'))

        def _b2_row(label, var, mask=False):
            row = tk.Frame(self.panel_b2, bg=BG)
            row.pack(fill='x', pady=(4, 0))
            tk.Label(row, text=f'{label}:', font=(MONO_FONT, 10), fg=FG, bg=BG,
                     width=12, anchor='e').pack(side='left')
            kw = {'show': '●'} if mask else {}
            self._entry(row, var, width=36, **kw).pack(side='left', padx=(8, 0))
        self._b2_rclone_status = tk.Frame(self.panel_b2, bg=BG)
        self._b2_rclone_status.pack(fill='x', pady=(8, 4))
        _b2_row('Key ID',    self.var_b2_key_id)
        _b2_row('App Key',   self.var_b2_app_key, mask=True)
        _b2_row('Bucket',    self.var_b2_bucket)
        _b2_row('Subfolder', self.var_b2_subfolder)
        tk.Label(self.panel_b2,
                 text=f'Files staged in {BATCH_LIMIT_GB} GB batches, uploaded via rclone.',
                 font=(MONO_FONT, 9), fg=GREEN, bg=BG).pack(anchor='w', pady=(8, 4))

        self._refresh_rclone_status()

        self._stitch_sep(outer).pack(fill='x', pady=8)

        # ── ntfy ──────────────────────────────────────────────────────────────
        ntfy_row = tk.Frame(outer, bg=BG)
        ntfy_row.pack(fill='x', pady=(0, 10))
        self._ntfy_row = ntfy_row
        self._lbl(ntfy_row, 'ntfy topic', color=GREEN).pack(side='left')
        tk.Label(ntfy_row, text='(optional)', font=(MONO_FONT, 9), fg=GREEN, bg=BG).pack(side='left', padx=6)
        _cfg = load_app_config()
        self.var_ntfy = tk.StringVar(value=_cfg.get('ntfy_topic', ''))
        self._entry(ntfy_row, self.var_ntfy, width=30).pack(side='left', padx=(10, 0))

        self._stitch_sep(outer).pack(fill='x', pady=4)

        # ── Buttons ───────────────────────────────────────────────────────────
        btn_row = tk.Frame(outer, bg=BG, pady=10)
        btn_row.pack()
        self.btn_start = tk.Button(btn_row, text='▶  EXECUTE',
            font=(MONO_FONT, 13, 'bold'), bg=GREEN, fg='#000',
            activebackground='#d8a533', activeforeground='#000',
            relief='flat', padx=22, pady=9, cursor='hand2',
            command=self._start)
        self.btn_start.pack(side='left', padx=(0, 12))
        self.btn_stop = tk.Button(btn_row, text='■  ABORT',
            font=(MONO_FONT, 13, 'bold'), bg=SURFACE, fg=MUTED,
            activebackground='#330000', activeforeground=RED,
            relief='flat', padx=22, pady=9, cursor='hand2',
            state='disabled', command=self._stop)
        self.btn_stop.pack(side='left')

        # ── Status + log (bottom pane — drag the sash to resize) ─────────────
        self.var_status = tk.StringVar(value='Ready')
        tk.Label(log_frame, textvariable=self.var_status,
                 font=(MONO_FONT, 10), fg='#ffffff', bg=BG, anchor='w').pack(fill='x', pady=(4, 2))

        log_container = tk.Frame(log_frame, bg=BG)
        log_container.pack(fill='both', expand=True)

        sb = ttk.Scrollbar(log_container)
        sb.pack(side='right', fill='y')

        self.log_box = tk.Text(log_container, bg=SURFACE, fg=FG,
                               font=(MONO_FONT, 10), relief='flat',
                               state='disabled', wrap='word',
                               yscrollcommand=sb.set)
        self.log_box.pack(side='left', fill='both', expand=True)
        sb.config(command=self.log_box.yview)

        # Log tags
        for tag, color in [('ok', GREEN), ('err', RED), ('warn', YELLOW),
                            ('gd', TEAL), ('head', TEAL), ('info', FG),
                            ('gdload', WHITE)]:
            self.log_box.tag_config(tag, foreground=color)

        self._on_mode()

    def _lbl(self, parent, text, color=MUTED):
        return tk.Label(parent, text=text, font=(MONO_FONT, 9, 'bold'),
                        fg=color, bg=BG)

    def _entry(self, parent, var, width=40, **kw):
        return tk.Entry(parent, textvariable=var, width=width,
                        bg=SURFACE, fg=FG, insertbackground=GREEN,
                        relief='flat', font=(MONO_FONT, 10), **kw)

    def _stitch_sep(self, parent, height=15, color=WHITE, bg=BG):
        """Horizontal 'stitched scar' divider — a dashed thread run through
        with rectangular cross-stitches, replacing the plain ttk.Separator
        line. Redraws itself to fill whatever width it's packed into."""
        canvas = tk.Canvas(parent, height=height, bg=bg, highlightthickness=0)
        mid = height // 2
        spacing = 14
        half = 3
        rect_w = 1

        def _draw(event=None):
            canvas.delete('all')
            w = canvas.winfo_width()
            if w <= 1:
                return
            canvas.create_line(0, mid, w, mid, fill=color, width=1, dash=(2, 3))
            x = spacing // 2
            while x < w:
                canvas.create_rectangle(x - rect_w, mid - half, x + rect_w, mid + half,
                                        fill=color, outline=color)
                x += spacing

        canvas.bind('<Configure>', _draw)
        return canvas

    def _on_mode(self):
        mode = self.var_mode.get()
        ref  = self._ntfy_row
        self.panel_local.pack_forget()
        self.panel_gdrive.pack_forget()
        self.panel_b2.pack_forget()
        if mode == 'local':
            self.panel_local.pack(fill='x', pady=(8, 4), before=ref)
        elif mode == 'gdrive':
            self.panel_gdrive.pack(fill='x', pady=(8, 4), before=ref)
        elif mode == 'b2':
            self.panel_b2.pack(fill='x', pady=(8, 4), before=ref)

    def _browse_output(self):
        p = filedialog.askdirectory(title='Select output folder')
        if p: self.var_output.set(p)

    # ── rclone status / auto-install ─────────────────────────────────────────

    def _refresh_rclone_status(self):
        """(Re)build the rclone found/not-found row in both the Google Drive
        and B2 panels, and update the Google Drive radio button's enabled
        state. Called at startup and again after an install attempt."""
        for container in (self._gdrive_rclone_status, self._b2_rclone_status):
            for child in container.winfo_children():
                child.destroy()
            if self._rclone_path:
                tk.Label(container, text='✓ rclone found', font=(MONO_FONT, 10),
                         fg=GREEN, bg=BG).pack(side='left')
            else:
                tk.Label(container, text=f'rclone not found — install: {RCLONE_INSTALL_HINT}',
                         font=(MONO_FONT, 10), fg=YELLOW, bg=BG).pack(side='left')
                if IS_WINDOWS:
                    tk.Button(container, text='Install via winget',
                              font=(MONO_FONT, 9, 'bold'), bg=SURFACE, fg=GREEN,
                              activebackground=BORDER, activeforeground=GREEN,
                              relief='flat', padx=8, pady=2, cursor='hand2',
                              command=self._install_rclone).pack(side='left', padx=(10, 0))

        gd_state = 'normal' if (self._gd_path or self._rclone_path) else 'disabled'
        self._gdrive_radio.config(state=gd_state)

    def _install_rclone(self):
        self._log('── Installing rclone via winget…', 'head')
        threading.Thread(target=self._install_rclone_bg, daemon=True).start()

    def _install_rclone_bg(self):
        install_rclone_winget(log_fn=self._log)   # logs its own progress/result
        self._rclone_path = find_rclone()
        self.after(0, self._refresh_rclone_status)

    # ── Drive management ──────────────────────────────────────────────────────

    def _add_drives(self):
        already = {info.path for _, info, _ in self._drives}
        picker = VolumePicker(self, already)
        self.wait_window(picker)
        for path in picker.selected:
            if path and path not in already:
                self._log(f'  Loading info for {os.path.basename(path)}…', 'gdload')
                threading.Thread(target=self._add_drive_bg, args=(path,),
                                 daemon=True).start()

    def _add_drive_bg(self, path: str):
        info = get_drive_info(path)
        self.after(0, lambda: self._add_drive_row(info))

    def _add_drive_row(self, info: DriveInfo):
        row = DriveRow(
            self._drive_frame, info,
            on_remove=lambda: self._remove_drive(info.path))
        row.pack(fill='x', pady=(0, 4))
        with self._drive_lock:
            self._drives.append((row, info, None))
        self._drive_canvas.configure(scrollregion=self._drive_canvas.bbox('all'))
        self._log(f'  Added: {info.label_or_name}  —  {info.detail_str}', 'info')

        # Auto-start worker if a run is already active
        if self._running and hasattr(self, '_run_cfg') and self._run_cfg:
            self._log(f'  [{info.label_or_name}] Hot-adding to active run…', 'gd')
            threading.Thread(
                target=self._launch_worker, args=(row, info, False),
                daemon=True).start()

    def _remove_drive(self, path: str):
        with self._drive_lock:
            for i, (row, info, worker) in enumerate(self._drives):
                if info.path == path:
                    row.pack_forget()
                    row.destroy()
                    self._drives.pop(i)
                    break

    # ── Start / Stop ──────────────────────────────────────────────────────────

    def _start(self):
        if not self._drives:
            messagebox.showwarning('No drives', 'Add at least one source drive first.')
            return

        mode       = self.var_mode.get()
        use_gdrive = (mode == 'gdrive')
        use_b2     = (mode == 'b2')
        use_rclone = False
        rclone_remote = ''
        gd_subfolder  = ''
        output_path   = ''
        b2_key_id = b2_app_key = b2_bucket = ''

        if use_b2:
            if not self._rclone_path:
                if IS_WINDOWS:
                    if messagebox.askyesno(
                        'rclone required',
                        'rclone is required for Backblaze B2 and was not found.\n\n'
                        'Install it now via winget?'):
                        self._install_rclone()
                        messagebox.showinfo(
                            'Installing…',
                            'Installing rclone in the background — watch the log below.\n'
                            'Click Execute again once it finishes.')
                    return
                messagebox.showerror('rclone required',
                    f'Install rclone: {RCLONE_INSTALL_HINT}')
                return
            b2_key_id    = self.var_b2_key_id.get().strip()
            b2_app_key   = self.var_b2_app_key.get().strip()
            b2_bucket    = self.var_b2_bucket.get().strip()
            gd_subfolder = self.var_b2_subfolder.get().strip() or 'NAS Migration'
            if not b2_key_id or not b2_app_key or not b2_bucket:
                messagebox.showwarning('Missing B2 credentials',
                    'Fill in Key ID, App Key, and Bucket name.')
                return
            save_b2_config(b2_key_id, b2_app_key, b2_bucket, gd_subfolder)
            output_path = f'b2:{b2_bucket}/{gd_subfolder}'

        elif use_gdrive:
            gd_subfolder  = self.var_gd_sub.get().strip() or 'NAS Migration'
            rclone_remote = self.var_rclone_remote.get().strip()
            use_rclone    = bool(self._rclone_path and rclone_remote)
            output_path   = (f'rclone:{rclone_remote}/{gd_subfolder}' if use_rclone
                             else os.path.join(self._gd_path or '', gd_subfolder))
        else:
            output_path = self.var_output.get().strip()
            if not output_path:
                messagebox.showwarning('Missing output', 'Choose an output folder.')
                return
            os.makedirs(output_path, exist_ok=True)

        ntfy_topic = self.var_ntfy.get().strip()
        if ntfy_topic:
            save_app_config(ntfy_topic=ntfy_topic)

        # ── Sanity check cloud (runs subprocess — keep on main thread before UI lock) ──
        if use_b2:
            if not self._check_b2(b2_key_id, b2_app_key, b2_bucket):
                return
        elif use_rclone:
            if not self._check_rclone(rclone_remote):
                return

        # ── Resume dialogs must be on main thread (messagebox requirement) ────
        with self._drive_lock:
            entries = list(self._drives)

        resume_map: dict[str, bool] = {}
        for _, info, _ in entries:
            prog = progress_file_for(output_path, info.path)
            resume_map[info.path] = False
            if prog.exists():
                try:
                    with open(prog) as f:
                        done_count = sum(1 for l in f if l.strip())
                except Exception:
                    done_count = 0
                if done_count:
                    ans = messagebox.askquestion(
                        'Resume?',
                        f'{info.label_or_name}: {done_count:,} files already done.\n\n'
                        'YES = resume (skip already-done files)\n'
                        'NO  = start fresh')
                    resume_map[info.path] = (ans == 'yes')
                    if not resume_map[info.path]:
                        try: os.remove(prog)
                        except Exception: pass
                else:
                    self._log(f'  [{info.label_or_name}] No previous session — starting fresh', 'info')
            else:
                self._log(f'  [{info.label_or_name}] No previous session — starting fresh', 'info')

        # ── Lock UI and hand off to background thread ─────────────────────────
        self._running = True
        self.btn_start.config(state='disabled')
        self.btn_stop.config(state='normal')
        self._set_status('> running…')

        threading.Thread(
            target=self._do_start,
            args=(entries, resume_map, output_path,
                  use_gdrive, use_rclone, use_b2,
                  rclone_remote, gd_subfolder,
                  b2_key_id, b2_app_key, b2_bucket,
                  ntfy_topic, self.var_workers.get()),
            daemon=True,
            name='setup',
        ).start()

    def _do_start(self, entries, resume_map, output_path,
                  use_gdrive, use_rclone, use_b2,
                  rclone_remote, gd_subfolder,
                  b2_key_id, b2_app_key, b2_bucket,
                  ntfy_topic, max_workers):
        """Heavy setup: registry, coordinator, workers. Runs in background thread."""

        # Registry
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        if self._registry is None:
            self._log('  Opening hash registry…', 'info')
            self._registry = HashRegistry(HASH_DB_FILE)
            n = self._registry.open()
            self._log(f'  ✓ Hash registry ready — {n:,} existing hashes', 'ok')

        # Pull hashes from any other machine sharing this B2 bucket, so this
        # run doesn't re-upload files another machine already confirmed.
        if use_b2 and self._rclone_path:
            self._log('  Syncing shared hash registry from B2…', 'info')
            merged = pull_remote_hashes(
                self._registry, self._rclone_path,
                b2_key_id, b2_app_key, b2_bucket,
                get_machine_id(), log_fn=self._log)
            if merged:
                self._log(f'  ✓ Merged {merged:,} hash(es) from other machine(s)', 'ok')
            else:
                self._log('  ✓ No new remote hashes to merge', 'info')
            # Push this machine's full registry to B2 immediately so other
            # machines can see it — critical for bootstrapping a machine that
            # has existing hashes that have never been pushed before.
            self._registry.flush()
            self._registry.checkpoint()
            threading.Thread(
                target=push_local_hashes,
                args=(self._rclone_path, b2_key_id, b2_app_key,
                      b2_bucket, get_machine_id()),
                kwargs={'log_fn': self._log},
                daemon=True,
            ).start()

        # Upload coordinator
        if self._coordinator is not None:
            self._coordinator.stop()
        self._coordinator = UploadCoordinator(self._log, self._registry)
        self._coordinator.start()

        # Store run config so hot-added drives can pick it up
        self._run_cfg = dict(
            output_path=output_path, use_gdrive=use_gdrive,
            use_rclone=use_rclone, use_b2=use_b2,
            rclone_remote=rclone_remote, gd_subfolder=gd_subfolder,
            b2_key_id=b2_key_id, b2_app_key=b2_app_key, b2_bucket=b2_bucket,
            ntfy_topic=ntfy_topic,
        )
        self._semaphore = threading.Semaphore(max_workers)

        for row, info, _ in entries:
            self._launch_worker(row, info, resume_map.get(info.path, False))

        # Poll until all workers done (allows hot-add mid-run)
        while self._running:
            time.sleep(2)
            with self._drive_lock:
                alive = [w for _, _, w in self._drives
                         if w is not None and w.is_alive()]
            if not alive:
                break

        self.after(0, self._all_done)

    def _launch_worker(self, row: 'DriveRow', info: DriveInfo, resume: bool):
        """Spawn a worker for one drive. Safe to call mid-run."""
        cfg = self._run_cfg
        worker = DriveWorker(
            source_path=info.path, output_path=cfg['output_path'], info=info,
            registry=self._registry, coordinator=self._coordinator,
            use_gdrive=cfg['use_gdrive'], use_rclone=cfg['use_rclone'],
            use_b2=cfg['use_b2'], rclone_path=self._rclone_path or '',
            rclone_remote=cfg['rclone_remote'], gd_subfolder=cfg['gd_subfolder'],
            b2_key_id=cfg['b2_key_id'], b2_app_key=cfg['b2_app_key'],
            b2_bucket=cfg['b2_bucket'], log_fn=self._log,
            on_done=lambda s, r=row: self.after(0, lambda: self._drive_done(r, s)),
            on_progress=lambda s, r=row: self._on_progress_throttled(r, s),
            ntfy_topic=cfg['ntfy_topic'], resume=resume,
            running_ref=lambda: self._running,
        )
        with self._drive_lock:
            for i, (r2, info2, _) in enumerate(self._drives):
                if info2.path == info.path:
                    self._drives[i] = (r2, info2, worker)
                    break

        def _launch(w=worker):
            self._semaphore.acquire()
            try:
                w._run()
            finally:
                self._semaphore.release()

        t = threading.Thread(target=_launch, daemon=True,
                             name=f'slot-{info.label_or_name[:8]}')
        t.start()
        self.after(0, lambda r=row, w=worker: r.update_stats(w.stats))

    def _monitor(self, threads: list):
        for t in threads:
            t.join()
        self.after(0, self._all_done)

    def _on_progress_throttled(self, row: DriveRow, stats: DriveStats):
        """Called from worker thread (already throttled per-drive in DriveWorker)."""
        self.after(0, lambda: self._apply_progress(row, stats))

    def _apply_progress(self, row: DriveRow, stats: DriveStats):
        row.update_stats(stats)
        self._update_totals()

    def _update_totals(self):
        with self._drive_lock:
            all_stats = [w.stats for _, _, w in self._drives if w is not None]
        if not all_stats:
            self._totals_lbl.config(text='')
            return
        copied  = sum(s.copied        for s in all_stats)
        dupes   = sum(s.skipped_dupe  for s in all_stats)
        skipped = sum(s.skipped_resume + s.skipped_sys for s in all_stats)
        errors  = sum(s.errors        for s in all_stats)
        total_b = sum(s.bytes_copied  for s in all_stats)
        self._totals_lbl.config(
            text=f'TOTAL  ·  {copied:,} copied  ·  {dupes:,} dupes  ·  '
                 f'{skipped:,} skipped  ·  {errors:,} errors  ·  {_fmt_bytes(total_b)}')

    def _poll_staged(self):
        """Scan staging dirs every 5s and update the staged-bytes label."""
        total = 0
        tmpdir = tempfile.gettempdir()
        try:
            for entry in os.scandir(tmpdir):
                if entry.name.startswith('se_stage_'):
                    for root, _, files in os.walk(entry.path):
                        for f in files:
                            try:
                                total += os.path.getsize(os.path.join(root, f))
                            except OSError:
                                pass
        except OSError:
            pass

        if total > 0:
            self._staged_lbl.config(text=f'⏳ staged for upload  ·  {_fmt_bytes(total)}')
        else:
            self._staged_lbl.config(text='')

        self.after(5000, self._poll_staged)

    def _drive_done(self, row: DriveRow, stats: DriveStats):
        row.update_stats(stats)
        self._update_totals()

    def _all_done(self):
        was_aborted = not self._running   # _stop() sets this False before we get here
        self._running = False
        self.btn_start.config(state='normal')
        self.btn_stop.config(state='disabled')
        if was_aborted:
            self._set_status('> aborted')
            self._log('\n══ Run aborted ══', 'warn')
        else:
            self._set_status('> all drives complete')
            self._log('\n══ All drives finished ══', 'ok')
        self._update_totals()
        if self._coordinator:
            self._coordinator.stop()
        if self._registry:
            self._registry.flush()
            cfg = self._run_cfg or {}
            if cfg.get('use_b2') and self._rclone_path:
                self._registry.checkpoint()
                self._log('  ↑ Final hash DB sync to B2 queued…', 'info')
                threading.Thread(
                    target=push_local_hashes,
                    args=(self._rclone_path, cfg['b2_key_id'], cfg['b2_app_key'],
                         cfg['b2_bucket'], get_machine_id()),
                    kwargs={'log_fn': self._log},
                    daemon=True,
                ).start()

    def _stop(self):
        self._running = False
        self._set_status('> aborting…')

    # ── Cloud sanity checks ───────────────────────────────────────────────────

    def _check_b2(self, key_id, app_key, bucket) -> bool:
        self._log('── Checking Backblaze B2 connection…', 'head')
        try:
            r = subprocess.run(
                [self._rclone_path, 'lsd', ':b2:',
                 f'--b2-account={key_id}', f'--b2-key={app_key}'],
                capture_output=True, text=True, timeout=60)
            if r.returncode != 0:
                self._log(f'  ERR  B2 auth failed: {r.stderr.strip()[:300]}', 'err')
                return False
            if bucket not in r.stdout:
                self._log(f'  ERR  Bucket "{bucket}" not found. Available:\n{r.stdout.strip()[:300]}', 'err')
                return False
            self._log(f'  B2 OK — bucket {bucket} found', 'ok')
            return True
        except subprocess.TimeoutExpired:
            self._log('  ERR  B2 check timed out', 'err')
            return False
        except Exception as e:
            self._log(f'  ERR  B2 check: {e}', 'err')
            return False

    def _check_rclone(self, remote) -> bool:
        self._log('── Checking rclone / Google Drive…', 'head')
        try:
            r = subprocess.run(
                [self._rclone_path, 'about', f'{remote}:'],
                capture_output=True, text=True, timeout=60)
            if r.returncode != 0:
                self._log(f'  ERR  rclone: {r.stderr.strip()[:300]}', 'err')
                return False
            self._log(f'  rclone OK — {remote}:', 'ok')
            return True
        except subprocess.TimeoutExpired:
            self._log('  ERR  rclone timed out', 'err')
            return False
        except Exception as e:
            self._log(f'  ERR  rclone: {e}', 'err')
            return False

    # ── Logging ───────────────────────────────────────────────────────────────

    def _log(self, msg: str, tag: str = ''):
        def _do():
            self.log_box.config(state='normal')
            self.log_box.insert('end', msg + '\n', tag or ())
            self.log_box.see('end')
            self.log_box.config(state='disabled')
        self.after(0, _do)

    def _set_status(self, msg: str):
        self.after(0, lambda: self.var_status.set(msg))


# ══════════════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════

def main():
    # --selfcheck: CI smoke test for frozen builds. Verifies critical modules
    # (esp. tkinter, which PyInstaller has silently omitted before) without
    # opening a window — headless runners have no display.
    if '--selfcheck' in sys.argv:
        print(f'Shinigami Eyes v{VERSION} selfcheck')
        print(f'  python  : {sys.version.split()[0]}')
        print(f'  tkinter : {tk.TkVersion}')
        print(f'  sqlite3 : {sqlite3.sqlite_version}')
        print('OK')
        return

    # --uicheck: construct the FULL UI, pump one event-loop pass, tear down.
    # Catches widget-construction bugs that import checks can't (e.g. the
    # tuple-pady TclError that crashed v3.1.1 on launch). Requires a display,
    # which GitHub's macOS runners have.
    if '--uicheck' in sys.argv:
        app = MigrationApp()
        app.update()
        app.destroy()
        print(f'Shinigami Eyes v{VERSION} uicheck OK')
        return

    app = MigrationApp()
    app.mainloop()


if __name__ == '__main__':
    main()
