#!/usr/bin/env python3
"""
nas_migrate_gui.py — Shinigami Eyes

Run with:
    python3 nas_migrate_gui.py

Requires Python 3 with tkinter.
  • Homebrew Python already includes it: brew install python
  • Or install separately: brew install python-tk
"""

import tkinter as tk
from tkinter import ttk, filedialog, scrolledtext, messagebox
import threading
import os
import shutil
import hashlib
import subprocess
import glob
import re
import tempfile
import hashlib as _hashlib_state
from datetime import datetime
from pathlib import Path

# ══════════════════════════════════════════════════════════════════════════════
# CONFIG
# ══════════════════════════════════════════════════════════════════════════════

BATCH_LIMIT_GB    = 10
BATCH_LIMIT_BYTES = BATCH_LIMIT_GB * 1024 * 1024 * 1024

MIN_FREE_GB       = 15   # pause migration until this much local space is free

# Local state directory — always on your machine, never on Google Drive or a NAS.
STATE_DIR      = Path.home() / '.shinigami_eyes'
HASH_DB_FILE   = STATE_DIR / 'hashes.db'
# Progress files are per-destination: progress_<8-char dest hash>.db

DOCUMENT_EXTS = {
    'pdf', 'doc', 'docx', 'txt', 'rtf', 'odt', 'xls', 'xlsx', 'xlsm',
    'csv', 'ppt', 'pptx', 'pptm', 'pages', 'numbers', 'keynote',
    'md', 'epub', 'wpd', 'dotx', 'docm',
}

IMAGE_EXTS = {
    'jpg', 'jpeg', 'png', 'gif', 'bmp', 'tiff', 'tif', 'heic', 'heif',
    'webp', 'svg', 'psd', 'raw', 'cr2', 'cr3', 'nef', 'arw', 'dng',
    'orf', 'rw2', 'raf', 'x3f', 'erf', 'mos', 'mef', 'rwl', 'srw',
    'srf', 'sr2', 'nrw', 'fff', 'iiq', '3fr', 'cap', 'ptx', 'pef',
    'kdc', 'mdc', 'mrw', 'rwz',
}

SKIP_FILENAMES = {
    '.ds_store', 'thumbs.db', 'desktop.ini', '.localized',
}

SKIP_DIRS = {
    '.spotlight-v100', '.trashes', '.fseventsd', '.temporaryitems',
    '__macosx', 'recycler', '$recycle.bin', 'system volume information',
    'caches', 'tmp', 'temp', '.cache', 'windows', 'program files',
    'program files (x86)',
}

# ── Colors — Matrix / hacker theme ───────────────────────────────────────────
BG       = '#030b03'   # near-black, green-tinted
FG       = '#00ff41'   # matrix green
ACCENT   = '#00ff41'   # bright green
SURFACE  = '#0a160a'   # dark green surface
BORDER   = '#1a4d1a'   # visible green border
GREEN    = '#00ff41'   # OK — bright matrix green
YELLOW   = '#ffe100'   # warnings — yellow (readable against black)
RED      = '#ff2222'   # errors — keep red for contrast
PURPLE   = '#00ffcc'   # section dividers — teal-green
TEAL     = '#00e5cc'   # Google Drive lines
MUTED    = '#2d7a2d'   # dim green for secondary text


# ══════════════════════════════════════════════════════════════════════════════
# FILE HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def progress_file_for(output_path: str) -> Path:
    """Return a local progress-file path keyed to a specific output destination."""
    key = _hashlib_state.md5(output_path.encode()).hexdigest()[:8]
    return STATE_DIR / f'progress_{key}.db'



def free_bytes() -> int:
    """Return available bytes on the volume where user data lives."""
    import shutil
    return shutil.disk_usage(Path.home()).free


def wait_for_space(min_gb: float, status_fn=None):
    """
    Block until at least min_gb of free space is available,
    reporting progress every 5 seconds via status_fn if provided.
    """
    import time
    min_bytes = min_gb * 1024 ** 3
    while free_bytes() < min_bytes:
        free = free_bytes() / 1024 ** 3
        if status_fn:
            status_fn(
                f'> waiting for GDrive to upload...  '
                f'{free:.1f} GB free, need {min_gb:.0f} GB'
            )
        time.sleep(5)


def find_google_drive():
    home = Path.home()
    matches = glob.glob(str(home / 'Library/CloudStorage/GoogleDrive-*/My Drive'))
    if matches:
        return matches[0]
    for p in [home / 'Google Drive/My Drive', home / 'Google Drive']:
        if p.exists():
            return str(p)
    return None


def find_rclone():
    """Return (rclone_binary, [remote_names]) or (None, []) if unavailable."""
    import shutil as _sh
    candidates = [
        _sh.which('rclone'),
        '/opt/homebrew/bin/rclone',
        '/usr/local/bin/rclone',
    ]
    rclone = next((p for p in candidates if p and os.path.isfile(p)), None)
    if not rclone:
        return None, []
    try:
        out = subprocess.run(
            [rclone, 'listremotes'],
            capture_output=True, text=True, timeout=10,
        )
        remotes = [r.rstrip(':') for r in out.stdout.strip().splitlines() if r.strip()]
        return rclone, remotes
    except Exception:
        return rclone, []


