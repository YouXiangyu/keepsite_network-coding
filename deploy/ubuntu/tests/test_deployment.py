"""Local artifact and command-stub activation tests. No SSH/system service calls."""
import importlib.util
import io
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location('prepare_release', ROOT / 'deploy/ubuntu/prepare-release.py')
prepare = importlib.util.module_from_spec(spec)
spec.loader.exec_module(prepare)


class ArtifactTests(unittest.TestCase):
    def setUp(self):
        cache = ROOT / '.cache/deployment-tests'
        cache.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=cache)
        self.root = Path(self.temporary.name)
        self.addCleanup(self.temporary.cleanup)
        self.archive = self.root / 'release.tar'
        with tarfile.open(self.archive, 'w') as bundle:
            data = b'original application source\n'
            member = tarfile.TarInfo('backend/app.py')
            member.size = len(data)
            bundle.addfile(member, io.BytesIO(data))
        self.sha = prepare.digest_file(self.archive)
        self.release = self.root / 'releases' / ('a' * 40)

    def test_prepare_resume_then_reuse_ready(self):
        self.assertFalse(prepare.prepare_artifact(self.archive, self.release, self.sha))
        self.assertFalse(prepare.prepare_artifact(self.archive, self.release, self.sha))
        (self.release / '.prepared-sha256').write_text(self.sha + '\n')
        self.assertTrue(prepare.prepare_artifact(self.archive, self.release, self.sha))

    def test_wrong_uploaded_hash_does_not_create_release(self):
        with self.assertRaisesRegex(ValueError, 'archive hash mismatch'):
            prepare.prepare_artifact(self.archive, self.release, '0' * 64)
        self.assertFalse(self.release.exists())

    def test_unidentified_existing_directory_is_preserved(self):
        self.release.mkdir(parents=True)
        sentinel = self.release / 'keep.txt'
        sentinel.write_text('preserve')
        with self.assertRaisesRegex(ValueError, 'unverified/conflicting'):
            prepare.prepare_artifact(self.archive, self.release, self.sha)
        self.assertEqual(sentinel.read_text(), 'preserve')

    def test_modified_prepared_source_is_not_reused(self):
        prepare.prepare_artifact(self.archive, self.release, self.sha)
        (self.release / '.prepared-sha256').write_text(self.sha + '\n')
        (self.release / 'backend/app.py').write_text('changed application source!\n')
        with self.assertRaisesRegex(ValueError, 'differs|incomplete'):
            prepare.prepare_artifact(self.archive, self.release, self.sha)

    def test_archive_traversal_rejected_before_extraction(self):
        with tarfile.open(self.archive, 'w') as bundle:
            member = tarfile.TarInfo('../escape')
            member.size = 1
            bundle.addfile(member, io.BytesIO(b'x'))
        with self.assertRaisesRegex(ValueError, 'Unsafe'):
            prepare.prepare_artifact(self.archive, self.release, prepare.digest_file(self.archive))
        self.assertFalse(self.release.exists())

    def test_archive_backslash_is_rejected(self):
        with tarfile.open(self.archive, 'w') as bundle:
            member = tarfile.TarInfo('backend\\escape.py')
            member.size = 1
            bundle.addfile(member, io.BytesIO(b'x'))
        with self.assertRaisesRegex(ValueError, 'Unsafe'):
            prepare.prepare_artifact(self.archive, self.release, prepare.digest_file(self.archive))


