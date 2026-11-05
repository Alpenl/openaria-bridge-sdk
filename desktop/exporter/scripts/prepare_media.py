#!/usr/bin/env python3
"""Prepare complete runtime tools without extracting archive paths or running scripts."""
import argparse
import hashlib
import json
import pathlib
import shutil
import subprocess
import tarfile
import tempfile
import urllib.request
import zipfile

LOCK = pathlib.Path(__file__).with_name('media-lock.json')


def download_media(platform, dest):
    lock = json.loads(LOCK.read_text())
    spec = lock[platform]
    suffix = '.exe' if platform == 'windows-amd64' else ''
    with tempfile.TemporaryDirectory(prefix='aria-media-download-') as temp:
        archive = pathlib.Path(temp) / 'download'
        with urllib.request.urlopen(spec['url'], timeout=90) as src, archive.open('wb') as out:
            shutil.copyfileobj(src, out)
        with archive.open('rb') as f:
            checksum = hashlib.sha256()
            for chunk in iter(lambda: f.read(1024*1024), b''):
                checksum.update(chunk)
            digest = checksum.hexdigest()
        if digest != spec['sha256']:
            raise RuntimeError('FFmpeg archive SHA256 mismatch; update the lock only after reviewing the upstream release')
        if suffix:
            bundle = zipfile.ZipFile(archive)
            entries = bundle.namelist()
            read = bundle.read
        else:
            bundle = tarfile.open(archive, 'r:xz')
            entries = [m.name for m in bundle.getmembers() if m.isfile()]
            def read(name):
                with bundle.extractfile(name) as stream:
                    return stream.read()
        with bundle:
            for ending, target in [('bin/ffmpeg'+suffix, 'ffmpeg'+suffix),
                                   ('bin/ffprobe'+suffix, 'ffprobe'+suffix), ('LICENSE.txt', 'LICENSE')]:
                matches = [n for n in entries if n.endswith('/'+ending)]
                if len(matches) != 1:
                    raise RuntimeError('Missing or ambiguous archive member: '+ending)
                (dest/target).write_bytes(read(matches[0]))
                if target != 'LICENSE':
                    (dest/target).chmod(0o755)
    (dest/'README.txt').write_text(
        'FFmpeg / ffprobe GPL build by BtbN\nRelease: '+lock['release']+
        '\nBuild recipes and source references: '+lock['source']+
        '\nArchive: '+spec['url']+'\nSHA256: '+spec['sha256']+
        '\nThese tools run beside the application; system tools are not replaced.\n')


def dependencies(path):
    lines = subprocess.check_output(['otool', '-L', str(path)], text=True).splitlines()[1:]
    return [line.strip().split(' (compatibility version', 1)[0] for line in lines]


def macos_media(dest):
    """Relocate the full Homebrew dylib closure, including license/source metadata."""
    cellar = pathlib.Path(subprocess.check_output(['brew', '--cellar'], text=True).strip()).resolve()
    prefix = pathlib.Path(subprocess.check_output(['brew', '--prefix', 'ffmpeg'], text=True).strip())
    lib = dest/'lib'; lib.mkdir()
    pending = [(prefix/'bin'/name, dest/name) for name in ('ffmpeg', 'ffprobe')]
    visited = {}; edits = {}; formulas = set()
    while pending:
        original, output = pending.pop()
        original = original.resolve()
        if output in visited:
            if visited[output] != original:
                raise RuntimeError('Conflicting dylib basename: '+str(output))
            continue
        if not original.is_relative_to(cellar):
            raise RuntimeError('Unexpected non-Homebrew dependency: '+str(original))
        formulas.add(original.relative_to(cellar).parts[0])
        shutil.copy2(original, output); output.chmod(0o755)
        visited[output] = original; edits[output] = []
        for dep in dependencies(original):
            if dep.startswith(('/System/Library/', '/usr/lib/')):
                continue
            if dep.startswith('@loader_path/'):
                resolved = original.parent / dep.removeprefix('@loader_path/')
            elif dep.startswith('/'):
                resolved = pathlib.Path(dep)
            else:
                raise RuntimeError('Unresolved Mach-O dependency: '+dep)
            if resolved.resolve() == original:
                continue  # dylib install ID, not another dependency
            target = lib/resolved.name
            pending.append((resolved, target))
            replacement = '@loader_path/'+('lib/' if output.parent == dest else '')+target.name
            edits[output].append((dep, replacement))
    for output, changes in edits.items():
        args = ['install_name_tool']
        if output.parent == lib:
            args += ['-id', '@loader_path/'+output.name]
        for old, new in changes:
            args += ['-change', old, new]
        if len(args) > 1:
            subprocess.run([*args, str(output)], check=True)
        subprocess.run(['codesign', '--force', '--sign', '-', str(output)], check=True)
        for dep in dependencies(output):
            if not dep.startswith(('/System/Library/', '/usr/lib/', '@loader_path/')):
                raise RuntimeError('Non-relocatable dependency remains: '+dep)
    info = subprocess.check_output(['brew', 'info', '--json=v2', *sorted(formulas)], text=True)
    (dest/'Homebrew-SOURCES.json').write_text(info)
    licenses = dest/'licenses'; licenses.mkdir()
    for formula in sorted(formulas):
        formula_prefix = pathlib.Path(subprocess.check_output(['brew', '--prefix', formula], text=True).strip())
        for path in formula_prefix.rglob('*'):
            if path.is_file() and (path.name.upper().startswith(('LICENSE', 'COPYING', 'COPYRIGHT')) or path.suffix == '.rb'):
                target = licenses/formula/path.relative_to(formula_prefix)
                target.parent.mkdir(parents=True, exist_ok=True); shutil.copy2(path, target)
    ff_license = subprocess.check_output([str(dest/'ffmpeg'), '-hide_banner', '-L'], stderr=subprocess.STDOUT)
    (dest/'LICENSE').write_bytes(ff_license)
    (dest/'README.txt').write_text('FFmpeg and ffprobe from Homebrew, with their non-system dylib closure.\n'
        'Libraries use relative loader paths; Homebrew is not required at runtime.\n'
        'Homebrew-SOURCES.json records exact installed versions, upstream source URLs and licenses.\n'
        'licenses/ contains available copyright notices and exact Homebrew build recipes.\n')
    for name in ('ffmpeg', 'ffprobe'):
        subprocess.run([str(dest/name), '-version'], check=True, stdout=subprocess.DEVNULL)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--platform', choices=['darwin-arm64', 'linux-amd64', 'windows-amd64'], required=True)
    parser.add_argument('--out', type=pathlib.Path, required=True)
    args = parser.parse_args()
    dest = args.out.resolve()
    dest.mkdir(parents=True, exist_ok=False)
    try:
        if args.platform == 'darwin-arm64':
            macos_media(dest)
        else:
            download_media(args.platform, dest)
    except BaseException:
        shutil.rmtree(dest)
        raise
    print('Prepared media:', dest)


if __name__ == '__main__':
    main()