def get_md5(filepath):
    h = hashlib.md5()
    try:
        with open(filepath, 'rb') as f:
            for chunk in iter(lambda: f.read(131072), b''):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def copy_and_hash(src_path: str, dest_dir: str):
    """
    Stream src_path into a temp file inside dest_dir while computing MD5 —
    single read pass instead of hash-then-copy.

    Returns (md5_hex, tmp_filepath, bytes_written).
    Caller must either shutil.move(tmp, final_dest) or os.unlink(tmp).
    Cleans up tmp and re-raises on any error.
    """
    h = hashlib.md5()
    tmp = os.path.join(dest_dir,
                       f'.shinigami_tmp_{os.getpid()}_{os.urandom(4).hex()}')
    written = 0
    try:
        with open(src_path, 'rb') as fsrc, open(tmp, 'wb') as fdst:
            for chunk in iter(lambda: fsrc.read(131072), b''):
                h.update(chunk)
                fdst.write(chunk)
                written += len(chunk)
        shutil.copystat(src_path, tmp)
        return h.hexdigest(), tmp, written
    except Exception:
        try:
            os.unlink(tmp)
        except Exception:
            pass
        raise


def get_creation_date(filepath):
    try:
        out = subprocess.run(
            ['mdls', '-raw', '-name', 'kMDItemContentCreationDate', filepath],
            capture_output=True, text=True, timeout=5
        ).stdout
        m = re.search(r'(\d{4})-(\d{2})-(\d{2})', out)
        if m:
            return f"{m.group(1)}-{m.group(3)}-{m.group(2)}"   # YYYY-DD-MM
    except Exception:
        pass
    try:
        out = subprocess.run(
            ['stat', '-f', '%SB', '-t', '%Y-%m-%d', filepath],
            capture_output=True, text=True, timeout=5
        ).stdout.strip()
        if re.match(r'\d{4}-\d{2}-\d{2}', out):
            y, mo, d = out[:4], out[5:7], out[8:10]
            return f"{y}-{d}-{mo}"
    except Exception:
        pass
    return datetime.now().strftime('%Y-%d-%m')


def build_dest_path(dest_dir, filename, source_file):
    p = Path(filename)
    base, ext = p.stem, p.suffix

    dest = Path(dest_dir) / filename
    if not dest.exists():
        return str(dest)

    cdate = get_creation_date(source_file)
    dest = Path(dest_dir) / f"{base}-{cdate}{ext}"
    if not dest.exists():
        return str(dest)

    n = 1
    while True:
        dest = Path(dest_dir) / f"{base}-DUPLICATE-{cdate}-{n}{ext}"
        if not dest.exists():
            return str(dest)
        n += 1


def should_skip(filepath, source_root):
    p = Path(filepath)
    name = p.name
    if name.startswith('.') or name.lower() in SKIP_FILENAMES:
        return True
    if name.startswith('~$') or name.endswith(('.tmp', '.temp', '.crdownload', '.part', '.swp')):
        return True
    parent = p.parent
    while parent != Path(source_root) and parent != parent.parent:
        dn = parent.name.lower()
        if dn.startswith('.') or dn in SKIP_DIRS:
            return True
        parent = parent.parent
    return False


