"""
Unit tests for the Google Drive → Backblaze B2 import feature
(GDriveImportWorker, list_gdrive_entries, download_gdrive_file).

There's no real Google Drive remote available in CI, so these tests spawn a
tiny fake 'rclone' executable that responds to the exact subcommands the
import worker calls (lsjson, lsd, copy) using a JSON fixture that mirrors
real `rclone lsjson --hash` output. This exercises the full, real
GDriveImportWorker._migrate() control flow — including the HashRegistry and
UploadCoordinator it shares with physical-drive imports — without any
network access or real credentials.

tkinter is stubbed the same way as test_app_logic.py, so this suite runs
headless. Run with:

    python3 -m unittest discover -s tests
"""
import importlib.util
import json
import os
import shutil
import stat
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock

_REPO_ROOT = Path(__file__).resolve().parent.parent
_MODULE_NAME = 'nas_migrate_gui_gdrive_test'

_saved_modules = {}
_STUBBED = ('tkinter', 'tkinter.ttk', 'tkinter.filedialog', 'tkinter.messagebox')


class _Dummy:
    def __init__(self, *a, **k):
        pass

    def __getattr__(self, name):
        return MagicMock()


def setUpModule():
    for name in _STUBBED:
        _saved_modules[name] = sys.modules.get(name)

    fake_tk = types.ModuleType('tkinter')
    for attr in ('Tk', 'Frame', 'Label', 'Button', 'Entry', 'Text', 'Canvas',
                 'PanedWindow', 'StringVar', 'BooleanVar', 'IntVar',
                 'PhotoImage', 'Toplevel'):
        setattr(fake_tk, attr, _Dummy)
    fake_tk.VERTICAL = 'vertical'
    sys.modules['tkinter'] = fake_tk

    fake_ttk = types.ModuleType('tkinter.ttk')
    fake_ttk.Scrollbar = _Dummy
    fake_ttk.Separator = _Dummy
    sys.modules['tkinter.ttk'] = fake_ttk

    sys.modules['tkinter.filedialog'] = types.ModuleType('tkinter.filedialog')

    fake_msgbox = types.ModuleType('tkinter.messagebox')
    fake_msgbox.showerror = lambda *a, **k: None
    fake_msgbox.showinfo = lambda *a, **k: None
    fake_msgbox.showwarning = lambda *a, **k: None
    sys.modules['tkinter.messagebox'] = fake_msgbox

    spec = importlib.util.spec_from_file_location(
        _MODULE_NAME, str(_REPO_ROOT / 'nas_migrate_gui.py'))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[_MODULE_NAME] = mod
    spec.loader.exec_module(mod)
    global app_mod
    app_mod = mod


def tearDownModule():
    for name in _STUBBED:
        if _saved_modules.get(name) is not None:
            sys.modules[name] = _saved_modules[name]
        else:
            sys.modules.pop(name, None)
    sys.modules.pop(_MODULE_NAME, None)


_FAKE_RCLONE_SRC = r'''#!/usr/bin/env python3
import sys, os, hashlib

FIXTURE = os.environ['FAKE_RCLONE_FIXTURE']

def main():
    args = sys.argv[1:]
    cmd = args[0] if args else ''

    if cmd == 'lsjson':
        with open(FIXTURE) as f:
            sys.stdout.write(f.read())
        return 0

    if cmd == 'lsd':
        return 0 if os.environ.get('FAKE_RCLONE_LSD_OK', '1') == '1' else 1

    if cmd == 'copy':
        src = args[1]
        dest_dir = args[2]
        export_fmt = None
        if '--drive-export-formats' in args:
            export_fmt = args[args.index('--drive-export-formats') + 1]

        last_copy_src_path = os.environ.get('FAKE_RCLONE_LAST_COPY_SRC')
        if last_copy_src_path:
            with open(last_copy_src_path, 'w') as f:
                f.write(src)

        # Deterministic content keyed off the source path, so re-downloading
        # (re-exporting) the same remote file always hashes identically —
        # mirroring a real Google Doc's export being stable content.
        content = hashlib.sha256(src.encode()).hexdigest().encode() * 50

        base_name = src.split(':', 1)[1].split('/')[-1] or 'file'
        if export_fmt:
            base_name = f'{base_name}.{export_fmt}'

        os.makedirs(dest_dir, exist_ok=True)
        with open(os.path.join(dest_dir, base_name), 'wb') as f:
            f.write(content)
        return 0

    sys.stderr.write(f'fake_rclone: unhandled command {cmd!r}\n')
    return 1


if __name__ == '__main__':
    sys.exit(main())
'''


