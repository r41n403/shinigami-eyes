"""
Unit tests for .github/workflows/build-executables.yml — the CI pipeline that
builds Shinigami Eyes into standalone Windows/macOS executables and cuts a
GitHub Release on version tags.

These aren't "run the workflow" tests (that needs a real GitHub runner or
`act`) — they're structural/consistency checks against the parsed YAML that
catch the class of bug this project has actually hit before: an artifact
name that doesn't match what a later step downloads, a pyinstaller flag
missing on one platform but not the other, a release step referencing a
filename no earlier step produced, etc. Run with:

    python3 -m unittest discover -s tests
    # or, if pytest is installed:
    pytest tests/
"""
import re
import unittest
from pathlib import Path

import yaml

REPO_ROOT   = Path(__file__).resolve().parent.parent
WORKFLOW    = REPO_ROOT / '.github' / 'workflows' / 'build-executables.yml'


def load_workflow() -> dict:
    with open(WORKFLOW, encoding='utf-8') as f:
        return yaml.safe_load(f)


class TestWorkflowLoads(unittest.TestCase):
    def test_file_exists(self):
        self.assertTrue(WORKFLOW.exists(), f'{WORKFLOW} not found')

    def test_yaml_parses(self):
        data = load_workflow()
        self.assertIsInstance(data, dict)

    def test_top_level_keys(self):
        data = load_workflow()
        self.assertIn('jobs', data)
        self.assertIn('build', data['jobs'])
        self.assertIn('release', data['jobs'])


def _get_trigger(data: dict) -> dict:
    """YAML 1.1 parses a bare 'on:' key as the boolean True, not the string
    'on' — this bites everyone who tests GitHub Actions YAML with PyYAML at
    least once. Handle both so the test doesn't silently pass on a KeyError
    being swallowed elsewhere."""
    if 'on' in data:
        return data['on']
    if True in data:
        return data[True]
    raise AssertionError("workflow has no top-level trigger ('on:') block")


class TestTriggers(unittest.TestCase):
    def setUp(self):
        self.data = load_workflow()
        self.on = _get_trigger(self.data)

    def test_push_branches_include_main_and_master(self):
        branches = self.on['push']['branches']
        self.assertIn('main', branches)
        self.assertIn('master', branches)

    def test_push_paths_cover_source_logo_and_workflow_file(self):
        paths = self.on['push']['paths']
        self.assertIn('nas_migrate_gui.py', paths,
                       'app source not in trigger paths — pushes to it would not rebuild')
        self.assertIn('shinigami_eyes_logo.png', paths,
                       'logo asset not in trigger paths — a logo-only change would not rebuild')
        self.assertIn('.github/workflows/build-executables.yml', paths)

    def test_tag_trigger_matches_semver_pattern(self):
        tags = self.on['push']['tags']
        self.assertIn('v*.*.*', tags)

    def test_manual_dispatch_enabled(self):
        self.assertIn('workflow_dispatch', self.on)


class TestBuildMatrix(unittest.TestCase):
    def setUp(self):
        self.data  = load_workflow()
        self.build = self.data['jobs']['build']
        self.matrix_entries = self.build['strategy']['matrix']['include']

    def test_exactly_two_platforms(self):
        self.assertEqual(len(self.matrix_entries), 2)

    def test_platform_values_are_windows_and_macos(self):
        platforms = {m['platform'] for m in self.matrix_entries}
        self.assertEqual(platforms, {'windows', 'macos'})

    def test_os_runners_match_platform(self):
        by_platform = {m['platform']: m for m in self.matrix_entries}
        self.assertEqual(by_platform['windows']['os'], 'windows-latest')
        self.assertEqual(by_platform['macos']['os'], 'macos-latest')

    def test_artifact_names_are_unique(self):
        names = [m['artifact_name'] for m in self.matrix_entries]
        self.assertEqual(len(names), len(set(names)), 'duplicate artifact_name in matrix')

    def test_windows_artifact_is_exe_macos_is_zip(self):
        by_platform = {m['platform']: m for m in self.matrix_entries}
        self.assertTrue(by_platform['windows']['artifact_path'].endswith('.exe'))
        self.assertTrue(by_platform['macos']['artifact_path'].endswith('.zip'))

    def test_fail_fast_disabled(self):
        # One platform's build failing shouldn't cancel the other mid-build.
        self.assertFalse(self.build['strategy']['fail-fast'])