# ══════════════════════════════════════════════════════════════════════════════
# GUI
# ══════════════════════════════════════════════════════════════════════════════

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title('Shinigami Eyes')
        self.configure(bg=BG)
        self.resizable(True, True)
        self.minsize(660, 700)

        self._running         = False
        self._thread          = None
        self._gd_path         = find_google_drive()
        self._rclone_path, self._rclone_remotes = find_rclone()

        self._build_ui()
        self.after(200, self._cleanup_orphans)

    # ── Startup orphan cleanup ────────────────────────────────────────────────

    def _cleanup_orphans(self):
        """Remove any nas_migrate_stage_* temp dirs left behind by crashed runs."""
        pattern = os.path.join(tempfile.gettempdir(), 'nas_migrate_stage_*')
        orphans = glob.glob(pattern)
        if not orphans:
            return
        total_bytes = 0
        removed = 0
        for d in orphans:
            try:
                for root, _, files in os.walk(d):
                    for f in files:
                        try:
                            total_bytes += os.path.getsize(os.path.join(root, f))
                        except Exception:
                            pass
                shutil.rmtree(d, ignore_errors=True)
                removed += 1
            except Exception:
                pass
        if removed:
            gb = total_bytes / (1024 ** 3)
            self._log(
                f'  STARTUP  Removed {removed} orphaned staging folder(s)  '
                f'({gb:.2f} GB recovered from temp)',
                'skip',
            )
            self._set_status(f'> cleaned {removed} orphaned temp folder(s) — {gb:.2f} GB freed')

    # ── UI construction ───────────────────────────────────────────────────────

    def _build_ui(self):
        self._build_header()
        self._build_form()
        self._build_progress()
        self._build_statusbar()

    def _build_header(self):
        h = tk.Frame(self, bg='#000a00', pady=14)
        h.pack(fill='x')
        tk.Label(h, text='[ SHINIGAMI EYES ]', font=('Menlo', 18, 'bold'),
                 fg='#00ff41', bg='#000a00').pack(side='left', padx=20)
        right = tk.Frame(h, bg='#000a00')
        right.pack(side='right', padx=20)
        tk.Label(right, text='// file migration system //', font=('Menlo', 10),
                 fg=MUTED, bg='#000a00').pack(anchor='e')
        tk.Label(right, text='developed by r41n403', font=('Menlo', 9),
                 fg='#1a5c1a', bg='#000a00').pack(anchor='e')

    def _build_form(self):
        outer = tk.Frame(self, bg=BG, padx=20, pady=16)
        outer.pack(fill='x')

        # ── Source drive ──────────────────────────────────────────────────────
        self._label(outer, 'Source drive').pack(anchor='w')
        src_row = tk.Frame(outer, bg=BG)
        src_row.pack(fill='x', pady=(4, 14))

        self.var_source = tk.StringVar()
        self._entry(src_row, self.var_source, width=46).pack(side='left', fill='x', expand=True)
        self._btn(src_row, 'Browse /Volumes…', self._browse_source).pack(side='left', padx=(8, 0))

        # ── Destination mode ──────────────────────────────────────────────────
        self._label(outer, 'Destination').pack(anchor='w')
        mode_row = tk.Frame(outer, bg=BG)
        mode_row.pack(fill='x', pady=(4, 10))

        self.var_mode = tk.StringVar(value='local')

        rb_cfg = dict(bg=BG, fg=FG, activebackground=BG, activeforeground='#00ff41',
                      selectcolor='#00ff41', font=('Menlo', 11))

        tk.Radiobutton(mode_row, text='Local folder',
                       variable=self.var_mode, value='local',
                       command=self._on_mode_change, **rb_cfg).pack(side='left')

        gd_text  = 'Google Drive' if self._gd_path else 'Google Drive (not detected)'
        gd_state = 'normal' if self._gd_path else 'disabled'
        tk.Radiobutton(mode_row, text=gd_text,
                       variable=self.var_mode, value='gdrive',
                       command=self._on_mode_change,
                       state=gd_state, **rb_cfg).pack(side='left', padx=(20, 0))

        # ── Local path panel ──────────────────────────────────────────────────
        self.panel_local = tk.Frame(outer, bg=BG)
        self.panel_local.pack(fill='x', pady=(0, 14))

        self._label(self.panel_local, 'Output folder').pack(anchor='w')
        loc_row = tk.Frame(self.panel_local, bg=BG)
        loc_row.pack(fill='x', pady=(4, 0))

        self.var_output = tk.StringVar()
        self._entry(loc_row, self.var_output, width=46).pack(side='left', fill='x', expand=True)
        self._btn(loc_row, 'Choose…', self._browse_output).pack(side='left', padx=(8, 0))

        # ── Google Drive panel ────────────────────────────────────────────────
        self.panel_gdrive = tk.Frame(outer, bg=BG)
        # (not packed yet — shown on mode switch)

        if self._gd_path:
            tk.Label(self.panel_gdrive,
                     text=f'Google Drive: {self._gd_path}',
                     font=('Menlo', 10), fg=MUTED, bg=BG,
                     wraplength=580, justify='left').pack(anchor='w')

        # ── rclone status ──────────────────────────────────────────────────────
        rclone_row = tk.Frame(self.panel_gdrive, bg=BG)
        rclone_row.pack(fill='x', pady=(6, 0))

        if self._rclone_path and self._rclone_remotes:
            tk.Label(rclone_row, text='rclone ✓', font=('Menlo', 10, 'bold'),
                     fg=GREEN, bg=BG).pack(side='left')
            tk.Label(rclone_row, text=' — uploads directly to cloud (no local cache)',
                     font=('Menlo', 10), fg=MUTED, bg=BG).pack(side='left')

            remote_row = tk.Frame(self.panel_gdrive, bg=BG)
            remote_row.pack(fill='x', pady=(6, 0))
            self._label(remote_row, 'Remote').pack(side='left')
            self.var_rclone_remote = tk.StringVar(value=self._rclone_remotes[0])
            remote_menu = tk.OptionMenu(remote_row, self.var_rclone_remote,
                                        *self._rclone_remotes)
            remote_menu.config(font=('Menlo', 11), bg=SURFACE, fg=FG,
                               activebackground=BORDER, activeforeground=FG,
                               highlightthickness=0, relief='flat')
            remote_menu['menu'].config(font=('Menlo', 11), bg=SURFACE, fg=FG)
            remote_menu.pack(side='left', padx=(10, 0))
        else:
            tk.Label(rclone_row,
                     text='rclone not found — install with: brew install rclone && rclone config',
                     font=('Menlo', 10), fg=YELLOW, bg=BG,
                     wraplength=560, justify='left').pack(anchor='w')
            self.var_rclone_remote = tk.StringVar(value='')

        gd_sub_row = tk.Frame(self.panel_gdrive, bg=BG)
        gd_sub_row.pack(fill='x', pady=(8, 4))
        self._label(gd_sub_row, 'Subfolder name').pack(side='left')
        self.var_gd_sub = tk.StringVar(value='NAS Migration')
        self._entry(gd_sub_row, self.var_gd_sub, width=28).pack(side='left', padx=(10, 0))

        info_text = (
            f'Files are staged locally in {BATCH_LIMIT_GB} GB batches, then '
            'uploaded to Google Drive via rclone and removed from local storage.'
            if (self._rclone_path and self._rclone_remotes) else
            f'Files are staged locally in {BATCH_LIMIT_GB} GB batches, '
            'then moved to Google Drive automatically.'
        )
        tk.Label(self.panel_gdrive, text=info_text,
                 font=('Helvetica Neue', 10), fg=MUTED, bg=BG,
                 wraplength=580, justify='left').pack(anchor='w', pady=(0, 14))

        # ── Action buttons ────────────────────────────────────────────────────
        ttk.Separator(outer).pack(fill='x', pady=4)
        btn_row = tk.Frame(outer, bg=BG, pady=12)
        btn_row.pack()

        self.btn_start = tk.Button(
            btn_row, text='▶  EXECUTE',
            font=('Menlo', 13, 'bold'),
            bg='#00ff41', fg='#000000', activebackground='#00cc33', activeforeground='#000000',
            relief='flat', padx=22, pady=9, cursor='hand2',
            command=self._start,
        )
        self.btn_start.pack(side='left')

        self.btn_stop = tk.Button(
            btn_row, text='■  ABORT',
            font=('Menlo', 13, 'bold'),
            bg='#ff2222', fg='#000000', activebackground='#cc0000', activeforeground='#000000',
            relief='flat', padx=22, pady=9, cursor='hand2',
            state='disabled', command=self._stop,
        )
        self.btn_stop.pack(side='left', padx=(12, 0))

    def _build_progress(self):
        frame = tk.Frame(self, bg=BG, padx=20)
        frame.pack(fill='both', expand=True, pady=(0, 4))

        header = tk.Frame(frame, bg=BG)
        header.pack(fill='x', pady=(0, 6))
        self._label(header, 'Progress').pack(side='left')

        self.btn_clear = tk.Button(header, text='[ clear ]',
                                   font=('Menlo', 10), fg=MUTED,
                                   bg=BG, activebackground=SURFACE,
                                   activeforeground=FG,
                                   relief='flat', cursor='hand2',
                                   command=self._clear_log)
        self.btn_clear.pack(side='right')

        self.log_box = scrolledtext.ScrolledText(
            frame, font=('Menlo', 10),
            bg='#000a00', fg='#00cc33', insertbackground=FG,
            relief='flat', borderwidth=0,
            state='disabled',
        )
        self.log_box.pack(fill='both', expand=True)

        self.log_box.tag_config('ok',   foreground=GREEN)
        self.log_box.tag_config('skip', foreground=YELLOW)
        self.log_box.tag_config('err',  foreground=RED)
        self.log_box.tag_config('info', foreground=ACCENT)
        self.log_box.tag_config('head', foreground=PURPLE, font=('Menlo', 10, 'bold'))
        self.log_box.tag_config('gd',   foreground=TEAL)

    def _build_statusbar(self):
        bar = tk.Frame(self, bg='#000a00', pady=6)
        bar.pack(fill='x', side='bottom')
        self.var_status = tk.StringVar(value='> ready_')
        tk.Label(bar, textvariable=self.var_status,
                 font=('Menlo', 10), fg=MUTED, bg='#000a00',
                 anchor='w').pack(side='left', padx=16)

    # ── Widget factories ──────────────────────────────────────────────────────

    def _label(self, parent, text):
        return tk.Label(parent, text=text, font=('Menlo', 11, 'bold'),
                        fg=FG, bg=BG)

    def _entry(self, parent, var, width=40):
        return tk.Entry(parent, textvariable=var, width=width,
                        font=('Menlo', 11), bg='#000a00', fg='#00ff41',
                        insertbackground='#00ff41', relief='flat',
                        highlightthickness=1, highlightcolor='#00ff41',
                        highlightbackground=BORDER)

    def _btn(self, parent, text, cmd):
        return tk.Button(parent, text=text, command=cmd,
                         font=('Menlo', 11),
                         bg=SURFACE, fg='#00ff41', activebackground=BORDER,
                         activeforeground='#00ff41',
                         relief='flat', padx=12, pady=5, cursor='hand2')

    # ── Mode switch ───────────────────────────────────────────────────────────

    def _on_mode_change(self):
        if self.var_mode.get() == 'local':
            self.panel_gdrive.pack_forget()
            self.panel_local.pack(fill='x', pady=(0, 14),
                                  before=self.btn_start.master.master)  # reinsert above separator
        else:
            self.panel_local.pack_forget()
            self.panel_gdrive.pack(fill='x', pady=(0, 0),
                                   before=self.btn_start.master.master)

    # ── Actions ───────────────────────────────────────────────────────────────

    def _browse_source(self):
        path = filedialog.askdirectory(initialdir='/Volumes',
                                       title='Select source drive or folder')
        if path:
            self.var_source.set(path)

    def _browse_output(self):
        path = filedialog.askdirectory(title='Select output folder')
        if path:
            self.var_output.set(path)

    def _clear_log(self):
        self.log_box.config(state='normal')
        self.log_box.delete('1.0', 'end')
        self.log_box.config(state='disabled')

    def _start(self):
        raw_source = self.var_source.get().strip()
        if not raw_source:
            messagebox.showwarning('Missing input', 'Please enter or browse for a source drive.')
            return

        # Resolve source path
        if os.path.isdir(raw_source):
            source_path = raw_source
        elif os.path.isdir(f'/Volumes/{raw_source}'):
            source_path = f'/Volumes/{raw_source}'
        else:
            vols = ', '.join(os.listdir('/Volumes')) if os.path.isdir('/Volumes') else 'none'
            messagebox.showerror('Drive not found',
                f"Cannot find '{raw_source}'.\n\nAvailable volumes: {vols}")
            return

        use_gdrive = (self.var_mode.get() == 'gdrive')

        if use_gdrive:
            subfolder     = self.var_gd_sub.get().strip() or 'NAS Migration'
            rclone_remote = self.var_rclone_remote.get().strip()
            use_rclone    = bool(self._rclone_path and rclone_remote)

            if use_rclone:
                # rclone mode: output_path is a stable key for the progress file
                output_path = f'rclone:{rclone_remote}/{subfolder}'
            else:
                if not self._gd_path:
                    messagebox.showerror('Google Drive not found',
                        'Google Drive for Desktop does not appear to be installed or signed in.\n\n'
                        'Install rclone (brew install rclone && rclone config) or install '
                        'Google Drive for Desktop.')
                    return
                output_path = os.path.join(self._gd_path, subfolder)
        else:
            use_rclone    = False
            rclone_remote = ''
            output_path = self.var_output.get().strip()
            if not output_path:
                messagebox.showwarning('Missing input', 'Please choose an output folder.')
                return

        # ── Check for a previous session (state files are always local) ─────
        # rclone output_path is a remote key, not a local path — don't mkdir it
        if not use_rclone:
            os.makedirs(output_path, exist_ok=True)
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        progress_path = progress_file_for(output_path)
        hash_db_path  = HASH_DB_FILE

        resume = False
        if os.path.exists(progress_path):
            try:
                with open(progress_path) as f:
                    done_count = sum(1 for line in f if line.strip())
            except Exception:
                done_count = 0

            answer = messagebox.askquestion(
                'Resume previous session?',
                f'{done_count:,} files were already processed in a previous run.\n\n'
                '[ YES ]  Resume — skip already-processed files, pick up where it stopped.\n\n'
                '[ NO ]   Start fresh — rescan everything. Cross-drive duplicate\n'
                '         hashes are always kept regardless.',
                icon='question',
            )
            resume = (answer == 'yes')
            if not resume:
                try:
                    os.remove(progress_path)
                except Exception:
                    pass

        # ── Launch ────────────────────────────────────────────────────────────
        self._running = True
        self.btn_start.config(state='disabled')
        self.btn_stop.config(state='normal')
        self._clear_log()

        self._thread = threading.Thread(
            target=self._run,
            args=(source_path, output_path, use_gdrive, resume, use_rclone, rclone_remote, subfolder if use_gdrive else ''),
            daemon=True,
        )
        self._thread.start()

    def _stop(self):
        self._running = False
        self._set_status('> aborting after current file...')

    def _run(self, source_path, output_path, use_gdrive, resume=False,
             use_rclone=False, rclone_remote='', gd_subfolder=''):
        try:
            self._migrate(source_path, output_path, use_gdrive, resume,
                          use_rclone, rclone_remote, gd_subfolder)
        except Exception as e:
            self._log(f'  FATAL: {e}', 'err')
        finally:
            self.after(0, self._on_done)

    def _on_done(self):
        self._running = False
        self.btn_start.config(state='normal')
        self.btn_stop.config(state='disabled')

    # ── Logging helpers ───────────────────────────────────────────────────────

    def _log(self, msg, tag=''):
        def _do():
            self.log_box.config(state='normal')
            self.log_box.insert('end', msg + '\n', tag or ())
            self.log_box.see('end')
            self.log_box.config(state='disabled')
        self.after(0, _do)

    def _set_status(self, msg):
        self.after(0, lambda: self.var_status.set(msg))

    # ══════════════════════════════════════════════════════════════════════════
    # MIGRATION LOGIC
    # ══════════════════════════════════════════════════════════════════════════

    def _migrate(self, source_path, output_path, use_gdrive, resume=False,
                 use_rclone=False, rclone_remote='', gd_subfolder=''):
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')

        copied = skipped_dupe = skipped_sys = skipped_resume = errors = batches = 0
        batch_bytes = 0

        # ── Persistent state file paths (always local, never on GDrive/NAS) ──
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        hash_db_path  = HASH_DB_FILE
        progress_path = progress_file_for(output_path)

        # ── Load existing hash DB (always — enables cross-drive dedup) ────────
        seen_hashes = {}   # md5 → original source path
        if os.path.exists(hash_db_path):
            try:
                with open(hash_db_path, 'r', errors='replace') as f:
                    for line in f:
                        line = line.strip()
                        if '|' in line:
                            h, _, src = line.partition('|')
                            if h and src:
                                seen_hashes[h] = src
            except Exception:
                pass

        # ── Load progress log (resume mode only) ─────────────────────────────
        processed_paths = set()   # source paths already handled in a prior run
        if resume and os.path.exists(progress_path):
            try:
                with open(progress_path, 'r', errors='replace') as f:
                    for line in f:
                        p = line.strip()
                        if p:
                            processed_paths.add(p)
            except Exception:
                pass

        # ── Set up directories ────────────────────────────────────────────────
        if use_gdrive:
            stage_dir    = tempfile.mkdtemp(prefix='nas_migrate_stage_')
            stage_docs   = os.path.join(stage_dir, 'Documents')
            stage_photos = os.path.join(stage_dir, 'Photos')
            os.makedirs(stage_docs,   exist_ok=True)
            os.makedirs(stage_photos, exist_ok=True)
            if not use_rclone:
                # Legacy mode: move files into local GDrive folder
                gd_docs   = os.path.join(output_path, 'Documents')
                gd_photos = os.path.join(output_path, 'Photos')
                os.makedirs(gd_docs,   exist_ok=True)
                os.makedirs(gd_photos, exist_ok=True)
            docs_dir   = stage_docs
            photos_dir = stage_photos
            log_path   = STATE_DIR / f'migration_log_{timestamp}.txt'
        else:
            docs_dir   = os.path.join(output_path, 'Documents')
            photos_dir = os.path.join(output_path, 'Photos')
            os.makedirs(docs_dir,   exist_ok=True)
            os.makedirs(photos_dir, exist_ok=True)
            log_path   = os.path.join(output_path, f'migration_log_{timestamp}.txt')

        log_lines = []

        def log(msg, tag=''):
            self._log(msg, tag)
            log_lines.append(msg)

        # ── Open persistent files for appending ───────────────────────────────
        try:
            hash_fh     = open(hash_db_path,  'a', buffering=1)
            progress_fh = open(progress_path, 'a', buffering=1)
        except Exception as e:
            log(f'  FATAL  Cannot open state files: {e}', 'err')
            return

        def mark_done(filepath):
            """Record a source path as fully processed."""
            processed_paths.add(filepath)
            progress_fh.write(filepath + '\n')

        def confirm_batch(batch):
            """Write hashes + mark done for a confirmed-uploaded batch."""
            for src_fp, src_hash in batch:
                hash_fh.write(f'{src_hash}|{src_fp}\n')
                mark_done(src_fp)
            hash_fh.flush()
            progress_fh.flush()

        # pending_batch: files staged but not yet confirmed uploaded.
        # (source_path, md5) pairs — only written to DB after upload verified.
        pending_batch = []

        # ── Batch flush (Google Drive mode) ───────────────────────────────────
        def flush_batch():
            nonlocal batch_bytes, batches

            # Count staged files
            staged = []
            for src_d in [stage_docs, stage_photos]:
                for root, _, files in os.walk(src_d):
                    staged.extend(os.path.join(root, f) for f in files)
            if not staged:
                return

            batches += 1
            gb = batch_bytes / (1024 ** 3)
            batch_bytes = 0

            if use_rclone:
                # ── rclone copy then manual cleanup ───────────────────────────
                # Using 'copy' (not 'move') so rclone never deletes local files.
                # Concurrent workers in 'move' delete files as they finish, then
                # retries hit "no such file" on already-deleted files — causing
                # false failure exits. With 'copy' we delete staging ourselves
                # only after a clean exit code.
                dest_remote = f'{rclone_remote}:{gd_subfolder}'
                log(f'\n  ── Flushing batch #{batches}  ({gb:.1f} GB, {len(staged)} files)'
                    f' → {dest_remote}', 'gd')
                try:
                    proc = subprocess.Popen(
                        [self._rclone_path, 'copy', stage_dir, dest_remote,
                         '--no-traverse',
                         '--transfers=8',
                         '--checkers=16',
                         '--buffer-size=64M',
                         '--drive-chunk-size=128M',
                         '-v',
                         '--stats=10s',
                         '--stats-one-line'],
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        text=True, bufsize=1,
                    )
                    # Stream rclone output live so progress is visible in the log
                    for line in proc.stdout:
                        line = line.rstrip()
                        if line:
                            log(f'  rclone: {line}', 'gd')
                        if not self._running:
                            proc.terminate()
                            break
                    proc.wait()
                    returncode = proc.returncode

                    if returncode != 0 and self._running:
                        log(f'  ERR  rclone exited with code {returncode} — staged files kept for retry', 'err')
                        # Leave pending_batch intact — not confirmed, not marked done
                    elif self._running:
                        # Upload confirmed — now safe to clean up staging locally
                        shutil.rmtree(stage_dir, ignore_errors=True)
                        os.makedirs(stage_docs,   exist_ok=True)
                        os.makedirs(stage_photos,  exist_ok=True)
                        log(f'  ── Batch #{batches} uploaded ✓  confirming {len(pending_batch)} files...', 'gd')
                        confirm_batch(pending_batch)
                        pending_batch.clear()
                        log(f'  ── Confirmed. Resuming scan.\n', 'gd')
                except Exception as e:
                    log(f'  ERR  rclone exception: {e} — staged files kept for retry', 'err')

            else:
                # ── Legacy: move into ~/Library/CloudStorage/... ──────────────
                pairs = []
                for src_d, dst_d in [(stage_docs, gd_docs), (stage_photos, gd_photos)]:
                    for root, _, files in os.walk(src_d):
                        for f in files:
                            pairs.append((os.path.join(root, f), dst_d))
                log(f'\n  ── Flushing batch #{batches}  ({gb:.1f} GB, {len(pairs)} files) → Google Drive', 'gd')
                confirmed = []
                for src, dst_d in pairs:
                    if not self._running:
                        break
                    if not os.path.exists(src):
                        continue
                    dest = build_dest_path(dst_d, os.path.basename(src), src)
                    try:
                        shutil.move(src, dest)
                        log(f'  →GD  {os.path.basename(dest)}', 'gd')
                        confirmed.append(src)
                    except Exception as e:
                        log(f'  ERR  move failed: {os.path.basename(src)} — {e}', 'err')
                # Confirm only the files that actually moved
                confirmed_set = set(confirmed)
                confirmed_batch = [(fp, h) for fp, h in pending_batch if fp in confirmed_set]
                confirm_batch(confirmed_batch)
                pending_batch[:] = [(fp, h) for fp, h in pending_batch if fp not in confirmed_set]
                log(f'  ── Batch #{batches} moved to Google Drive ✓', 'gd')

                # Wait for GDrive to upload and free local space
                if free_bytes() < MIN_FREE_GB * 1024 ** 3:
                    log(f'  ── Disk below {MIN_FREE_GB} GB free — waiting for GDrive to upload...', 'gd')
                    wait_for_space(MIN_FREE_GB, status_fn=self._set_status)
                    log(f'  ── Space recovered, resuming.\n', 'gd')
                else:
                    log('', 'gd')

        # ── Process a single file ─────────────────────────────────────────────
        def process_file(filepath, dest_dir):
            nonlocal copied, skipped_dupe, skipped_sys, skipped_resume, errors, batch_bytes

            if not self._running:
                return

            # Resume fast-path: already handled in a previous run
            if filepath in processed_paths:
                skipped_resume += 1
                return

            if should_skip(filepath, source_path):
                skipped_sys += 1
                return

            # Single-pass: stream to temp file in dest_dir while computing hash.
            # This avoids reading the source file twice (hash then copy).
            tmp_path = None
            try:
                h, tmp_path, fsize = copy_and_hash(filepath, dest_dir)
            except Exception as e:
                log(f'  ERR   {os.path.basename(filepath)}: {e}', 'err')
                errors += 1
                return  # not marked done — will retry on resume

            if h in seen_hashes:
                # Duplicate — discard the temp copy we just wrote
                try:
                    os.unlink(tmp_path)
                except Exception:
                    pass
                log(f'  SKIP  {os.path.basename(filepath)}'
                    f'  ← dup of {os.path.basename(seen_hashes[h])}', 'skip')
                skipped_dupe += 1
                mark_done(filepath)
                return

            # New unique file — update in-memory dedup immediately,
            # but defer hash DB write + mark_done until upload is confirmed.
            seen_hashes[h] = filepath

            dest = build_dest_path(dest_dir, os.path.basename(filepath), filepath)
            try:
                shutil.move(tmp_path, dest)
                log(f'  OK    {os.path.basename(dest)}', 'ok')
                copied += 1
                if use_gdrive:
                    # Pend confirmation until rclone verifies the upload
                    pending_batch.append((filepath, h))
                    batch_bytes += fsize
                    if batch_bytes >= BATCH_LIMIT_BYTES:
                        flush_batch()
                else:
                    # Local mode: file is in final dest now — safe to confirm
                    hash_fh.write(f'{h}|{filepath}\n')
                    mark_done(filepath)
            except Exception as e:
                log(f'  ERR   {os.path.basename(filepath)}: {e}', 'err')
                errors += 1
                try:
                    if tmp_path and os.path.exists(tmp_path):
                        os.unlink(tmp_path)
                except Exception:
                    pass
                # not marked done — will retry on resume

        # ── Single-pass walk — documents and photos in one traversal ─────────
        def walk_all():
            log('── Scanning ────────────────────────────────────────────────', 'head')
            self._set_status('> scanning drive...')
            count = 0
            for root, dirs, files in os.walk(source_path):
                if not self._running:
                    break
                dirs[:] = sorted([
                    d for d in dirs
                    if not d.startswith('.') and d.lower() not in SKIP_DIRS
                ])
                for f in files:
                    if not self._running:
                        break
                    ext = Path(f).suffix.lstrip('.').lower()
                    if ext in DOCUMENT_EXTS:
                        process_file(os.path.join(root, f), docs_dir)
                        count += 1
                    elif ext in IMAGE_EXTS:
                        process_file(os.path.join(root, f), photos_dir)
                        count += 1
                    if count % 10 == 0 and count > 0:
                        self._set_status(
                            f'> {count:,} seen  |  {copied} copied  |  '
                            f'{skipped_dupe} dupes  |  {skipped_resume} resumed  |  {errors} errors'
                        )

        # ── Header ────────────────────────────────────────────────────────────
        if use_gdrive and use_rclone:
            mode_label = f'Google Drive via rclone  ({BATCH_LIMIT_GB} GB batches → {rclone_remote}:{gd_subfolder})'
        elif use_gdrive:
            mode_label = f'Google Drive  ({BATCH_LIMIT_GB} GB batches, legacy mode)'
        else:
            mode_label = 'Local folder'
        resume_label = f'RESUME  ({len(processed_paths):,} files already done)' if resume else 'NEW RUN'
        log(f'Source : {source_path}', 'info')
        log(f'Output : {output_path}', 'info')
        log(f'Mode   : {mode_label}', 'info')
        log(f'Session: {resume_label}', 'info')
        log(f'Hashes : {len(seen_hashes):,} loaded from previous runs  (~/.shinigami_eyes/)', 'info')
        log(f'Started: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}\n', 'info')

        # ── rclone sanity check ───────────────────────────────────────────────
        if use_rclone:
            log('── Checking rclone connection ──────────────────────────────', 'head')
            self._set_status('> verifying rclone auth...')
            try:
                result = subprocess.run(
                    [self._rclone_path, 'about', f'{rclone_remote}:'],
                    capture_output=True, text=True, timeout=60,
                )
                if result.returncode != 0:
                    log(f'  ERR  rclone auth check failed:', 'err')
                    log(f'  {result.stderr.strip()[:300]}', 'err')
                    log('  Fix: run  rclone config reconnect gdrive:  then try again.', 'err')
                    return
                log(f'  rclone OK  — authenticated to {rclone_remote}:\n', 'ok')
            except subprocess.TimeoutExpired:
                log('  ERR  rclone timed out — check your network connection.', 'err')
                return
            except Exception as e:
                log(f'  ERR  rclone check failed: {e}', 'err')
                return

        # ── Run ───────────────────────────────────────────────────────────────
        walk_all()

        if use_gdrive and self._running:
            flush_batch()

        # ── Close persistent files ────────────────────────────────────────────
        try:
            hash_fh.close()
            progress_fh.close()
        except Exception:
            pass

        # ── Summary ───────────────────────────────────────────────────────────
        stopped_early = not self._running
        status = '⚠  Stopped early' if stopped_early else '✓  Complete'

        log('\n' + '─' * 52, 'head')
        log(f'  {status}', 'head')
        log(f'  Files copied         : {copied}', 'head')
        log(f'  Duplicates skipped   : {skipped_dupe}', 'head')
        log(f'  Resumed (skipped)    : {skipped_resume}', 'head')
        log(f'  System/junk skipped  : {skipped_sys}', 'head')
        log(f'  Errors               : {errors}', 'head')
        if use_gdrive:
            log(f'  Batches sent to GDrive: {batches}', 'head')
        log(f'  Total hashes on file : {len(seen_hashes):,}', 'head')
        log('─' * 52, 'head')
        if stopped_early:
            log('  Restart and choose RESUME to continue where this stopped.', 'info')

        self._set_status(
            f'> {status.lower()} — {copied} copied  |  {skipped_dupe} dupes  |  {errors} errors'
        )

        # Save session log
        try:
            with open(log_path, 'w') as lf:
                lf.write('\n'.join(log_lines))
        except Exception:
            pass

        # Clean up staging
        if use_gdrive:
            shutil.rmtree(stage_dir, ignore_errors=True)


# ══════════════════════════════════════════════════════════════════════════════

if __name__ == '__main__':
    app = App()
    app.mainloop()
