#!/usr/bin/env python3
"""Verify an uploaded artifact, resume only matching releases, prepare dependencies."""
import argparse
import hashlib
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import tarfile


def digest_file(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


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
        if not existing:
            release.mkdir(parents=True, mode=0o755)
            bundle.extractall(release, members=members, filter='data')
        for member in members:
            path = release.joinpath(*PurePosixPath(member.name).parts)
            parts = path.relative_to(release).parts
            if any(release.joinpath(*parts[:index]).is_symlink() for index in range(1, len(parts) + 1)):
                raise ValueError('Release payload contains a symlink')
            if member.isfile():
                if not path.is_file() or path.stat().st_size != member.size:
                    raise ValueError('Release payload is incomplete')
                with bundle.extractfile(member) as source:
                    expected = hashlib.file_digest(source, 'sha256').hexdigest()
                if digest_file(path) != expected:
                    raise ValueError('Release file differs from verified artifact')
        marker.write_text(expected_hash + '\n')
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
    release = Path('/opt/memoverse/releases') / args.commit
    if not prepare_artifact(args.archive, release, args.sha256):
        command = ['bash', str(release / 'deploy/ubuntu/install.sh'), '--release', str(release), '--domain', args.domain, '--execute']
        if args.initialize_host:
            command.append('--initialize-host')
        subprocess.run(command, check=True)
        (release / '.prepared-sha256').write_text(args.sha256 + '\n')
    print(f'Verified and prepared: {release}')


if __name__ == '__main__':
    main()