class TestBuildSteps(unittest.TestCase):
    def setUp(self):
        self.data  = load_workflow()
        self.steps = self.data['jobs']['build']['steps']

    def _step(self, name_substr: str, platform_if: str = None):
        for s in self.steps:
            if name_substr.lower() in s.get('name', '').lower():
                if platform_if is None or platform_if in s.get('if', ''):
                    return s
        return None

    def test_checkout_step_present(self):
        self.assertIsNotNone(self._step('Check out repo'))

    def test_pyinstaller_installed_on_both_platforms(self):
        win = self._step('Install PyInstaller', "'windows'")
        mac = self._step('Install PyInstaller', "'macos'")
        self.assertIsNotNone(win, 'no Windows-conditioned PyInstaller install step')
        self.assertIsNotNone(mac, 'no macOS-conditioned PyInstaller install step')

    def test_macos_pip_uses_break_system_packages(self):
        # Homebrew's externally-managed Python rejects a bare `pip install`
        # (PEP 668) — regression-tested here because CI broke on this once.
        mac = self._step('Install PyInstaller', "'macos'")
        self.assertIn('--break-system-packages', mac['run'])

    def test_build_steps_target_correct_script(self):
        win = self._step('Build (Windows)')
        mac = self._step('Build (macOS)')
        self.assertIsNotNone(win)
        self.assertIsNotNone(mac)
        self.assertIn('nas_migrate_gui.py', win['run'])
        self.assertIn('nas_migrate_gui.py', mac['run'])

    def test_windows_build_is_onefile_macos_is_not(self):
        win = self._step('Build (Windows)')
        mac = self._step('Build (macOS)')
        self.assertIn('--onefile', win['run'])
        self.assertNotIn('--onefile', mac['run'],
                          '--onefile on macOS would break the .app bundle output')

    def test_both_builds_are_windowed_with_correct_name(self):
        win = self._step('Build (Windows)')
        mac = self._step('Build (macOS)')
        for step in (win, mac):
            self.assertIn('--windowed', step['run'])
            self.assertIn('--name "Shinigami Eyes"', step['run'])

    def test_logo_bundled_with_platform_correct_separator(self):
        """--add-data separator is ';' on Windows and ':' elsewhere — mixing
        these up is a classic PyInstaller cross-platform mistake."""
        win = self._step('Build (Windows)')
        mac = self._step('Build (macOS)')
        self.assertIn('--add-data "shinigami_eyes_logo.png;."', win['run'])
        self.assertIn('--add-data "shinigami_eyes_logo.png:."', mac['run'])
        # and make sure neither accidentally used the other platform's separator
        self.assertNotIn('shinigami_eyes_logo.png:.', win['run'])
        self.assertNotIn('shinigami_eyes_logo.png;.', mac['run'])

    def test_macos_zip_step_uses_ditto_and_matches_matrix_artifact_path(self):
        zip_step = self._step('Zip macOS app bundle')
        self.assertIsNotNone(zip_step)
        self.assertIn('ditto', zip_step['run'])
        self.assertIn('Shinigami Eyes.app', zip_step['run'])

        matrix = self.data['jobs']['build']['strategy']['matrix']['include']
        macos_entry = next(m for m in matrix if m['platform'] == 'macos')
        zip_name = Path(macos_entry['artifact_path']).name
        self.assertIn(zip_name, zip_step['run'],
                      'zip step does not produce the file name the matrix expects to upload')

    def test_upload_artifact_step_is_templated_not_hardcoded(self):
        upload = self._step('Upload artifact')
        self.assertIsNotNone(upload)
        self.assertEqual(upload['with']['name'], '${{ matrix.artifact_name }}')
        self.assertEqual(upload['with']['path'], '${{ matrix.artifact_path }}')


class TestReleaseJob(unittest.TestCase):
    def setUp(self):
        self.data    = load_workflow()
        self.release = self.data['jobs']['release']

    def test_depends_on_build(self):
        self.assertEqual(self.release['needs'], 'build')

    def test_only_runs_on_version_tags(self):
        self.assertIn("startsWith(github.ref, 'refs/tags/v')", self.release['if'])

    def test_has_contents_write_permission(self):
        # Without this, softprops/action-gh-release fails to create the
        # release (this repo hit that exact failure once).
        self.assertEqual(self.release.get('permissions', {}).get('contents'), 'write')

    def test_package_steps_produce_files_the_release_step_lists(self):
        steps = self.release['steps']
        package_steps = [s for s in steps if s.get('name', '').startswith('Package')]
        self.assertTrue(package_steps, 'no "Package ..." steps found in release job')

        produced = set()
        for s in package_steps:
            # each is a `cp src ./Some-Name.ext` — pull out the destination
            m = re.search(r'cp\s+"[^"]+"\s+"\./([^"]+)"', s['run'])
            self.assertIsNotNone(m, f"couldn't parse output filename from: {s['run']}")
            produced.add(m.group(1))

        release_step = next(s for s in steps if s.get('uses', '').startswith('softprops/'))
        listed = {line.strip() for line in release_step['with']['files'].splitlines() if line.strip()}

        self.assertEqual(produced, listed,
                          f'release "files:" list {listed} does not match what the '
                          f'package steps actually produce {produced}')

    def test_download_artifact_step_present(self):
        steps = self.release['steps']
        self.assertTrue(
            any(s.get('uses', '').startswith('actions/download-artifact') for s in steps))


class TestArtifactNameConsistency(unittest.TestCase):
    """Cross-job check: the artifact_name each matrix leg uploads under must
    be exactly what the release job's package steps look for under
    artifacts/<name>/... — a mismatch here means the release step silently
    can't find the file (this is the kind of drift hand-edited YAML invites)."""

    def test_release_package_steps_reference_real_artifact_names(self):
        data = load_workflow()
        matrix_names = {m['artifact_name']
                        for m in data['jobs']['build']['strategy']['matrix']['include']}
        release_steps = data['jobs']['release']['steps']
        referenced = set()
        for s in release_steps:
            for name in matrix_names:
                if name in s.get('run', ''):
                    referenced.add(name)
        self.assertEqual(referenced, matrix_names,
                          'not every matrix artifact_name is referenced in the release job')


if __name__ == '__main__':
    unittest.main()