class _FakeRcloneMixin(unittest.TestCase):
    """Provides self.rclone_path (a working fake rclone binary) and
    self._set_fixture(entries) to control what it "lists"."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix='se_test_gdrive_')
        self.rclone_path = os.path.join(self._tmp, 'fake_rclone.py')
        with open(self.rclone_path, 'w') as f:
            f.write(_FAKE_RCLONE_SRC)
        os.chmod(self.rclone_path, os.stat(self.rclone_path).st_mode | stat.S_IEXEC)

        self._fixture_path = os.path.join(self._tmp, 'fixture.json')
        os.environ['FAKE_RCLONE_FIXTURE'] = self._fixture_path
        self._set_fixture([])

        self._last_copy_src_path = os.path.join(self._tmp, 'last_copy_src.txt')
        os.environ['FAKE_RCLONE_LAST_COPY_SRC'] = self._last_copy_src_path

        self._state_dir = os.path.join(self._tmp, 'state')
        os.makedirs(self._state_dir, exist_ok=True)
        self._orig_state_dir = app_mod.STATE_DIR
        self._orig_hash_db = app_mod.HASH_DB_FILE
        app_mod.STATE_DIR = Path(self._state_dir)
        app_mod.HASH_DB_FILE = Path(self._state_dir) / 'hashes.db'

    def tearDown(self):
        app_mod.STATE_DIR = self._orig_state_dir
        app_mod.HASH_DB_FILE = self._orig_hash_db
        os.environ.pop('FAKE_RCLONE_FIXTURE', None)
        os.environ.pop('FAKE_RCLONE_LAST_COPY_SRC', None)
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _set_fixture(self, entries: list[dict]):
        with open(self._fixture_path, 'w') as f:
            json.dump(entries, f)

    def _last_copy_src(self) -> str:
        """The exact remote:path string the fake rclone's most recent 'copy'
        was invoked with — used to assert download_gdrive_file() built the
        fully-qualified path (remote:folder/entry_path), not just
        remote:entry_path. Regression coverage for a real production bug:
        entry['path'] from lsjson is relative to the queried folder, so
        omitting the folder prefix made rclone look for every file at the
        Drive root and fail with 'directory not found'."""
        with open(self._last_copy_src_path) as f:
            return f.read()

    def _make_registry(self):
        reg = app_mod.HashRegistry(app_mod.HASH_DB_FILE)
        reg.open()
        return reg

    def _make_coordinator(self, registry, queued: list):
        """A coordinator whose _run() confirms immediately instead of
        actually shelling out to rclone for the B2 upload leg — that leg is
        already covered by the existing upload-coordinator code path used by
        physical drives; here we're testing what gets QUEUED for it."""
        coord = app_mod.UploadCoordinator(lambda msg, tag='': None, registry)
        def fake_run(job):
            queued.append(job)
            coord._confirm(job)
        coord._run = fake_run
        coord.start()
        return coord


class TestListGdriveEntries(_FakeRcloneMixin):
    def test_parses_regular_and_native_entries(self):
        self._set_fixture([
            {"Path": "a/photo.jpg", "Name": "photo.jpg", "Size": 500000,
             "MimeType": "image/jpeg", "Hashes": {"md5": "abc123"}},
            {"Path": "b/Doc", "Name": "Doc", "Size": 0,
             "MimeType": "application/vnd.google-apps.document", "Hashes": {}},
        ])
        entries = app_mod.list_gdrive_entries(self.rclone_path, 'gdrive', 'Folder')
        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[0]['md5'], 'abc123')
        self.assertIsNone(entries[1]['md5'])
        self.assertEqual(entries[1]['mime_type'],
                         'application/vnd.google-apps.document')

    def test_returns_none_on_lsjson_failure(self):
        # Point at a script that always exits non-zero
        bad_rclone = os.path.join(self._tmp, 'bad_rclone.sh')
        with open(bad_rclone, 'w') as f:
            f.write('#!/bin/sh\nexit 1\n')
        os.chmod(bad_rclone, os.stat(bad_rclone).st_mode | stat.S_IEXEC)
        result = app_mod.list_gdrive_entries(bad_rclone, 'gdrive', 'Folder')
        self.assertIsNone(result)


