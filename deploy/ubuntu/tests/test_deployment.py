"""Local artifact and command-stub activation tests. No SSH/system service calls."""
import importlib.util
import base64
import hashlib
import sys
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
        self.etc = self.root / 'etc'
        (self.etc / 'nginx/sites-available').mkdir(parents=True)
        (self.etc / 'nginx/sites-enabled').mkdir()
        (self.etc / 'systemd/system').mkdir(parents=True)
        self.env = self.etc / 'memoverse.env'
        self.site = self.etc / 'nginx/sites-available/memoverse'
        self.unit = self.etc / 'systemd/system/memoverse.service'
        self.next_env = self.etc / 'memoverse.env.next'
        self.next_site = self.etc / 'nginx/sites-available/memoverse.next'
        self.environment = '\n'.join([
            'MEMOVERSE_DATA_DIR=/var/lib/memoverse-v2', 'MEMOVERSE_STORAGE=s3',
            'MEMOVERSE_S3_BUCKET=private-fixture', 'AWS_REGION=ap-northeast-1',
            'MEMOVERSE_COGNITO_REGION=ap-northeast-1', 'MEMOVERSE_COGNITO_USER_POOL_ID=ap-northeast-1_test',
            'MEMOVERSE_COGNITO_CLIENT_ID=client', 'MEMOVERSE_COGNITO_DOMAIN=https://auth.example.test',
            'MEMOVERSE_PUBLIC_URL=https://example.test', 'MEMOVERSE_TEMPLATE_DIR=/fixture/template',
            'MEMOVERSE_SESSION_KEY=' + base64.urlsafe_b64encode(b'a' * 32).decode(),
        ]) + '\n'
        self.env.write_text(self.environment)
        self.next_env.write_text(self.environment + 'CONFIG_REVISION=new\n')
        self.site.write_text('PRIVATE_BASIC')
        self.next_site.write_text('PUBLIC_SESSION')
        self.unit.write_text('actual-previous-unit')
        for release in (self.new, self.old):
            (release / 'deploy/ubuntu').mkdir(parents=True)
            (release / 'deploy/ubuntu/memoverse.service').write_text('new-unit')
        self.current = self.root / 'current'
        self.current.write_text(self.old.as_posix())
        self.log = self.root / 'commands.log'
        # Only adapt root guard/host path. All deployment control flow is unchanged.
        source = (ROOT / 'deploy/ubuntu/activate.sh').read_text(encoding='utf-8')
        source = source.replace('$EUID', '0').replace('/opt/memoverse', self.root.as_posix()).replace('/etc/', self.etc.as_posix() + '/')
        # Windows cannot create the Linux nginx symlink; simulate its presence
        # only at the fixed site-link check, retaining all target validation.
        source = source.replace('[[ -L ' + self.etc.as_posix() + '/nginx/sites-enabled/memoverse', '[[ -e ' + self.etc.as_posix() + '/nginx/sites-enabled/memoverse')
        (self.etc / 'nginx/sites-enabled/memoverse').write_text('fixture-enabled-link')
        stubs = r'''
systemctl() {
  printf 'systemctl %s\n' "$*" >> "$TEST_LOG"
  current=$(cat "$TEST_CURRENT" 2>/dev/null || true)
  if [[ "$1" == stop && "$MODE" == stop_fail ]]; then return 1; fi
  if [[ "$1" == enable && "$MODE" == enable_fail ]]; then return 1; fi
  if [[ "$1" == is-active && "$3" == memoverse && ! -f "$TEST_CURRENT" ]]; then return 1; fi
  if [[ "$1" == reload && "$2" == nginx ]]; then
    if [[ "$MODE" == rollback_nginx_fail || ( "$MODE" == nginx_reload_fail && "$(cat "$TEST_SITE")" == PUBLIC_SESSION ) ]]; then return 1; fi
  fi
  if [[ "$1" == start && "$2" == nginx ]]; then
    if [[ "$MODE" == rollback_nginx_fail || ( "$MODE" == nginx_reload_fail && "$(cat "$TEST_SITE")" == PUBLIC_SESSION ) ]]; then return 1; fi
  fi
  if [[ "$1" == restart ]]; then
    if [[ "$MODE" == rollback_fail || ( "$MODE" == restart_fail_new && "$current" == "$TEST_NEW" ) ]]; then return 1; fi
  fi
  return 0
}
curl() {
  printf 'curl %s\n' "$*" >> "$TEST_LOG"
  current=$(cat "$TEST_CURRENT" 2>/dev/null || true)
  if [[ "$MODE" == health_fail_new && "$current" == "$TEST_NEW" ]]; then return 1; fi
  case "${@: -1}" in
    */api/state) if [[ "$MODE" == anonymous_api ]]; then printf 200; else printf 401; fi ;;
    */api/auth/session) if [[ "$MODE" == auth_unconfigured ]]; then printf '{"authenticated":false,"configured":false}'; else printf '{"authenticated":false,"configured":true,"providers":{"email":true}}'; fi ;;
    *) printf '{"status":"ok"}' ;;
  esac
}
readlink() { if [[ "${@: -1}" == */sites-enabled/memoverse ]]; then printf '%s\n' "$TEST_SITE"; else cat "$TEST_CURRENT" 2>/dev/null; fi; }
realpath() { printf '%s\n' "$1"; }
stat() {
  case "$2" in
    %u) printf 0 ;;
    %G) printf root ;;
    %a) if [[ "$MODE" == env_permissions && "$3" == *.env.next ]]; then printf 666; elif [[ "$3" == *.env* ]]; then printf 600; else printf 644; fi ;;
  esac
}
ln() { printf '%s' "$2" > "$3"; }
install() {
  printf 'install %s\n' "$*" >> "$TEST_LOG"
  if [[ "$1" == -d ]]; then mkdir -p -- "${@: -1}"; else command cp -- "${@: -2:1}" "${@: -1}"; fi
}
nginx() {
  printf 'nginx %s\n' "$*" >> "$TEST_LOG"
  if [[ "$MODE" == nginx_syntax_fail && "$*" == *'-c '* ]]; then return 1; fi
  return 0
}
flock() { return 0; }
python3() { "$TEST_PYTHON" "$@"; }
sleep() { :; }
'''
        self.script = self.root / 'activate-test.sh'
        self.script.write_text(stubs + '\n' + source, encoding='utf-8')

    def run_activation(self, mode, *, configuration=False):
        arguments = [self.bash, str(self.script), self.new.as_posix()]
        if configuration:
            arguments += ['--environment', self.next_env.as_posix(), '--nginx', self.next_site.as_posix()]
        return subprocess.run(arguments,
            env={**os.environ, 'MODE': mode, 'TEST_LOG': self.log.as_posix(),
                 'TEST_CURRENT': self.current.as_posix(), 'TEST_NEW': self.new.as_posix(),
                 'TEST_SITE': self.site.as_posix(), 'TEST_PYTHON': sys.executable.replace('\\', '/')},
            capture_output=True, text=True, timeout=10)

    def test_success_switches_and_restarts(self):
        result = self.run_activation('success')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.current.read_text(), self.new.as_posix())

    def test_restart_failure_rolls_back_and_checks_health(self):
        result = self.run_activation('restart_fail_new')
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(self.current.read_text(), self.old.as_posix())
        self.assertIn('Restored previous code, environment and unit', result.stderr)
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
        self.unit.write_text('new-unit')
        result = self.run_activation('success')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn('systemctl restart', self.log.read_text())

    def test_stop_failure_keeps_old_link(self):
        result = self.run_activation('stop_fail')
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(self.current.read_text(), self.old.as_posix())

    def test_first_deploy_starts_without_existing_unit(self):
        self.current.unlink()
        self.unit.unlink()
        resolved = subprocess.run([self.bash, '-c', 'readlink -f "$1"', '_', self.current.as_posix()],
                                  capture_output=True, text=True, timeout=5)
        self.assertEqual(resolved.returncode, 0, resolved.stderr)
        self.assertTrue(resolved.stdout.strip().endswith('/current'))
        self.assertFalse(self.current.exists())
        # Exercise real GNU readlink behavior, not the previous missing-file stub.
        source = '\n'.join(line for line in self.script.read_text(encoding='utf-8').splitlines() if not line.startswith('readlink()'))
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

    def test_configuration_switch_waits_for_auth_before_public_nginx(self):
        result = self.run_activation('success', configuration=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.env.read_text(), self.next_env.read_text())
        self.assertEqual(self.site.read_text(), 'PUBLIC_SESSION')
        commands = self.log.read_text()
        self.assertLess(commands.index('systemctl stop nginx'), commands.index('/api/auth/session'))
        self.assertLess(commands.index('/api/auth/session'), commands.index('systemctl start nginx'))

    def test_same_commit_with_environment_changes_still_restarts(self):
        self.current.write_text(self.new.as_posix())
        self.unit.write_text('new-unit')
        result = self.run_activation('success', configuration=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('systemctl restart memoverse', self.log.read_text())
        self.assertEqual(self.env.read_text(), self.next_env.read_text())

    def test_restart_failure_restores_actual_old_environment_unit_and_private_site(self):
        self.env.write_text('MEMOVERSE_DATA_DIR=/var/lib/memoverse\n')
        old_env = self.env.read_text()
        result = self.run_activation('restart_fail_new', configuration=True)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(self.env.read_text(), old_env)
        self.assertEqual(self.unit.read_text(), 'actual-previous-unit')
        self.assertEqual(self.site.read_text(), 'PRIVATE_BASIC')
        self.assertEqual(self.current.read_text(), self.old.as_posix())

    def test_public_site_is_not_applied_without_backend_authentication(self):
        for mode in ('anonymous_api', 'auth_unconfigured'):
            with self.subTest(mode=mode):
                result = self.run_activation(mode, configuration=True)
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertEqual(self.site.read_text(), 'PRIVATE_BASIC')
                self.assertEqual(self.current.read_text(), self.old.as_posix())

    def test_nginx_reload_failure_restores_private_site_before_old_app(self):
        result = self.run_activation('nginx_reload_fail', configuration=True)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(self.site.read_text(), 'PRIVATE_BASIC')
        self.assertEqual(self.env.read_text(), self.environment)
        self.assertEqual(self.unit.read_text(), 'actual-previous-unit')
        commands = self.log.read_text()
        self.assertLess(commands.rindex('systemctl stop nginx'), commands.rindex('systemctl start nginx'))
        self.assertLess(commands.rindex('systemctl start nginx'), commands.rindex('systemctl restart memoverse'))

    def test_private_nginx_rollback_failure_does_not_start_old_application(self):
        result = self.run_activation('rollback_nginx_fail', configuration=True)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn('ROLLBACK FAILED', result.stderr)
        commands = self.log.read_text()
        self.assertEqual(commands.count('systemctl restart memoverse'), 1)
        self.assertIn('systemctl stop nginx', commands)

    def test_candidate_permissions_or_syntax_fail_before_stopping_live_app(self):
        for mode in ('env_permissions', 'nginx_syntax_fail'):
            with self.subTest(mode=mode):
                self.log.write_text('')
                result = self.run_activation(mode, configuration=True)
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn('systemctl stop memoverse', self.log.read_text())
                self.assertEqual(self.env.read_text(), self.environment)
                self.assertEqual(self.site.read_text(), 'PRIVATE_BASIC')

    def test_wrong_candidate_data_root_fails_without_touching_legacy_data(self):
        self.next_env.write_text(self.environment.replace('/var/lib/memoverse-v2', '/var/lib/memoverse'))
        result = self.run_activation('success', configuration=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn('systemctl stop memoverse', self.log.read_text())
        self.assertEqual(self.current.read_text(), self.old.as_posix())


class PrepareOnlyTests(unittest.TestCase):
    def test_execute_prepare_only_never_calls_activation(self):
        powershell = shutil.which('pwsh')
        if not powershell:
            self.skipTest('PowerShell unavailable')
        cache = ROOT / '.cache/deployment-tests'
        cache.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=cache) as temporary:
            root = Path(temporary)
            (root / 'scripts').mkdir()
            script = root / 'scripts/deploy-ubuntu.ps1'
            script.write_bytes((ROOT / 'scripts/deploy-ubuntu.ps1').read_bytes())
            transfer = root / '.cache/server'
            (transfer / 'ssh').mkdir(parents=True)
            (transfer / 'ssh/id_rsa').write_text('synthetic-not-a-key')
            (transfer / 'ssh/known_hosts').write_text('synthetic-host')
            archive = transfer / ('release-' + 'a' * 40 + '.tar')
            archive.write_bytes(b'synthetic-artifact')
            Path(str(archive) + '.sha256').write_text(hashlib.sha256(archive.read_bytes()).hexdigest())
            log = root / 'commands.log'
            harness = root / 'test.ps1'
            harness.write_text('''
$ErrorActionPreference = 'Stop'
function git { $global:LASTEXITCODE=0; if($args -contains 'rev-parse'){ 'a' * 40 } }
function tar { $global:LASTEXITCODE=0; '# synthetic helper' }
function scp { $global:LASTEXITCODE=0; Add-Content -LiteralPath $env:TEST_LOG -Value 'scp' }
function ssh { $global:LASTEXITCODE=0; Add-Content -LiteralPath $env:TEST_LOG -Value $args[-1] }
& $env:TEST_DEPLOY -Server 192.0.2.1 -PrepareOnly -Execute
''', encoding='utf-8')
            result = subprocess.run([powershell, '-NoProfile', '-File', str(harness)],
                env={**os.environ, 'TEST_LOG': str(log), 'TEST_DEPLOY': str(script)},
                capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('Prepared', result.stdout)
            commands = log.read_text(encoding='utf-8-sig')
            self.assertIn('sudo python3', commands)
            self.assertNotIn('activate.sh', commands)
            self.assertNotIn('systemctl', commands)


if __name__ == '__main__':
    unittest.main()
