"""
Unit tests for the Backblaze B2 -> local NAS import feature
(B2ImportWorker, list_b2_entries, download_b2_file, categorize_for_b2_import).

There's no real B2 bucket available in CI, so these tests spawn a tiny fake
'rclone' executable that responds to the exact subcommands the import
worker calls (lsjson, lsd, copyto) using a JSON fixture that mirrors real
`rclone lsjson` output. This exercises the full, real
B2ImportWorker._migrate() control flow — including the HashRegistry it
shares with physical-drive and Google Drive imports — without any network
access or real credentials.

Mirrors tests/test_gdrive_import.py's structure. One thing this feature
gets right from the start that the Google Drive import feature initially
got wrong in production: the fake rclone records the exact remote path
`download_b2_file()` invokes 'copyto' with, so a regression that drops the
prefix (bucket/prefix) — the exact bug found in the real gdrive-import
rollout — would be caught here immediately (see
TestDownloadB2File.test_remote_path_is_prefixed_with_bucket_and_prefix).

tkinter is stubbed the same way as test_app_logic.py/test_gdrive_import.py,
so this suite runs headless. Run with:

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
_MODULE_NAME = 'nas_migrate_gui_b2_test'

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
                 'PhotoImage', 'Toplevel', 'Checkbutton'):
        setattr(fake_tk, attr, _Dummy)
    fake_tk.VERTICAL = 'vertical'
    sys.modules['tkinter'] = fake_tk

    fake_ttk = types.ModuleType('tkinter.ttk')
    fake_ttk.Scrollbar = _Dummy
    fake_ttk.Separator = _Dummy
    fake_ttk.Spinbox = _Dummy
    sys.modules['tkinter.ttk'] = fake_ttk

    sys.modules['tkinter.filedialog'] = types.ModuleType('tkinter.filedialog')

    fake_msgbox = types.ModuleType('tkinter.messagebox')
    fake_msgbox.showerror = lambda *a, **k: None
    fake_msgbox.showinfo = lambda *a, **k: None
    fake_msgbox.showwarning = lambda *a, **k: None
    fake_msgbox.askyesno = lambda *a, **k: False
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

    if cmd == 'copyto':
        src = args[1]
        dest_path = args[2]

        last_copy_src_path = os.environ.get('FAKE_RCLONE_LAST_COPY_SRC')
        if last_copy_src_path:
            with open(last_copy_src_path, 'w') as f:
                f.write(src)

        # Deterministic content keyed off the source path, so re-downloading
        # the same object always hashes identically.
        content = hashlib.sha256(src.encode()).hexdigest().encode() * 50

        os.makedirs(os.path.dirname(dest_path), exist_ok=True)
        with open(dest_path, 'wb') as f:
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
        self._tmp = tempfile.mkdtemp(prefix='se_test_b2_')
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

    def _set_fixture(self, entries: list):
        with open(self._fixture_path, 'w') as f:
            json.dump(entries, f)

    def _last_copy_src(self) -> str:
        with open(self._last_copy_src_path) as f:
            return f.read()

    def _make_registry(self):
        reg = app_mod.HashRegistry(app_mod.HASH_DB_FILE)
        reg.open()
        return reg


class TestCategorizeForB2Import(unittest.TestCase):
    def test_known_extensions_map_to_expected_category(self):
        self.assertEqual(app_mod.categorize_for_b2_import('a.jpg'), 'Photos')
        self.assertEqual(app_mod.categorize_for_b2_import('a.pdf'), 'Documents')
        self.assertEqual(app_mod.categorize_for_b2_import('a.mp4'), 'Videos')
        self.assertEqual(app_mod.categorize_for_b2_import('a.mp3'), 'Audio')
        self.assertEqual(app_mod.categorize_for_b2_import('a.zip'), 'Archives')

    def test_unrecognized_extension_returns_empty_string(self):
        self.assertEqual(app_mod.categorize_for_b2_import('a.exe'), '')
        self.assertEqual(app_mod.categorize_for_b2_import('a.dll'), '')

    def test_case_insensitive(self):
        self.assertEqual(app_mod.categorize_for_b2_import('A.JPG'), 'Photos')

    def test_categories_do_not_overlap(self):
        # Every extension should belong to exactly one category — a file
        # landing in the wrong folder, or being ambiguous, would be a bug.
        seen = {}
        for category, exts in app_mod.B2_IMPORT_CATEGORIES.items():
            for ext in exts:
                self.assertNotIn(ext, seen,
                    f'{ext} appears in both {seen.get(ext)} and {category}')
                seen[ext] = category


class TestListB2Entries(_FakeRcloneMixin):
    def test_parses_entries(self):
        self._set_fixture([
            {"Path": "a/photo.jpg", "Name": "photo.jpg", "Size": 500000},
            {"Path": "b/doc.pdf", "Name": "doc.pdf", "Size": 20000},
        ])
        entries = app_mod.list_b2_entries(
            self.rclone_path, 'kid', 'key', 'mybucket', 'Folder')
        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[0]['path'], 'a/photo.jpg')
        self.assertEqual(entries[0]['size'], 500000)

    def test_returns_none_on_lsjson_failure(self):
        bad_rclone = os.path.join(self._tmp, 'bad_rclone.sh')
        with open(bad_rclone, 'w') as f:
            f.write('#!/bin/sh\nexit 1\n')
        os.chmod(bad_rclone, os.stat(bad_rclone).st_mode | stat.S_IEXEC)
        result = app_mod.list_b2_entries(bad_rclone, 'kid', 'key', 'mybucket', 'Folder')
        self.assertIsNone(result)


class TestDownloadB2File(_FakeRcloneMixin):
    def test_regular_file_download(self):
        entry = {'path': 'x/beach.jpg', 'name': 'beach.jpg', 'size': 1000}
        h, tmp, written, name = app_mod.download_b2_file(
            self.rclone_path, 'kid', 'key', 'mybucket', 'Folder', entry, self._tmp)
        try:
            self.assertTrue(os.path.exists(tmp))
            self.assertGreater(written, 0)
            self.assertEqual(name, 'beach.jpg')
            self.assertEqual(len(h), 32)   # md5 hexdigest length
        finally:
            shutil.rmtree(os.path.dirname(tmp), ignore_errors=True)

    def test_remote_path_is_prefixed_with_bucket_and_prefix(self):
        # Regression coverage for the exact class of bug the Google Drive
        # import feature shipped with: entry['path'] from list_b2_entries()
        # is relative to the queried prefix, not the bucket root. Dropping
        # the prefix here would make every download fail against a real B2
        # bucket with 'directory not found', the way gdrive-import did.
        entry = {'path': 'x/beach.jpg', 'name': 'beach.jpg', 'size': 1000}
        _, tmp, _, _ = app_mod.download_b2_file(
            self.rclone_path, 'kid', 'key', 'mybucket', 'NAS Migration', entry, self._tmp)
        shutil.rmtree(os.path.dirname(tmp), ignore_errors=True)
        self.assertEqual(self._last_copy_src(),
                         ':b2:mybucket/NAS Migration/x/beach.jpg')

    def test_remote_path_with_no_prefix(self):
        entry = {'path': 'beach.jpg', 'name': 'beach.jpg', 'size': 1000}
        _, tmp, _, _ = app_mod.download_b2_file(
            self.rclone_path, 'kid', 'key', 'mybucket', '', entry, self._tmp)
        shutil.rmtree(os.path.dirname(tmp), ignore_errors=True)
        self.assertEqual(self._last_copy_src(), ':b2:mybucket/beach.jpg')

    def test_deterministic_content_hash(self):
        entry = {'path': 'x/report.pdf', 'name': 'report.pdf', 'size': 500}
        h1, tmp1, _, _ = app_mod.download_b2_file(
            self.rclone_path, 'kid', 'key', 'mybucket', 'F', entry, self._tmp)
        shutil.rmtree(os.path.dirname(tmp1), ignore_errors=True)
        h2, tmp2, _, _ = app_mod.download_b2_file(
            self.rclone_path, 'kid', 'key', 'mybucket', 'F', entry, self._tmp)
        shutil.rmtree(os.path.dirname(tmp2), ignore_errors=True)
        self.assertEqual(h1, h2)


class TestB2ImportWorkerEndToEnd(_FakeRcloneMixin):
    """Drives the real B2ImportWorker._migrate() through every branch:
    new file, already-known dupe (caught only after download, since B2's
    remote hash is SHA1 not MD5), category routing, file-type filtering,
    junk extension, unrecognized extension, and resume."""

    @property
    def ALL_EXTENSIONS(self):
        return frozenset(
            ext for exts in app_mod.B2_IMPORT_CATEGORIES.values() for ext in exts)

    def _run_worker(self, registry, bucket='mybucket', prefix='F',
                    output_path=None, extensions=None, resume=False):
        output_path = output_path or tempfile.mkdtemp(dir=self._tmp)
        info = app_mod.DriveInfo(path=f'b2-import://{bucket}:{prefix}',
                                 label=f'B2: {prefix}')
        worker = app_mod.B2ImportWorker(
            bucket=bucket, prefix=prefix, output_path=output_path, info=info,
            registry=registry, rclone_path=self.rclone_path,
            b2_key_id='kid', b2_app_key='key',
            extensions=extensions if extensions is not None else self.ALL_EXTENSIONS,
            log_fn=lambda msg, tag='': None,
            running_ref=lambda: True,
            resume=resume,
        )
        worker._run()
        return worker, output_path

    def test_full_pipeline_all_branches(self):
        self._set_fixture([
            # new regular file — should download + land in Photos/
            {"Path": "photos/beach.jpg", "Name": "beach.jpg", "Size": 500000},
            # new document — should land in Documents/
            {"Path": "docs/report.pdf", "Name": "report.pdf", "Size": 20000},
            # junk extension — must skip via should_skip
            {"Path": "junk/thumbs.log", "Name": "thumbs.log", "Size": 100},
            # unrecognized extension — must skip
            {"Path": "misc/data.xyz123", "Name": "data.xyz123", "Size": 9000},
        ])
        registry = self._make_registry()
        worker, output_path = self._run_worker(registry)

        s = worker.stats
        self.assertEqual(s.status, 'done', s.fatal)
        self.assertEqual(s.copied, 2, 'expected beach.jpg + report.pdf')
        self.assertEqual(s.skipped_sys, 2, 'junk ext + unrecognized ext')
        self.assertEqual(s.errors, 0)

        self.assertTrue(os.path.exists(os.path.join(output_path, 'Photos')))
        self.assertTrue(os.path.exists(os.path.join(output_path, 'Documents')))
        photos = os.listdir(os.path.join(output_path, 'Photos'))
        docs = os.listdir(os.path.join(output_path, 'Documents'))
        self.assertEqual(len(photos), 1)
        self.assertEqual(len(docs), 1)
        self.assertTrue(photos[0].endswith('beach.jpg'))
        self.assertTrue(docs[0].endswith('report.pdf'))

    def test_duplicate_content_caught_after_download(self):
        # B2 has no pre-known MD5 to fast-path against (see download_b2_file
        # docstring) — dedup can only happen after the file is downloaded
        # and hashed locally. Preseed the registry with exactly the hash
        # the fake rclone will deterministically produce for this path.
        self._set_fixture([
            {"Path": "photos/beach.jpg", "Name": "beach.jpg", "Size": 500000},
        ])
        entries = app_mod.list_b2_entries(self.rclone_path, 'kid', 'key', 'mybucket', 'F')
        h_expected, tmp, _, _ = app_mod.download_b2_file(
            self.rclone_path, 'kid', 'key', 'mybucket', 'F', entries[0], self._tmp)
        shutil.rmtree(os.path.dirname(tmp), ignore_errors=True)

        registry = self._make_registry()
        registry.add(h_expected, 'already/migrated/somewhere.jpg')
        registry.flush()

        worker, output_path = self._run_worker(registry)
        self.assertEqual(worker.stats.copied, 0)
        self.assertEqual(worker.stats.skipped_dupe, 1)
        self.assertEqual(os.listdir(os.path.join(output_path, 'Photos')), [])

    def test_file_type_filter_excludes_deselected_extensions(self):
        self._set_fixture([
            {"Path": "photos/beach.jpg", "Name": "beach.jpg", "Size": 500000},
            {"Path": "docs/report.pdf", "Name": "report.pdf", "Size": 20000},
        ])
        registry = self._make_registry()
        # Only Documents selected — the Photos file must be filtered out
        # even though it's a perfectly recognized, valid category.
        doc_exts = frozenset(app_mod.B2_IMPORT_CATEGORIES['Documents'])
        worker, output_path = self._run_worker(registry, extensions=doc_exts)

        self.assertEqual(worker.stats.copied, 1)
        self.assertEqual(len(os.listdir(os.path.join(output_path, 'Documents'))), 1)
        # Photos wasn't in the filter at all, so its folder shouldn't even
        # be created — no point leaving empty category folders on the NAS.
        self.assertFalse(os.path.exists(os.path.join(output_path, 'Photos')))

    def test_resume_skips_already_processed_entries(self):
        self._set_fixture([
            {"Path": "photos/beach.jpg", "Name": "beach.jpg", "Size": 500000},
            {"Path": "docs/report.pdf", "Name": "report.pdf", "Size": 20000},
        ])
        registry = self._make_registry()
        output_path = tempfile.mkdtemp(dir=self._tmp)

        info = app_mod.DriveInfo(path='b2-import://mybucket:F', label='B2: F')
        progress_path = app_mod.progress_file_for(
            output_path, f'b2-import://mybucket:F')
        progress_path.parent.mkdir(parents=True, exist_ok=True)
        with open(progress_path, 'w') as f:
            f.write('photos/beach.jpg\n')

        worker, _ = self._run_worker(registry, output_path=output_path, resume=True)
        self.assertEqual(worker.stats.skipped_resume, 1)
        self.assertEqual(worker.stats.copied, 1, 'only report.pdf should be new')

    def test_lsjson_failure_marks_worker_as_error(self):
        os.environ['FAKE_RCLONE_LSD_OK'] = '0'
        bad_rclone = os.path.join(self._tmp, 'bad_rclone.sh')
        with open(bad_rclone, 'w') as f:
            f.write('#!/bin/sh\nexit 1\n')
        os.chmod(bad_rclone, os.stat(bad_rclone).st_mode | stat.S_IEXEC)

        registry = self._make_registry()
        info = app_mod.DriveInfo(path='b2-import://mybucket:F', label='B2: F')
        output_path = tempfile.mkdtemp(dir=self._tmp)
        worker = app_mod.B2ImportWorker(
            bucket='mybucket', prefix='F', output_path=output_path, info=info,
            registry=registry, rclone_path=bad_rclone,
            b2_key_id='kid', b2_app_key='key',
            extensions=self.ALL_EXTENSIONS,
            log_fn=lambda msg, tag='': None,
            running_ref=lambda: True,
        )
        worker._run()
        # A listing failure is reported via stats.fatal but doesn't raise —
        # _run()'s try/except would also catch it if it did (same nuance as
        # GDriveImportWorker's equivalent test).
        self.assertTrue(worker.stats.fatal or worker.stats.status == 'error')
        self.assertIn('Could not list B2 bucket', worker.stats.fatal)


class TestDriveInfoB2ImportFields(unittest.TestCase):
    def test_extra_fields_default_empty(self):
        info = app_mod.DriveInfo(path='/some/drive')
        self.assertEqual(info.b2_import_key_id, '')
        self.assertEqual(info.b2_import_extensions, '')

    def test_extensions_round_trip_through_comma_join(self):
        exts = frozenset({'.jpg', '.pdf', '.mp3'})
        joined = ','.join(sorted(exts))
        info = app_mod.DriveInfo(path='b2-import://bucket:prefix',
                                 b2_import_extensions=joined)
        recovered = frozenset(e for e in info.b2_import_extensions.split(',') if e)
        self.assertEqual(recovered, exts)


if __name__ == '__main__':
    unittest.main()