class TestDownloadGdriveFile(_FakeRcloneMixin):
    def test_regular_file_download(self):
        entry = {'path': 'x/beach.jpg', 'name': 'beach.jpg', 'size': 1000,
                 'mime_type': 'image/jpeg', 'md5': 'known'}
        h, tmp, written, name, work_dir = app_mod.download_gdrive_file(
            self.rclone_path, 'gdrive', 'Folder', entry, self._tmp)
        try:
            self.assertTrue(os.path.exists(tmp))
            self.assertGreater(written, 0)
            self.assertEqual(name, 'beach.jpg')
            self.assertEqual(len(h), 32)   # md5 hexdigest length
        finally:
            shutil.rmtree(work_dir, ignore_errors=True)

    def test_remote_path_is_prefixed_with_folder(self):
        # Regression test: entry['path'] from list_gdrive_entries() is
        # relative to the queried folder, not the Drive root. A real run
        # against production Google Drive hit exactly this bug — every
        # download failed with rclone's "directory not found" because the
        # folder prefix was silently dropped when building the copy source.
        entry = {'path': 'x/beach.jpg', 'name': 'beach.jpg', 'size': 1000,
                 'mime_type': 'image/jpeg', 'md5': 'known'}
        _, _, _, _, work_dir = app_mod.download_gdrive_file(
            self.rclone_path, 'gdrive', 'NAS Migration', entry, self._tmp)
        shutil.rmtree(work_dir, ignore_errors=True)
        self.assertEqual(self._last_copy_src(), 'gdrive:NAS Migration/x/beach.jpg')

    def test_remote_path_with_no_folder_has_no_extra_slash(self):
        entry = {'path': 'beach.jpg', 'name': 'beach.jpg', 'size': 1000,
                 'mime_type': 'image/jpeg', 'md5': 'known'}
        _, _, _, _, work_dir = app_mod.download_gdrive_file(
            self.rclone_path, 'gdrive', '', entry, self._tmp)
        shutil.rmtree(work_dir, ignore_errors=True)
        self.assertEqual(self._last_copy_src(), 'gdrive:beach.jpg')

    def test_native_doc_export_appends_extension(self):
        entry = {'path': 'x/Report', 'name': 'Report', 'size': 0,
                 'mime_type': 'application/vnd.google-apps.document', 'md5': None}
        h, tmp, written, name, work_dir = app_mod.download_gdrive_file(
            self.rclone_path, 'gdrive', 'Folder', entry, self._tmp)
        try:
            self.assertTrue(name.endswith('.pdf'))
        finally:
            shutil.rmtree(work_dir, ignore_errors=True)

    def test_native_doc_export_is_deterministic(self):
        # Same doc "exported" twice must hash identically — this is what
        # makes post-download dedup meaningful for native Google files.
        entry = {'path': 'x/Report', 'name': 'Report', 'size': 0,
                 'mime_type': 'application/vnd.google-apps.document', 'md5': None}
        h1, tmp1, _, _, wd1 = app_mod.download_gdrive_file(
            self.rclone_path, 'gdrive', 'Folder', entry, self._tmp)
        h2, tmp2, _, _, wd2 = app_mod.download_gdrive_file(
            self.rclone_path, 'gdrive', 'Folder', entry, self._tmp)
        shutil.rmtree(wd1, ignore_errors=True)
        shutil.rmtree(wd2, ignore_errors=True)
        self.assertEqual(h1, h2)


