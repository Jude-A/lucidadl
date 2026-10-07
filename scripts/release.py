"""Small release checks; network failures and conflicting publications fail closed."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.request


TAG = re.compile(r"v(\d+\.\d+\.\d+)\Z")


def version_from_tag(tag):
    match = TAG.fullmatch(tag)
    if not match:
        raise ValueError(f"Expected a stable vX.Y.Z tag, got {tag!r}")
    return match[1]


def source_version(root=Path('.')):
    # Read without importing the package or its dependencies.
    values = [
        re.search(r'^version = "([^"]+)"', (root / 'pyproject.toml').read_text(), re.M)[1],
        re.search(r'^__version__ = "([^"]+)"', (root / 'lucidadl/__init__.py').read_text(), re.M)[1],
        re.search(r'"(\d+\.\d+\.\d+)" in _version.output', (root / 'selftest.py').read_text())[1],
        json.loads((root / '.release-please-manifest.json').read_text())['.'],
    ]
    if len(set(values)) != 1:
        raise ValueError(f"Version mismatch: {values}")
    return values[0]


def distributions(version, directory=Path('dist')):
    names = {f'lucidadl-{version}-py3-none-any.whl', f'lucidadl-{version}.tar.gz'}
    files = {p.name: p for p in directory.iterdir() if p.is_file()}
    if set(files) != names:
        raise ValueError(f"Expected wheel and sdist for {version}, found {sorted(files)}")
    return files


def pypi_files(version):
    url = f'https://pypi.org/pypi/lucidadl/{version}/json'
    try:
        with urllib.request.urlopen(url, timeout=30) as response:
            return json.load(response)['urls']
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return []
        raise


def missing_distributions(files, remote):
    missing = set(files)
    for item in remote:
        name = item['filename']
        if name not in files:
            raise ValueError(f'Unexpected PyPI file: {name}')
        digest = hashlib.sha256(files[name].read_bytes()).hexdigest()
        if item['digests']['sha256'] != digest or item.get('yanked', False):
            raise ValueError(f'PyPI file differs or is yanked: {name}; inspect before retrying')
        missing.discard(name)
    return sorted(missing)


def verify_publication(version, files):
    # PyPI's JSON API can lag behind a successful upload. Only missing files
    # are retried; conflicting files and network/API errors still fail immediately.
    for attempt in range(6):
        missing = missing_distributions(files, pypi_files(version))
        if not missing:
            print(f'PyPI {version}: both distributions verified by SHA-256')
            return
        if attempt < 5:
            print(f'Waiting for PyPI to list {missing}; checking again in 10 seconds', flush=True)
            time.sleep(10)
    raise ValueError(f'PyPI publication incomplete: {missing}; retry verification later')


def finalize_changelog(content, version):
    """Move curated Unreleased notes into Release Please's new section."""
    chunks = re.split(r'(?=^## )', content, flags=re.M)
    pending = next((x for x in chunks if x.startswith('## [Unreleased]')), None)
    index = next(i for i, x in enumerate(chunks) if re.match(r'## \[' + re.escape(version) + r'\]', x))
    heading, _, generated = chunks[index].partition('\n')
    curated = pending.partition('\n')[2].strip() if pending else ''
    notes = heading + '\n\n' + (curated or generated.strip()) + '\n'
    chunks[index] = notes + '\n'
    if pending:
        chunks.remove(pending)
    chunks.insert(1, '## [Unreleased]\n\n')
    result = ''.join(chunks)
    result = re.sub(r'(?m)^\[Unreleased\]: .*$',
                    f'[Unreleased]: https://github.com/Jude-A/lucidadl/compare/v{version}...HEAD', result)
    return result, notes


def finalize_pr():
    version = source_version()
    path = Path('CHANGELOG.md')
    content, notes = finalize_changelog(path.read_text(), version)
    path.write_text(content)
    # Keep the Release Please delimiters: its release notes come from this body.
    body = json.loads(subprocess.check_output(
        ['gh', 'pr', 'view', os.environ['RELEASE_PR'], '--json', 'body'], text=True))['body']
    parts = re.split(r'^---\s*$', body, flags=re.M)
    if len(parts) != 3:
        raise ValueError('Unexpected Release Please body format')
    notes = notes.rstrip() + (
        '\n\n### Upgrade\n\n'
        'With pipx: `pipx upgrade lucidadl`\n\n'
        'With pip: `pip install --upgrade lucidadl`\n'
    )
    Path('release-pr-body.txt').write_text(parts[0].rstrip() + '\n---\n\n' + notes + '\n---\n' + parts[2].lstrip())


def update_nix(version, digest, path=Path('nix/default.nix')):
    text = path.read_text()
    current = re.search(r'\bversion = "(\d+\.\d+\.\d+)";', text)
    if not current or 'rev = "v${version}";' not in text:
        raise ValueError('Nix source format changed; update the release helper first')
    if tuple(map(int, version.split('.'))) <= tuple(map(int, current[1].split('.'))):
        return
    if not re.fullmatch(r'sha256-[A-Za-z0-9+/]{43}=', digest):
        raise ValueError('Expected a SHA-256 SRI hash')
    text = text[:current.start(1)] + version + text[current.end(1):]
    text, count = re.subn(r'\bhash = "sha256-[A-Za-z0-9+/=]+";', f'hash = "{digest}";', text)
    if count != 1:
        raise ValueError('Expected exactly one Nix source hash')
    path.write_text(text)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=['check', 'finalize', 'stage', 'verify', 'nix'])
    parser.add_argument('--tag')
    parser.add_argument('--hash')
    args = parser.parse_args()
    if args.command == 'finalize':
        finalize_pr()
        return
    if args.command == 'check' and not args.tag:
        print(source_version())
        return
    version = version_from_tag(args.tag)
    if args.command == 'check':
        if source_version() != version:
            raise ValueError('Tag and source versions differ')
    elif args.command == 'nix':
        update_nix(version, args.hash)
    else:
        files = distributions(version)
        if args.command == 'verify':
            verify_publication(version, files)
        else:
            missing = missing_distributions(files, pypi_files(version))
            destination = Path('pending-dist')
            destination.mkdir(exist_ok=False)
            for name in missing:
                shutil.copy2(files[name], destination / name)
            with open(os.environ['GITHUB_OUTPUT'], 'a') as output:
                output.write(f'pending={str(bool(missing)).lower()}\n')


if __name__ == '__main__':
    main()