class ActivationTests(unittest.TestCase):
    def setUp(self):
        bash = shutil.which('bash')
        git = shutil.which('git')
        if not bash and git:
            candidate = Path(git).parent.parent / 'bin/bash.exe'
            if candidate.is_file():
                bash = str(candidate)
        if not bash:
            self.skipTest('Bash unavailable; activation simulation not run')
        self.bash = bash
        cache = ROOT / '.cache/deployment-tests'
        cache.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=cache)
        self.root = Path(self.temporary.name)
        self.addCleanup(self.temporary.cleanup)
        self.new = self.root / 'releases' / ('a' * 40)
        self.old = self.root / 'releases' / ('b' * 40)
        for release in (self.new, self.old):
            (release / 'demo/dist/client').mkdir(parents=True)
            (release / 'demo/dist/client/index.html').write_text('test')
            (release / 'backend/.venv/bin').mkdir(parents=True)
            python = release / 'backend/.venv/bin/python'
            python.write_text('#!/bin/sh\nexit 0\n')
            python.chmod(0o755)
            (release / '.artifact-sha256').write_text('fixture-hash\n')
            (release / '.prepared-sha256').write_text('fixture-hash\n')
        self.current = self.root / 'current'
        self.current.write_text(self.old.as_posix())
        self.log = self.root / 'commands.log'
        # Only adapt root guard/host path. All deployment control flow is unchanged.
        source = (ROOT / 'deploy/ubuntu/activate.sh').read_text(encoding='utf-8')
        source = source.replace('$EUID', '0').replace('/opt/memoverse', self.root.as_posix())
        stubs = r'''
systemctl() {
  printf 'systemctl %s\n' "$*" >> "$TEST_LOG"
  current=$(cat "$TEST_CURRENT" 2>/dev/null || true)
  if [[ "$1" == stop && "$MODE" == stop_fail ]]; then return 1; fi
  if [[ "$1" == enable && "$MODE" == enable_fail ]]; then return 1; fi
  if [[ "$1" == is-active && ! -f "$TEST_CURRENT" ]]; then return 1; fi
  if [[ "$1" == restart ]]; then
    if [[ "$MODE" == rollback_fail || ( "$MODE" == restart_fail_new && "$current" == "$TEST_NEW" ) ]]; then return 1; fi
  fi
  return 0
}
curl() {
  printf 'curl %s\n' "$*" >> "$TEST_LOG"
  current=$(cat "$TEST_CURRENT" 2>/dev/null || true)
  if [[ "$MODE" == health_fail_new && "$current" == "$TEST_NEW" ]]; then return 1; fi
  return 0
}
readlink() { cat "$TEST_CURRENT" 2>/dev/null; }
realpath() { printf '%s\n' "$1"; }
ln() { printf '%s' "$2" > "$3"; }
install() { printf 'install-unit\n' >> "$TEST_LOG"; return 0; }
sleep() { :; }
'''
        self.script = self.root / 'activate-test.sh'
        self.script.write_text(stubs + '\n' + source, encoding='utf-8')

    def run_activation(self, mode):
        return subprocess.run([self.bash, str(self.script), self.new.as_posix()],
            env={**os.environ, 'MODE': mode, 'TEST_LOG': self.log.as_posix(),
                 'TEST_CURRENT': self.current.as_posix(), 'TEST_NEW': self.new.as_posix()},
            capture_output=True, text=True, timeout=10)

    def test_success_switches_and_restarts(self):
        result = self.run_activation('success')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.current.read_text(), self.new.as_posix())

    def test_restart_failure_rolls_back_and_checks_health(self):
        result = self.run_activation('restart_fail_new')
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(self.current.read_text(), self.old.as_posix())
        self.assertIn('Restored and healthy', result.stderr)
        self.assertIn('--max-time 3', self.log.read_text())

    def test_health_failure_rolls_back(self):
        result = self.run_activation('health_fail_new')
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(self.current.read_text(), self.old.as_posix())

    def test_failed_rollback_is_explicit(self):
        result = self.run_activation('rollback_fail')
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn('ROLLBACK FAILED', result.stderr)

    def test_same_commit_healthy_does_not_restart(self):
        self.current.write_text(self.new.as_posix())
        result = self.run_activation('success')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn('systemctl restart', self.log.read_text())

    def test_stop_failure_keeps_old_link(self):
        result = self.run_activation('stop_fail')
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(self.current.read_text(), self.old.as_posix())

    def test_first_deploy_starts_without_existing_unit(self):
        self.current.unlink()
        resolved = subprocess.run([self.bash, '-c', 'readlink -f "$1"', '_', self.current.as_posix()],
                                  capture_output=True, text=True, timeout=5)
        self.assertEqual(resolved.returncode, 0, resolved.stderr)
        self.assertTrue(resolved.stdout.strip().endswith('/current'))
        self.assertFalse(self.current.exists())
        # Exercise real GNU readlink behavior, not the previous missing-file stub.
        source = self.script.read_text(encoding='utf-8').replace('readlink() { cat "$TEST_CURRENT" 2>/dev/null; }', '')
        self.script.write_text(source, encoding='utf-8')
        result = self.run_activation('success')
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_enable_failure_same_commit_retry_checks_enable_without_restart(self):
        first = self.run_activation('enable_fail')
        self.assertEqual(first.returncode, 1, first.stderr)
        self.assertEqual(self.current.read_text(), self.new.as_posix())
        self.log.write_text('')
        repeated = self.run_activation('enable_fail')
        self.assertEqual(repeated.returncode, 1, repeated.stderr)
        self.assertIn('systemctl enable', self.log.read_text())
        self.assertNotIn('systemctl restart', self.log.read_text())
        self.log.write_text('')
        recovered = self.run_activation('success')
        self.assertEqual(recovered.returncode, 0, recovered.stderr)
        self.assertIn('systemctl enable', self.log.read_text())
        self.assertNotIn('systemctl restart', self.log.read_text())


if __name__ == '__main__':
    unittest.main()