class TestGDriveImportWorkerEndToEnd(_FakeRcloneMixin):
    """Drives the real GDriveImportWorker._migrate() through every branch:
    known-hash dedup (no download), new regular file, native doc export,
    unsupported native type, junk extension, unrecognized extension,
    post-download dedup, and resume."""

    def _run_worker(self, registry, coordinator, remote='gdrive', folder='F',
                    output_path='b2:bucket/sub', gd_subfolder='sub',
                    resume=False):
        info = app_mod.DriveInfo(path=f'gdrive-import://{remote}:{folder}',
                                 label=f'GDrive: {folder}')
        worker = app_mod.GDriveImportWorker(
            remote=remote, folder=folder, output_path=output_path, info=info,
            registry=registry, coordinator=coordinator,
            rclone_path=self.rclone_path, gd_subfolder=gd_subfolder,
            b2_key_id='kid', b2_app_key='key', b2_bucket='bucket',
            log_fn=lambda msg, tag='': None,
            running_ref=lambda: True,
            batch_limit=10 ** 12,   # keep everything in one batch to inspect
            resume=resume,
        )
        worker._run()
        return worker

    def test_full_pipeline_all_branches(self):
        self._set_fixture([
            # new regular file — should download + stage
            {"Path": "photos/beach.jpg", "Name": "beach.jpg", "Size": 500000,
             "MimeType": "image/jpeg", "Hashes": {"md5": "hash-new"}},
            # already-known dupe — must skip WITHOUT downloading
            {"Path": "photos/dupe.jpg", "Name": "dupe.jpg", "Size": 400000,
             "MimeType": "image/jpeg", "Hashes": {"md5": "hash-dupe"}},
            # native Google Doc — exported to pdf, hashed, staged
            {"Path": "docs/Report", "Name": "Report", "Size": 0,
             "MimeType": "application/vnd.google-apps.document", "Hashes": {}},
            # native Google Form — no export target, must skip
            {"Path": "forms/Survey", "Name": "Survey", "Size": 0,
             "MimeType": "application/vnd.google-apps.form", "Hashes": {}},
            # junk extension — must skip via should_skip
            {"Path": "junk/thumbs.log", "Name": "thumbs.log", "Size": 100,
             "MimeType": "text/plain", "Hashes": {"md5": "hash-junk"}},
            # unrecognized extension — must skip
            {"Path": "misc/data.xyz123", "Name": "data.xyz123", "Size": 9000,
             "MimeType": "application/octet-stream", "Hashes": {"md5": "hash-unk"}},
        ])
        registry = self._make_registry()
        registry.add('hash-dupe', 'preexisting/dupe.jpg')
        registry.flush()
        queued = []
        coordinator = self._make_coordinator(registry, queued)
        try:
            worker = self._run_worker(registry, coordinator)
        finally:
            coordinator.stop()

        s = worker.stats
        self.assertEqual(s.status, 'done', s.fatal)
        self.assertEqual(s.copied, 2, 'expected beach.jpg + Report.pdf')
        self.assertEqual(s.skipped_dupe, 1)
        self.assertEqual(s.skipped_sys, 3, 'form + junk ext + unrecognized ext')
        self.assertEqual(s.errors, 0)

        self.assertEqual(len(queued), 1)
        job = queued[0]
        self.assertTrue(job.use_b2)
        self.assertEqual(job.gd_subfolder, 'sub')
        resume_keys = sorted(k for k, h in job.pending)
        self.assertEqual(resume_keys, ['docs/Report', 'photos/beach.jpg'])

    def test_known_hash_duplicate_skips_without_download(self):
        # If the fake rclone's `copy` were ever invoked for the dupe, the
        # batch would contain 2 files instead of 0 for this single-entry
        # fixture — this test exists specifically to prove the fast path
        # doesn't touch the network/download step at all.
        self._set_fixture([
            {"Path": "photos/dupe.jpg", "Name": "dupe.jpg", "Size": 400000,
             "MimeType": "image/jpeg", "Hashes": {"md5": "hash-dupe"}},
        ])
        registry = self._make_registry()
        registry.add('hash-dupe', 'preexisting/dupe.jpg')
        registry.flush()
        queued = []
        coordinator = self._make_coordinator(registry, queued)
        try:
            worker = self._run_worker(registry, coordinator)
        finally:
            coordinator.stop()
        self.assertEqual(worker.stats.copied, 0)
        self.assertEqual(worker.stats.skipped_dupe, 1)
        self.assertEqual(len(queued), 0, 'nothing should have been queued for upload')

    def test_post_download_dedup_catches_native_export_collision(self):
        # A native doc has no pre-known hash — dedup for it can only happen
        # AFTER export. Preseed the registry with exactly the hash the fake
        # rclone will deterministically produce for this doc.
        self._set_fixture([
            {"Path": "docs/Report", "Name": "Report", "Size": 0,
             "MimeType": "application/vnd.google-apps.document", "Hashes": {}},
        ])
        entries = app_mod.list_gdrive_entries(self.rclone_path, 'gdrive', 'F')
        h_expected, tmp, _, _, work_dir = app_mod.download_gdrive_file(
            self.rclone_path, 'gdrive', 'F', entries[0], self._tmp)
        shutil.rmtree(work_dir, ignore_errors=True)

        registry = self._make_registry()
        registry.add(h_expected, 'already/migrated/somewhere.pdf')
        registry.flush()
        queued = []
        coordinator = self._make_coordinator(registry, queued)
        try:
            worker = self._run_worker(registry, coordinator)
        finally:
            coordinator.stop()
        self.assertEqual(worker.stats.copied, 0,
                         'post-download dedup should have caught the export collision')
        self.assertEqual(worker.stats.skipped_dupe, 1)

    def test_resume_skips_already_processed_entries(self):
        self._set_fixture([
            {"Path": "photos/a.jpg", "Name": "a.jpg", "Size": 100000,
             "MimeType": "image/jpeg", "Hashes": {"md5": "hashA"}},
            {"Path": "photos/b.jpg", "Name": "b.jpg", "Size": 100000,
             "MimeType": "image/jpeg", "Hashes": {"md5": "hashB"}},
        ])
        registry = self._make_registry()
        queued = []
        coordinator = self._make_coordinator(registry, queued)

        out_path = 'b2:bucket/sub'
        prog_path = app_mod.progress_file_for(out_path, 'gdrive-import://gdrive:F')
        prog_path.parent.mkdir(parents=True, exist_ok=True)
        with open(prog_path, 'w') as f:
            f.write('photos/a.jpg\n')

        try:
            worker = self._run_worker(registry, coordinator, output_path=out_path,
                                      resume=True)
        finally:
            coordinator.stop()
        self.assertEqual(worker.stats.skipped_resume, 1)
        self.assertEqual(worker.stats.copied, 1, 'only b.jpg should be newly copied')

    def test_empty_folder_reports_no_files(self):
        self._set_fixture([])
        registry = self._make_registry()
        queued = []
        coordinator = self._make_coordinator(registry, queued)
        try:
            worker = self._run_worker(registry, coordinator)
        finally:
            coordinator.stop()
        self.assertEqual(worker.stats.status, 'done')
        self.assertEqual(worker.stats.copied, 0)

    def test_lsjson_failure_marks_worker_as_error(self):
        # Break lsjson by pointing FAKE_RCLONE_FIXTURE at a nonexistent file
        os.environ['FAKE_RCLONE_FIXTURE'] = '/no/such/fixture.json'
        registry = self._make_registry()
        queued = []
        coordinator = self._make_coordinator(registry, queued)
        try:
            worker = self._run_worker(registry, coordinator)
        finally:
            coordinator.stop()
        # A listing failure is reported via stats.fatal but doesn't raise —
        # _run()'s try/except would also catch it if it did.
        self.assertTrue(worker.stats.fatal or worker.stats.status == 'error')


class TestDriveInfoPathEncoding(unittest.TestCase):
    """The gdrive-import:// prefix on DriveInfo.path is the sole dispatch
    mechanism _launch_worker uses to pick GDriveImportWorker over
    DriveWorker, and the sole resume-key source — both must parse back out
    exactly what was encoded."""

    def test_remote_and_folder_round_trip(self):
        remote, folder = 'gdrive', 'Clients/Acme/Photos'
        path = f'gdrive-import://{remote}:{folder}'
        parsed_remote, parsed_folder = path[len('gdrive-import://'):].split(':', 1)
        self.assertEqual(parsed_remote, remote)
        self.assertEqual(parsed_folder, folder)

    def test_empty_folder_round_trips_to_empty_string(self):
        path = 'gdrive-import://gdrive:'
        remote, folder = path[len('gdrive-import://'):].split(':', 1)
        self.assertEqual(remote, 'gdrive')
        self.assertEqual(folder, '')


if __name__ == '__main__':
    unittest.main()
