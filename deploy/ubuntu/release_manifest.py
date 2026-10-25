"""Small, strict deployment manifest shared by CI and the root pull worker."""
import json
import re

REPOSITORY = 'YouXiangyu/network-coding'
PREFIX = 'deploy/keepsite/'
LATEST_KEY = PREFIX + 'latest.json'
MAX_ARCHIVE = 64 * 1024 * 1024
MAX_MANIFEST = 4096
SHA = re.compile(r'[a-f0-9]{40}')
DIGEST = re.compile(r'[a-f0-9]{64}')


def manifest(commit, digest, size):
    value = {'version': 1, 'repository': REPOSITORY, 'commit': commit,
             'sha256': digest, 'size': size,
             'key': f'{PREFIX}artifacts/{commit}/{digest}.tar'}
    return validate(value)


def validate(value):
    if (not isinstance(value, dict) or set(value) != {'version', 'repository', 'commit', 'sha256', 'size', 'key'}
            or type(value['version']) is not int or value['version'] != 1 or value['repository'] != REPOSITORY
            or not isinstance(value['commit'], str) or SHA.fullmatch(value['commit']) is None
            or not isinstance(value['sha256'], str) or DIGEST.fullmatch(value['sha256']) is None
            or type(value['size']) is not int or not 0 < value['size'] <= MAX_ARCHIVE
            or value['key'] != f"{PREFIX}artifacts/{value['commit']}/{value['sha256']}.tar"):
        raise ValueError('Invalid deployment manifest')
    return value


def decode(raw):
    if len(raw) > MAX_MANIFEST:
        raise ValueError('Manifest too large')
    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError('Duplicate manifest key')
            value[key] = item
        return value
    return validate(json.loads(raw, object_pairs_hook=unique))


def encode(value):
    return json.dumps(validate(value), sort_keys=True, separators=(',', ':')).encode()
