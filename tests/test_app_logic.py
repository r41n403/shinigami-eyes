"""
Unit tests for the hardening added to nas_migrate_gui.py after a real
incident: a second app instance's startup orphan-cleanup deleted a first
instance's in-progress staging folder mid-batch, producing a
'No such file or directory' error for every remaining file in that batch.

Three independent fixes are covered here:
  1. acquire_single_instance_lock() / _pid_is_alive() — the actual fix,
     prevents a second instance from ever starting alongside a live one.
  2. cleanup_orphan_stages() age-gating — defense in depth, so even if two
     instances somehow coexist, a stage dir with recent activity anywhere
     in its tree is left alone.
  3. copy_and_hash() self-healing — if a destination directory still goes
     missing mid-batch for any reason, one file errors and recovers rather
     than every subsequent file in the batch failing.

tkinter is stubbed so this suite runs in headless/CI environments without a
display or a Tk install — real GUI behavior isn't exercised here (that's
what --uicheck in CI covers), only the plain-Python logic above.
"""
import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock

_REPO_ROOT = Path(__file__).resolve().parent.parent
_MODULE_NAME = 'nas_migrate_gui_under_test'

_saved_modules = {}
_STUBBED = ('tkinter', 'tkinter.ttk', 'tkinter.filedialog', 'tkinter.messagebox')


class _Dummy:
    """Stands in for any tkinter widget class — accepts any constructor args,
    and any attribute access returns a further mock, so widget-building code
    in the module executes without error but does nothing."""
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
    fake_msgbox.askyesno = lambda *a, **k: False
    sys.modules['tkinter.messagebox'] = fake_msgbox

    spec = importlib.util.spec_from_file_location(
        _MODULE_NAME, str(_REPO_ROOT / 'nas_migrate_gui.py'))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[_MODULE_NAME] = mod   # must be registered before exec — dataclass
    spec.loader.exec_module(mod)      # decorators look modules up by __module__
    global app_mod
    app_mod = mod


def tearDownModule():
    for name in _STUBBED:
        if _saved_modules.get(name) is not None:
            sys.modules[name] = _saved_modules[name]
        else:
            sys.modules.pop(name, None)
    sys.modules.pop(_MODULE_NAME, None)


