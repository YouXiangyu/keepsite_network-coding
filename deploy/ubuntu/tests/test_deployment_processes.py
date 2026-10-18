"""Real process locks/signals; all service commands and host paths are isolated."""
import hashlib
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest
from unittest.mock import patch

import test_deployment as baseline

ROOT = baseline.ROOT
prepare = baseline.prepare


def wait_file(path, process, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return
        if process.poll() is not None:
            out, err = process.communicate()
            raise AssertionError(f'Process exited before {path.name}: {out}\n{err}')
        time.sleep(0.02)
    raise AssertionError(f'Timed out waiting for {path}')


class ExtractionRecoveryTests(unittest.TestCase):
    setUp = baseline.ArtifactTests.setUp

    def test_partial_extraction_is_not_published_and_retry_succeeds(self):
        def partial(bundle, target, **kwargs):
            (target / 'partial').write_text('interrupted')
            raise RuntimeError('injected extraction failure')
        with patch.object(tarfile.TarFile, 'extractall', partial):
            with self.assertRaisesRegex(RuntimeError, 'injected'):
                prepare.prepare_artifact(self.archive, self.release, self.sha)
        self.assertFalse(self.release.exists())
        self.assertEqual(list(self.release.parent.glob('.*.extract-*')), [])
        self.assertFalse(prepare.prepare_artifact(self.archive, self.release, self.sha))
        self.assertEqual((self.release / '.artifact-sha256').read_text().strip(), self.sha)


@unittest.skipUnless(sys.platform == 'linux' and shutil.which('flock'), 'Requires Linux with real flock and POSIX signals')
class LinuxActivationProcesses(unittest.TestCase):
    def setUp(self):
        baseline.ActivationTests.setUp(self)
        source = self.script.read_text()
        source = source.replace('flock() { return 0; }', '')
        # Use real Linux filesystem link operations; only services are stubs.
        source = '\n'.join(line for line in source.splitlines()
                           if not line.startswith(('readlink()', 'realpath()', 'ln()')))
        source = source.replace('current=$(cat "$TEST_CURRENT" 2>/dev/null || true)',
                                'current=$(readlink -f "$TEST_CURRENT" 2>/dev/null || true)')
        source = source.replace('! -f "$TEST_CURRENT"', '! -e "$TEST_CURRENT"')
        self.current.unlink()
        self.current.symlink_to(self.old)
        enabled = self.etc / 'nginx/sites-enabled/memoverse'
        enabled.unlink()
        enabled.symlink_to(self.site)
        source = source.replace('systemctl() {', 'fake_systemctl() {', 1)
        hooks = r'''
checkpoint() {
  if [[ "$TEST_STAGE" == "$1" && ! -f "$TEST_ROOT/reached" ]]; then
    : > "$TEST_ROOT/reached"
    while [[ ! -f "$TEST_ROOT/release" ]]; do command sleep 0.02; done
  fi
}
systemctl() {
  fake_systemctl "$@"
  local result=$?
  if [[ "${1:-} ${2:-}" == 'stop memoverse' ]]; then
    if [[ -f "$TEST_ROOT/reached" && "$TEST_HOLD_ROLLBACK" == 1 ]]; then
      : > "$TEST_ROOT/recovering"
      while [[ ! -f "$TEST_ROOT/recover" ]]; do command sleep 0.02; done
    fi
    checkpoint stop
  fi
  return "$result"
}
mv() {
  command mv "$@" || return $?
  if [[ "${@: -1}" == */memoverse.service ]]; then checkpoint unit; fi
  if [[ "${@: -1}" == "$TEST_CURRENT" ]]; then checkpoint link; fi
}
'''
        source = source.replace('# Switch code/config', hooks + '\n# Switch code/config', 1)
        self.script.write_text(source)
        self.processes = []
        self.addCleanup(self.stop_processes)

    def stop_processes(self):
        for process in self.processes:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
            process.communicate(timeout=5)

    def launch(self, stage='', mode='success', hold=False):
        environment = {**os.environ, 'MODE': mode, 'TEST_LOG': str(self.log),
                       'TEST_CURRENT': str(self.current), 'TEST_NEW': str(self.new),
                       'TEST_SITE': str(self.site), 'TEST_PYTHON': sys.executable,
                       'TEST_ROOT': str(self.root), 'TEST_STAGE': stage,
                       'TEST_HOLD_ROLLBACK': '1' if hold else '0'}
        process = subprocess.Popen([self.bash, str(self.script), str(self.new),
                                    '--environment', str(self.next_env), '--nginx', str(self.next_site)],
                                   env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   text=True, start_new_session=True)
        self.processes.append(process)
        return process

    def assert_restored(self):
        self.assertEqual(self.current.resolve(), self.old)
        self.assertEqual(self.env.read_text(), self.environment)
        self.assertEqual(self.unit.read_text(), 'actual-previous-unit')
        self.assertEqual(self.site.read_text(), 'PRIVATE_BASIC')

    def interrupt_stage(self, stage):
        process = self.launch(stage)
        wait_file(self.root / 'reached', process)
        process.send_signal(signal.SIGTERM)
        out, err = process.communicate(timeout=10)
        self.assertEqual(process.returncode, 143, out + err)
        self.assertEqual(err.count('Activation failed; rollback evidence:'), 1)
        self.assert_restored()

    def test_term_after_stop_restores(self):
        self.interrupt_stage('stop')

    def test_term_after_unit_install_restores(self):
        self.interrupt_stage('unit')

    def test_term_after_link_switch_restores(self):
        self.interrupt_stage('link')

    def test_term_keeps_previously_stopped_services_stopped(self):
        process = self.launch('unit', mode='stopped_signal')
        wait_file(self.root / 'reached', process)
        process.send_signal(signal.SIGTERM)
        out, err = process.communicate(timeout=10)
        self.assertEqual(process.returncode, 143, out + err)
        self.assert_restored()
        commands = self.log.read_text()
        self.assertNotIn('systemctl restart memoverse', commands)
        self.assertNotIn('systemctl start nginx', commands)
        self.assertIn('application active=0; nginx active=0', err)

    def test_repeated_signals_recover_once_and_lock_covers_recovery(self):
        process = self.launch('link', hold=True)
        wait_file(self.root / 'reached', process)
        process.send_signal(signal.SIGTERM)
        wait_file(self.root / 'recovering', process)
        for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            process.send_signal(sig)
        other = self.launch()
        out, err = other.communicate(timeout=10)
        self.assertEqual(other.returncode, 2, out + err)
        self.assertIn('Another preparation or activation', err)
        # The Python preparation lock contends on the actual same inode too.
        with self.assertRaisesRegex(RuntimeError, 'Another preparation'):
            with prepare.deployment_lock(self.root / 'activation-backups/.lock'):
                self.fail('prepare entered activation recovery')
        (self.root / 'recover').touch()
        out, err = process.communicate(timeout=10)
        self.assertEqual(process.returncode, 143, out + err)
        self.assertEqual(err.count('Activation failed; rollback evidence:'), 1)
        self.assert_restored()

    def test_preparation_lock_blocks_activation(self):
        with prepare.deployment_lock(self.root / 'activation-backups/.lock'):
            process = self.launch()
            out, err = process.communicate(timeout=10)
        self.assertEqual(process.returncode, 2, out + err)
        self.assertNotIn('systemctl stop', self.log.read_text() if self.log.exists() else '')

    def test_interrupted_rollback_failure_is_nonzero(self):
        process = self.launch('unit', mode='rollback_fail')
        wait_file(self.root / 'reached', process)
        process.send_signal(signal.SIGTERM)
        out, err = process.communicate(timeout=10)
        self.assertEqual(process.returncode, 2, out + err)
        self.assertIn('ROLLBACK FAILED', err)

    def test_success_and_same_commit_do_not_rollback_or_restart(self):
        first = self.launch()
        out, err = first.communicate(timeout=10)
        self.assertEqual(first.returncode, 0, out + err)
        self.assertNotIn('rollback evidence:', err)
        self.log.write_text('')
        second = self.launch()
        out, err = second.communicate(timeout=10)
        self.assertEqual(second.returncode, 0, out + err)
        self.assertIn('Already active', out)
        self.assertNotIn('systemctl restart', self.log.read_text())
        self.assertNotIn('rollback evidence:', err)


@unittest.skipUnless(sys.platform == 'linux', 'Requires Linux POSIX signals and fcntl')
class LinuxPreparationProcesses(unittest.TestCase):
    setUp = baseline.ArtifactTests.setUp

    def test_exited_installer_leader_cannot_leave_descendants_outside_lock(self):
        ready = self.root / 'descendant'
        lock = self.root / 'deployment.lock'
        sleeper = ("import os,signal,time; from pathlib import Path; "
                   "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                   f"Path({str(ready)!r}).write_text(str(os.getpgrp())); time.sleep(60)")
        leader = self.root / 'leader.py'
        leader.write_text('import subprocess,sys,time\nfrom pathlib import Path\n'
                          f'subprocess.Popen([sys.executable, "-c", {sleeper!r}])\n'
                          f'while not Path({str(ready)!r}).exists(): time.sleep(0.01)\n'
                          'raise SystemExit(7)\n')
        runner = self.root / 'runner.py'
        runner.write_text('import importlib.util, pathlib, sys\n'
                          f'spec=importlib.util.spec_from_file_location("p", {str(ROOT / "deploy/ubuntu/prepare-release.py")!r})\n'
                          'p=importlib.util.module_from_spec(spec); spec.loader.exec_module(p)\n'
                          f'with p.deployment_lock(pathlib.Path({str(lock)!r})):\n'
                          f'    p.run_installer([sys.executable, {str(leader)!r}])\n')
        child = subprocess.Popen([sys.executable, str(runner)], stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, text=True)
        group = None
        try:
            wait_file(ready, child)
            group = int(ready.read_text())
            with self.assertRaisesRegex(RuntimeError, 'Another preparation'):
                with prepare.deployment_lock(lock):
                    self.fail('Installer descendants lost the deployment lock')
            out, err = child.communicate(timeout=10)
            self.assertNotEqual(child.returncode, 0)
            self.assertIn('Dependency preparation failed', err)
            self.assertFalse(prepare.group_has_live_processes(group))
            with prepare.deployment_lock(lock):
                pass
        finally:
            if child.poll() is None:
                child.kill()
            if group is not None:
                try:
                    os.killpg(group, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            child.communicate(timeout=5)

    def test_term_during_extraction_cleans_staging_then_retry(self):
        script = self.root / 'extract.py'
        script.write_text('''
import importlib.util, pathlib, signal, sys, tarfile, time
spec = importlib.util.spec_from_file_location('prepare', sys.argv[1])
p = importlib.util.module_from_spec(spec); spec.loader.exec_module(p)
signal.signal(signal.SIGTERM, p.interrupted)
original = tarfile.TarFile.extractall
def paused(self, target, **kwargs):
    original(self, target, **kwargs)
    pathlib.Path(sys.argv[5]).touch()
    time.sleep(60)
tarfile.TarFile.extractall = paused
with p.deployment_lock(pathlib.Path(sys.argv[6])):
    p.prepare_artifact(pathlib.Path(sys.argv[2]), pathlib.Path(sys.argv[3]), sys.argv[4])
''')
        ready = self.root / 'reached'
        lock = self.root / 'deployment.lock'
        args = [sys.executable, str(script), str(ROOT / 'deploy/ubuntu/prepare-release.py'),
                str(self.archive), str(self.release), self.sha, str(ready), str(lock)]
        child = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            wait_file(ready, child)
            other = subprocess.run(args, capture_output=True, text=True, timeout=5)
            self.assertNotEqual(other.returncode, 0)
            self.assertIn('Another preparation', other.stderr)
            child.send_signal(signal.SIGTERM)
            out, err = child.communicate(timeout=5)
            self.assertEqual(child.returncode, 143, out + err)
            self.assertFalse(self.release.exists())
            self.assertEqual(list(self.release.parent.glob('.*.extract-*')), [])
            with prepare.deployment_lock(lock):
                self.assertFalse(prepare.prepare_artifact(self.archive, self.release, self.sha))
        finally:
            if child.poll() is None:
                child.kill()
            child.communicate()


@unittest.skipUnless(shutil.which('pwsh'), 'PowerShell 7 required for deployment process tests')
class PowerShellDeploymentProcesses(unittest.TestCase):
    def setUp(self):
        cache = ROOT / '.cache/deployment-tests'
        cache.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=cache)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / 'scripts').mkdir()
        (self.root / 'demo').mkdir()
        self.deploy = self.root / 'scripts/deploy-ubuntu.ps1'
        self.deploy.write_bytes((ROOT / 'scripts/deploy-ubuntu.ps1').read_bytes())
        self.transfer = self.root / '.cache/server'
        (self.transfer / 'ssh').mkdir(parents=True)
        (self.transfer / 'ssh/id_rsa').write_text('synthetic')
        (self.transfer / 'ssh/known_hosts').write_text('synthetic')
        self.archive = self.transfer / ('release-' + 'a' * 40 + '.tar')
        self.archive.write_bytes(b'synthetic-artifact')
        self.hashfile = Path(str(self.archive) + '.sha256')
        self.hashfile.write_text(hashlib.sha256(self.archive.read_bytes()).hexdigest())
        self.harness = self.root / 'harness.ps1'
        self.harness.write_text(r'''
$ErrorActionPreference = 'Stop'
function git {
    $global:LASTEXITCODE=0
    if ($args -contains 'rev-parse') { 'a' * 40 }
    if ($args -contains 'archive') {
        $OutputArgument = $args | Where-Object { $_ -like '--output=*' }
        [IO.File]::WriteAllText($OutputArgument.Substring(9), 'rebuilt-artifact')
    }
}
function npm { $global:LASTEXITCODE=0; Add-Content $env:TEST_LOG 'build' }
function tar { $global:LASTEXITCODE=0; if ($args -contains '-xOf') { '# fixture helper' } }
function scp {
    Add-Content $env:TEST_LOG ('scp ' + $args[-1])
    if ($env:HOLD_UPLOAD -eq '1') {
        [IO.File]::WriteAllText($env:TEST_REACHED, 'ready')
        while (-not (Test-Path -LiteralPath $env:TEST_RELEASE)) { Start-Sleep -Milliseconds 20 }
    }
    $global:LASTEXITCODE = if ($env:FAIL_UPLOAD -eq '1') { 1 } else { 0 }
}
function ssh { $global:LASTEXITCODE=0; Add-Content $env:TEST_LOG $args[-1] }
function Move-Item {
    param($LiteralPath, $Destination)
    if ($env:FAIL_PUBLISH -eq '1') { throw 'injected publication interruption' }
    Microsoft.PowerShell.Management\Move-Item -LiteralPath $LiteralPath -Destination $Destination
}
& $env:TEST_DEPLOY -Server 192.0.2.1 -PrepareOnly -Execute
''')
        self.processes = []
        self.addCleanup(self.stop_processes)

    def stop_processes(self):
        for process in self.processes:
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=5)

    def launch(self, name, **flags):
        environment = {**os.environ, 'TEST_DEPLOY': str(self.deploy),
                       'TEST_LOG': str(self.root / (name + '.log')),
                       'TEST_REACHED': str(self.root / 'reached'),
                       'TEST_RELEASE': str(self.root / 'release'),
                       'HOLD_UPLOAD': '', 'FAIL_UPLOAD': '', 'FAIL_PUBLISH': '', **flags}
        process = subprocess.Popen([shutil.which('pwsh'), '-NoProfile', '-File', str(self.harness)],
                                   env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   text=True, encoding='utf-8')
        self.processes.append(process)
        return process

    def test_workspace_lock_failure_cleanup_and_unique_transfers(self):
        first = self.launch('first', HOLD_UPLOAD='1', FAIL_UPLOAD='1')
        wait_file(self.root / 'reached', first)
        second = self.launch('second')
        out, err = second.communicate(timeout=15)
        self.assertNotEqual(second.returncode, 0)
        self.assertIn('Another deployment', out + err)
        self.assertFalse((self.root / 'second.log').exists())
        (self.root / 'release').touch()
        out, err = first.communicate(timeout=15)
        self.assertNotEqual(first.returncode, 0)
        self.assertIn('Release upload failed', out + err)
        first_log = (self.root / 'first.log').read_text(encoding='utf-8-sig')
        self.assertIn('rm -f --', first_log)
        retry = self.launch('retry')
        out, err = retry.communicate(timeout=15)
        self.assertEqual(retry.returncode, 0, out + err)
        retry_log = (self.root / 'retry.log').read_text(encoding='utf-8-sig')
        first_path = next(line for line in first_log.splitlines() if line.startswith('scp '))
        retry_path = next(line for line in retry_log.splitlines() if line.startswith('scp '))
        self.assertNotEqual(first_path, retry_path)
        self.assertEqual(self.archive.read_bytes(), b'synthetic-artifact')
        self.assertEqual(list(self.transfer.glob('prepare-*.py')), [])
        self.assertNotIn('activate.sh', retry_log)

    def test_hash_only_interruption_can_rebuild_without_unverified_archive(self):
        self.archive.unlink()
        interrupted = self.launch('interrupted', FAIL_PUBLISH='1')
        out, err = interrupted.communicate(timeout=15)
        self.assertNotEqual(interrupted.returncode, 0)
        self.assertFalse(self.archive.exists())
        self.assertEqual(list(self.transfer.glob('*.building.tar')), [])
        self.assertTrue(self.hashfile.exists())
        retry = self.launch('retry')
        out, err = retry.communicate(timeout=15)
        self.assertEqual(retry.returncode, 0, out + err)
        self.assertEqual(self.hashfile.read_text().strip(), hashlib.sha256(self.archive.read_bytes()).hexdigest())
