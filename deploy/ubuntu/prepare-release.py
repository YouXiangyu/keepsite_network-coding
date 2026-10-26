#!/usr/bin/env python3
"""Verify an uploaded artifact, resume only matching releases, prepare dependencies."""
import argparse
from contextlib import contextmanager
import hashlib
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import signal
import subprocess
import tarfile
import tempfile
import time


def digest_file(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def atomic_text(path, value):
    temporary = path.with_name(path.name + f'.{os.getpid()}.next')
    try:
        temporary.write_text(value)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def deployment_lock(path):
    # The same inode is locked by activate.sh, including its rollback.
    import fcntl
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('Another preparation or activation is running') from None
        yield


def interrupted(signum, frame):
    raise SystemExit(128 + signum)


def group_has_live_processes(group):
    # Ubuntu /proc distinguishes dead orphan zombies from running descendants.
    # killpg(..., 0) alone can report a zombie-only group indefinitely.
    for entry in Path('/proc').iterdir():
        if not entry.name.isdigit():
            continue
        try:
            fields = (entry / 'stat').read_text().rpartition(')')[2].split()
            if int(fields[2]) == group and fields[0] not in {'Z', 'X'}:
                return True
        except (FileNotFoundError, ProcessLookupError):
            continue
    return False


def run_installer(command):
    child = None
    try:
        child = subprocess.Popen(command, start_new_session=True)
        if child.wait() != 0:
            raise RuntimeError('Dependency preparation failed')
    finally:
        if child is not None:
            # A shell can exit before its descendants. Always clean its owned
            # group, even when poll() says the leader has already exited.
            handlers = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)}
            for sig in handlers:
                signal.signal(sig, signal.SIG_IGN)
            try:
                try:
                    os.killpg(child.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                deadline = time.monotonic() + 2
                while group_has_live_processes(child.pid) and time.monotonic() < deadline:
                    time.sleep(0.02)
                try:
                    os.killpg(child.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                child.wait()
                # Retain the deployment lock until no descendant can continue
                # installing; uninterruptible kernel I/O may delay this wait.
                while group_has_live_processes(child.pid):
                    time.sleep(0.02)
            finally:
                for sig, handler in handlers.items():
                    signal.signal(sig, handler)


def prepare_artifact(archive, release, expected_hash):
    if digest_file(archive) != expected_hash:
        raise ValueError('Uploaded archive hash mismatch')
    if release.is_symlink():
        raise ValueError('Release directory cannot be a symlink')
    marker = release / '.artifact-sha256'
    existing = release.exists()
    if existing and (not marker.is_file() or marker.read_text().strip() != expected_hash):
        raise ValueError('Existing release has unverified/conflicting identity; nothing removed')
    with tarfile.open(archive, 'r:') as bundle:
        members = bundle.getmembers()
        seen = set()
        for member in members:
            name = PurePosixPath(member.name)
            if name.is_absolute() or '..' in name.parts or '\\' in member.name or not (member.isfile() or member.isdir()):
                raise ValueError('Unsafe archive member')
            if not name.parts or name.parts[0] in ('.cache', '.artifact-sha256', '.prepared-sha256') or name.parts[:2] == ('backend', '.venv'):
                raise ValueError('Archive contains deployment-managed paths')
            if member.isfile() and member.name in seen:
                raise ValueError('Duplicate archive file')
            seen.add(member.name)
        staging = None
        try:
            target = release
            if not existing:
                release.parent.mkdir(parents=True, exist_ok=True)
                staging = Path(tempfile.mkdtemp(prefix=f'.{release.name}.extract-', dir=release.parent))
                target = staging
                bundle.extractall(target, members=members, filter='data')
            directories = {target}
            for member in members:
                path = target.joinpath(*PurePosixPath(member.name).parts)
                parts = path.relative_to(target).parts
                if any(target.joinpath(*parts[:index]).is_symlink() for index in range(1, len(parts) + 1)):
                    raise ValueError('Release payload contains a symlink')
                directories.update(target.joinpath(*parts[:index]) for index in range(len(parts)))
                if member.isfile():
                    if not path.is_file() or path.stat().st_size != member.size:
                        raise ValueError('Release payload is incomplete')
                    with bundle.extractfile(member) as source:
                        expected = hashlib.file_digest(source, 'sha256').hexdigest()
                    if digest_file(path) != expected:
                        raise ValueError('Release file differs from verified artifact')
                elif not path.is_dir():
                    raise ValueError('Release directory is incomplete')
                else:
                    directories.add(path)
            # data_filter discards directory modes; mkdir then inherits the
            # root worker's private umask. Normalize only verified source
            # directories, never managed venv/cache/state or their targets.
            for path in directories:
                path.chmod(0o755)
            if staging is not None:
                atomic_text(target / '.artifact-sha256', expected_hash + '\n')
                target.rename(release)
                staging = None
        finally:
            if staging is not None:
                try:
                    shutil.rmtree(staging)
                except FileNotFoundError:
                    # A signal can arrive after rename but before clearing the
                    # local staging variable. The verified release is retained.
                    pass
    return (release / '.prepared-sha256').is_file() and (release / '.prepared-sha256').read_text().strip() == expected_hash


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('archive', type=Path)
    parser.add_argument('commit')
    parser.add_argument('sha256')
    parser.add_argument('domain')
    parser.add_argument('--initialize-host', action='store_true')
    args = parser.parse_args()
    if os.geteuid() != 0 or not re.fullmatch('[a-f0-9]{40}', args.commit) or not re.fullmatch('[a-f0-9]{64}', args.sha256):
        raise SystemExit('Root and exact commit/artifact identity required')
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9.-]*\.[A-Za-z]{2,}', args.domain):
        raise SystemExit('Invalid domain')
    release = Path('/opt/keepsite/releases') / args.commit
    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, interrupted)
    with deployment_lock(Path('/opt/keepsite/activation-backups/.lock')):
        if not prepare_artifact(args.archive, release, args.sha256):
            command = ['bash', str(release / 'deploy/ubuntu/install.sh'), '--release', str(release), '--domain', args.domain, '--execute']
            if args.initialize_host:
                command.append('--initialize-host')
            run_installer(command)
            atomic_text(release / '.prepared-sha256', args.sha256 + '\n')
    print(f'Verified and prepared: {release}')


if __name__ == '__main__':
    main()