class _TempStateDirMixin(unittest.TestCase):
    """Points the module's STATE_DIR/INSTANCE_LOCK_FILE at a scratch temp
    dir for the duration of each test, so tests never touch a real
    ~/.shinigami_eyes/instance.lock that a genuinely running app might hold."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix='se_test_state_')
        self._orig_state_dir = app_mod.STATE_DIR
        self._orig_lock_file = app_mod.INSTANCE_LOCK_FILE
        app_mod.STATE_DIR = Path(self._tmp)
        app_mod.INSTANCE_LOCK_FILE = Path(self._tmp) / 'instance.lock'

    def tearDown(self):
        app_mod.STATE_DIR = self._orig_state_dir
        app_mod.INSTANCE_LOCK_FILE = self._orig_lock_file
        shutil.rmtree(self._tmp, ignore_errors=True)


class TestPidLiveness(unittest.TestCase):
    def test_own_pid_is_alive(self):
        self.assertTrue(app_mod._pid_is_alive(os.getpid()))

    def test_bogus_pid_is_not_alive(self):
        # 999999 is not a valid PID on any platform this app targets
        self.assertFalse(app_mod._pid_is_alive(999999))


class TestSingleInstanceLock(_TempStateDirMixin):
    def test_fresh_lock_is_acquired(self):
        self.assertTrue(app_mod.acquire_single_instance_lock())
        self.assertEqual(app_mod.INSTANCE_LOCK_FILE.read_text().strip(),
                         str(os.getpid()))

    def test_blocked_by_a_genuinely_live_process(self):
        # This is the exact scenario the lock exists to prevent: another
        # process (a second app instance) is actually running.
        proc = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(10)'])
        try:
            app_mod.INSTANCE_LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
            app_mod.INSTANCE_LOCK_FILE.write_text(str(proc.pid))
            self.assertFalse(app_mod.acquire_single_instance_lock())
        finally:
            proc.kill()
            proc.wait()

    def test_lock_freed_after_holder_process_exits(self):
        proc = subprocess.Popen([sys.executable, '-c', 'pass'])
        proc.wait()
        app_mod.INSTANCE_LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
        app_mod.INSTANCE_LOCK_FILE.write_text(str(proc.pid))
        self.assertTrue(app_mod.acquire_single_instance_lock())

    def test_corrupt_lock_file_treated_as_free(self):
        app_mod.INSTANCE_LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
        app_mod.INSTANCE_LOCK_FILE.write_text('not-a-pid')
        self.assertTrue(app_mod.acquire_single_instance_lock())

    def test_missing_lock_file_treated_as_free(self):
        self.assertFalse(app_mod.INSTANCE_LOCK_FILE.exists())
        self.assertTrue(app_mod.acquire_single_instance_lock())


class TestOrphanStageCleanup(unittest.TestCase):
    def setUp(self):
        self._dirs = []

    def tearDown(self):
        for d in self._dirs:
            shutil.rmtree(d, ignore_errors=True)

    def _make_stage(self, prefix, age_secs=None):
        d = tempfile.mkdtemp(prefix=f'se_stage_{prefix}_', dir=tempfile.gettempdir())
        self._dirs.append(d)
        docs = os.path.join(d, 'Documents')
        os.makedirs(docs)
        f = os.path.join(docs, 'f.txt')
        with open(f, 'w') as fh:
            fh.write('x')
        if age_secs is not None:
            t = time.time() - age_secs
            os.utime(f, (t, t))
            os.utime(docs, (t, t))
            os.utime(d, (t, t))
        return d

    def test_recently_active_stage_dir_is_kept(self):
        # A batch that's actively being written into — even a file staged
        # a moment ago inside the Documents/ subfolder, not the top-level
        # dir itself — must survive cleanup.
        young = self._make_stage('YOUNG')
        app_mod.cleanup_orphan_stages()
        self.assertTrue(os.path.isdir(young),
                        'an actively-used stage dir was deleted out from under a run')

    def test_old_untouched_stage_dir_is_removed(self):
        old = self._make_stage('OLD', age_secs=app_mod.ORPHAN_STAGE_MIN_AGE_SECS + 60)
        app_mod.cleanup_orphan_stages()
        self.assertFalse(os.path.isdir(old),
                         'a genuinely orphaned stage dir from a crashed run was not cleaned up')

    def test_activity_in_subdirectory_counts_as_recent(self):
        # Regression target: the top-level stage dir's own mtime only
        # changes when ITS immediate entries change (i.e. once, at
        # creation) — not when files are added into Documents/Photos.
        # Backdating just the top-level dir must not cause a live batch to
        # be swept.
        d = self._make_stage('SUBACTIVE')
        old = time.time() - (app_mod.ORPHAN_STAGE_MIN_AGE_SECS + 60)
        os.utime(d, (old, old))   # only the top-level dir is "old"
        app_mod.cleanup_orphan_stages()
        self.assertTrue(os.path.isdir(d),
                        'recent file activity in a subdirectory was ignored')


class TestCopyAndHashSelfHeal(unittest.TestCase):
    def setUp(self):
        self._tmp_files = []

    def tearDown(self):
        for p in self._tmp_files:
            try:
                if os.path.isdir(p):
                    shutil.rmtree(p, ignore_errors=True)
                elif os.path.exists(p):
                    os.unlink(p)
            except Exception:
                pass

    def _make_source(self, content=b'hello world' * 1000):
        f = tempfile.NamedTemporaryFile(delete=False)
        f.write(content)
        f.close()
        self._tmp_files.append(f.name)
        return f.name, len(content)

    def test_normal_copy_succeeds(self):
        src, size = self._make_source()
        dest_dir = tempfile.mkdtemp()
        self._tmp_files.append(dest_dir)
        h, tmp, written = app_mod.copy_and_hash(src, dest_dir)
        self._tmp_files.append(tmp)
        self.assertEqual(written, size)
        self.assertTrue(os.path.exists(tmp))
        self.assertEqual(len(h), 32)   # md5 hexdigest length

    def test_recovers_when_dest_dir_is_missing(self):
        # The exact failure mode from the incident: dest_dir vanished (in
        # production, via a second instance's orphan cleanup) between when
        # the worker resolved it and when it tried to write into it.
        src, size = self._make_source()
        dest_dir = tempfile.mkdtemp()
        shutil.rmtree(dest_dir)   # simulate it having been swept away
        h, tmp, written = app_mod.copy_and_hash(src, dest_dir)
        self._tmp_files.append(dest_dir)
        self._tmp_files.append(tmp)
        self.assertEqual(written, size)
        self.assertTrue(os.path.exists(tmp))

    def test_still_raises_if_source_is_missing(self):
        # Self-healing must be scoped to the destination directory only —
        # a genuinely missing/removed source file should still error out.
        dest_dir = tempfile.mkdtemp()
        self._tmp_files.append(dest_dir)
        with self.assertRaises(OSError):
            app_mod.copy_and_hash('/no/such/source/file', dest_dir)

    def test_write_integrity_check_still_enforced(self):
        # The retry loop must not accidentally swallow a real integrity
        # failure — only a missing destination directory triggers a retry.
        src, size = self._make_source()
        dest_dir = tempfile.mkdtemp()
        self._tmp_files.append(dest_dir)
        h, tmp, written = app_mod.copy_and_hash(src, dest_dir)
        self._tmp_files.append(tmp)
        self.assertEqual(written, size)


if __name__ == '__main__':
    unittest.main()
