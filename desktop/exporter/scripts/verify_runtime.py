#!/usr/bin/env python3
"""Smoke test delivered media and stage only complete, checksummed installers."""
import argparse
import hashlib
import json
import os
import pathlib
import shutil
import subprocess
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--platform', required=True, choices=['darwin-arm64', 'windows-amd64', 'linux-amd64'])
    parser.add_argument('--build', type=pathlib.Path, default=ROOT/'build/packages')
    parser.add_argument('--out', type=pathlib.Path, default=ROOT/'build/deliverables')
    args = parser.parse_args()
    config = json.loads((ROOT/'mygo.json').read_text())
    folder = args.build.resolve()/args.platform
    version, slug = config['version'], config['slug']
    if args.platform == 'darwin-arm64':
        app = folder/(config['name']+'.app')
        media = app/'Contents/Resources'
        subprocess.run(['codesign', '--verify', '--deep', '--strict', str(app)], check=True)
        installers = [f'{slug}-{version}-arm64.pkg']
        dmg = folder/f'{slug}-{version}-arm64.dmg'
        if dmg.exists(): installers.append(dmg.name)
    elif args.platform == 'windows-amd64':
        media = folder
        installers = [f'{slug}-setup-{version}-amd64.exe']
    else:
        media = folder
        installers = [f'{slug}_{version}_amd64.deb', f'{slug}-{version}-linux-amd64.tar.gz', 'install.sh']
    suffix = '.exe' if args.platform == 'windows-amd64' else ''
    for name in ['ffmpeg'+suffix, 'ffprobe'+suffix, 'FFmpeg-LICENSE.txt', 'FFmpeg-README.txt']:
        if not (media/name).is_file():
            raise RuntimeError('Incomplete runtime: '+str(media/name))
    env = os.environ.copy()
    env.pop('OPENARIA_FFMPEG_DIR', None)
    # Invoke exact packaged tools. Clear loader overrides so local libraries cannot mask missing payload.
    for name in list(env):
        if name.startswith(('DYLD_', 'LD_LIBRARY_PATH')):env.pop(name)
    with tempfile.TemporaryDirectory(prefix='aria-runtime-') as tmp:
        movie = pathlib.Path(tmp)/'smoke.mp4'
        subprocess.run([str(media/('ffmpeg'+suffix)), '-v', 'error', '-nostdin',
                        '-f', 'lavfi', '-i', 'testsrc2=size=192x64:rate=24',
                        '-f', 'lavfi', '-i', 'sine=frequency=1000:sample_rate=48000',
                        '-t', '1', '-c:v', 'libx264', '-c:a', 'aac',
                        '-fps_mode', 'passthrough', '-shortest', '-shortest_buf_duration', '.1', str(movie)],
                       env=env, check=True, timeout=30)
        info = json.loads(subprocess.check_output([str(media/('ffprobe'+suffix)), '-v', 'error',
                        '-count_frames', '-show_streams', '-of', 'json', str(movie)], env=env, timeout=30))
        video = next(s for s in info['streams'] if s['codec_type']=='video')
        assert video['width']==192 and video['height']==64 and int(video['nb_read_frames'])==24
        assert any(s['codec_name']=='aac' for s in info['streams'])
    dest = args.out.resolve()/args.platform
    dest.mkdir(parents=True, exist_ok=False)
    sums = []
    for name in installers:
        source = folder/name
        if not source.is_file() or source.stat().st_size==0:
            raise RuntimeError('Missing installer: '+str(source))
        shutil.copy2(source, dest/name)
        with source.open('rb') as f:
            checksum=hashlib.sha256()
            for chunk in iter(lambda:f.read(1024*1024),b''):checksum.update(chunk)
            digest=checksum.hexdigest()
        sums.append(digest+'  '+name+'\n')
    (dest/'SHA256SUMS').write_text(''.join(sums))
    (dest/'BUILD-INFO.json').write_text(json.dumps({'version':version,'platform':args.platform,
        'commit':os.environ.get('GITHUB_SHA','local'),'media_smoke_test':'24 video frames + AAC',
        'signing':'ad-hoc macOS; unsigned Windows; no notarization or automatic updater'},indent=2)+'\n')
    print('Verified deliverables:',dest)


if __name__ == '__main__':main()
