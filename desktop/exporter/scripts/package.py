#!/usr/bin/env python3
"""Build from an isolated source copy; never put signing secrets in configuration."""
import argparse, base64, hashlib, json, os, pathlib, re, shutil, subprocess, tempfile, urllib.parse
from permission_package import build_pkg, build_windows_installer
ROOT = pathlib.Path(__file__).resolve().parents[1]
def validate_artifacts(out, config, platforms, skip_dmg=False):
 for platform in platforms.split(','):
  system, arch=platform.split('/')
  folder=out/platform.replace('/', '-')
  slug, version=config['slug'], config['version']
  if system=='darwin':
   names=[config['name']+'.app', f"{slug}-{version}-{arch}.pkg"]
   if not skip_dmg: names.append(f"{slug}-{version}"+('' if arch=='universal' else '-'+arch)+'.dmg')
  elif system=='windows': names=[f"{slug}-setup-{version}-{arch}.exe"]
  elif system=='linux': names=[f"{slug}_{version}_{arch}.deb",f"{slug}-{version}-linux-{arch}.tar.gz"]
  else: raise SystemExit('Unsupported platform: '+platform)
  for name in names:
   artifact=folder/name
   valid=artifact.is_dir() if name.endswith('.app') else artifact.is_file() and artifact.stat().st_size>0
   if not valid: raise SystemExit('Missing required installer artifact: '+str(artifact))

def validate_release(out, config, platforms):
 for platform in platforms.split(','):
  target=platform.replace('/', '-')
  folder=out/target
  name=f"{config['slug']}-{config['version']}-{target}.tar.gz"
  manifest=folder/f"update-{target}.json"
  try:
   m=json.loads(manifest.read_text())
   archive=folder/name
   u=config['updates']
   base=u.get('url', '').rstrip('/') or f"https://github.com/{u['github']}/releases/download/{u.get('tagPrefix', 'v')}{config['version']}"
   signature=base64.b64decode(m['signature'], validate=True)
   if (m['version']!=config['version'] or m['url']!=base+'/'+urllib.parse.quote(name, safe='')
       or not archive.is_file() or archive.stat().st_size==0
       or m['size']!=archive.stat().st_size or len(signature)!=64):
    raise ValueError('version, archive URL, size or signature mismatch')
  except (OSError, ValueError, KeyError, TypeError) as e:
   raise SystemExit(f'Invalid release artifact {manifest}: {e}')

def read_checksums(root):
 path=root/'SHA256SUMS'
 if not path.exists(): return {}
 sums={}
 for line in path.read_text().splitlines():
  match=re.fullmatch(r'([0-9a-f]{64}) [ *]([^/\\]+)', line)
  if not match or match[2] in ('.', '..') or match[2] in sums:
   raise SystemExit('Invalid or duplicate SHA256SUMS entry: '+line)
  sums[match[2]]=match[1]
 return sums

def file_digest(path):
 h=hashlib.sha256()
 with path.open('rb') as f:
  for chunk in iter(lambda: f.read(1024*1024), b''): h.update(chunk)
 return h.hexdigest()

def merged_checksums(fresh, out, platforms, release=False):
 targets={p.replace('/', '-') for p in platforms.split(',')}
 current=read_checksums(fresh)
 if release and not current: raise SystemExit('Missing release SHA256SUMS')
 # MyGo lists publishable basenames, not platform-relative paths. Resolve
 # ownership from actual target directories, never by version substring.
 def owners(root, name):
  return [folder/name for folder in root.iterdir() if folder.is_dir() and (folder/name).is_file()] if root.exists() else []
 merged={}
 for name, digest in read_checksums(out).items():
  paths=owners(out, name)
  retained=[p for p in paths if p.parent.name not in targets]
  if not paths: raise SystemExit('Existing checksum has no delivered file: '+name)
  if retained:
   if any(file_digest(p)!=digest for p in retained):
    raise SystemExit('Existing checksum mismatch: '+name)
   merged[name]=digest
 for name, digest in current.items():
  paths=[p for p in owners(fresh, name) if p.parent.name in targets]
  if not paths or any(file_digest(p)!=digest for p in paths):
   raise SystemExit('Fresh checksum mismatch or missing file: '+name)
  # Flat feed names must not refer to different bytes across platforms.
  others=[p for p in owners(out, name) if p.parent.name not in targets]
  if any(file_digest(p)!=digest for p in others):
   raise SystemExit('Conflicting feed basename: '+name)
  merged[name]=digest
 if release:
  for platform in platforms.split(','):
   archives=list((fresh/platform.replace('/', '-')).glob('*.tar.gz'))
   if not archives or any(p.name not in current for p in archives):
    raise SystemExit('Release archive missing from SHA256SUMS: '+platform)
 return ''.join(f'{merged[name]}  {name}\n' for name in sorted(merged))

def copy_windows_media(source, stage):
 source=pathlib.Path(source).resolve()
 names={'ffmpeg.exe':'ffmpeg.exe','ffprobe.exe':'ffprobe.exe','LICENSE':'FFmpeg-LICENSE.txt','README.txt':'FFmpeg-README.txt'}
 for name in names:
  p=source/name
  if not p.is_file(): raise SystemExit('Missing Windows media dependency: '+str(p))
  if name.endswith('.exe'):
   with p.open('rb') as f:
    header=f.read(64)
    if len(header)<64 or header[:2]!=b'MZ': raise SystemExit('Invalid Windows executable: '+str(p))
    f.seek(int.from_bytes(header[60:64],'little')); pe=f.read(6)
    if pe!=b'PE\0\0\x64\x86': raise SystemExit('Media dependency must be Windows amd64: '+str(p))
 target=stage/'resources/windows-amd64'; target.mkdir(parents=True,exist_ok=True)
 for src,dst in names.items(): shutil.copy2(source/src,target/dst)

def copy_linux_media(source, stage):
 source=pathlib.Path(source).resolve()
 names={'ffmpeg':'ffmpeg','ffprobe':'ffprobe','LICENSE':'FFmpeg-LICENSE.txt','README.txt':'FFmpeg-README.txt'}
 for name in names:
  p=source/name
  if not p.is_file(): raise SystemExit('Missing Linux media dependency: '+str(p))
  if name in ('ffmpeg','ffprobe'):
   with p.open('rb') as f: header=f.read(20)
   if len(header)<20 or header[:6]!=b'\x7fELF\x02\x01' or int.from_bytes(header[18:20],'little')!=62:
    raise SystemExit('Media dependency must be Linux ELF amd64: '+str(p))
 target=stage/'resources/linux-amd64'; target.mkdir(parents=True,exist_ok=True)
 for src,dst in names.items():
  shutil.copy2(source/src,target/dst)
  if src in ('ffmpeg','ffprobe'): (target/dst).chmod(0o755)

def copy_macos_media(source, stage):
 source=pathlib.Path(source).resolve()
 for name in ('ffmpeg','ffprobe','LICENSE','README.txt','Homebrew-SOURCES.json'):
  if not (source/name).is_file(): raise SystemExit('Missing macOS media dependency: '+name)
 for name in ('ffmpeg','ffprobe'):
  arches=subprocess.check_output(['lipo','-archs',str(source/name)],text=True).split()
  if 'arm64' not in arches: raise SystemExit('Media dependency must support macOS arm64: '+name)
 if not (source/'lib').is_dir() or not (source/'licenses').is_dir():
  raise SystemExit('Missing macOS runtime libraries or licenses')
 target=stage/'resources/darwin-arm64'
 shutil.copytree(source,target,dirs_exist_ok=True)
 (target/'LICENSE').rename(target/'FFmpeg-LICENSE.txt')
 (target/'README.txt').rename(target/'FFmpeg-README.txt')

def main():
 p=argparse.ArgumentParser()
 p.add_argument('--release', action='store_true')
 p.add_argument('--skip-dmg', action='store_true', help='App-only diagnostic build; does not claim DMG success')
 p.add_argument('--version'); p.add_argument('--url'); p.add_argument('--github')
 p.add_argument('--public-key'); p.add_argument('--private-key')
 p.add_argument('--platform', default='darwin/arm64,windows/amd64,linux/amd64')
 p.add_argument('--out', default=str(ROOT/'build/packages'))
 p.add_argument('--macos-media-dir', help='Relocated arm64 Homebrew media runtime prepared by prepare_media.py')
 p.add_argument('--windows-media-dir', help='Verified amd64 FFmpeg distribution with ffmpeg.exe, ffprobe.exe, LICENSE and README.txt')
 p.add_argument('--linux-media-dir', help='Verified Linux amd64 FFmpeg distribution with ffmpeg, ffprobe, LICENSE and README.txt')
 a=p.parse_args(); c=json.loads((ROOT/'mygo.json').read_text()); env=os.environ.copy()
 env.setdefault('GOCACHE', str(pathlib.Path(tempfile.gettempdir())/'openaria-gocache'))
 out=pathlib.Path(a.out).resolve()
 if a.version: c['version']=a.version
 if not re.fullmatch(r'\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?',c['version']): p.error('invalid version')
 if a.release:
  if not (bool(a.url)^bool(a.github)) or not a.public_key or not a.private_key: p.error('release requires exactly one --url/--github and --public-key + --private-key')
  if a.url:
   u=urllib.parse.urlsplit(a.url)
   if u.scheme!='https' or not u.hostname or u.username or u.password or u.query or u.fragment: p.error('--url requires a clean HTTPS directory URL')
  if a.github and not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', a.github): p.error('invalid GitHub owner/repo')
  key=pathlib.Path(a.private_key).resolve()
  if key.is_relative_to(ROOT): p.error('private key must be outside repository')
  env['MYGO_UPDATER_PRIVATE_KEY']=key.read_text().strip()
  subprocess.run(['go','run','./tools/keycheck',str(pathlib.Path(a.public_key).resolve())],cwd=ROOT,env=env,check=True)
  notes=(ROOT/'CHANGELOG.md').read_text()
  if not re.search(r'^## '+re.escape(c['version'])+r'\s*\n\s*\S',notes,re.M): p.error('CHANGELOG requires a nonempty section for this version')
  c['updates']={'publicKey':pathlib.Path(a.public_key).read_text().strip(),'deltas':0,'changelog':'CHANGELOG.md'}
  c['updates']['url' if a.url else 'github']=a.url or a.github
  env['MYGO_LDFLAGS']='-X main.updatesConfigured=1'
 else:
  if any([a.url,a.github,a.public_key,a.private_key]): p.error('update parameters require --release')
  env.pop('MYGO_UPDATER_PRIVATE_KEY',None); env.pop('MYGO_LDFLAGS',None)
 with tempfile.TemporaryDirectory(prefix='openaria-package-') as temp:
  stage=pathlib.Path(temp)/'source'
  shutil.copytree(ROOT,stage,ignore=shutil.ignore_patterns('.git','build','.cache','*.key'))
  if a.macos_media_dir:
   if 'darwin/arm64' not in a.platform.split(','): p.error('--macos-media-dir requires darwin/arm64')
   copy_macos_media(a.macos_media_dir, stage)
  if a.windows_media_dir:
   if 'windows/amd64' not in a.platform.split(','): p.error('--windows-media-dir requires windows/amd64')
   copy_windows_media(a.windows_media_dir, stage)
  if a.linux_media_dir:
   if 'linux/amd64' not in a.platform.split(','): p.error('--linux-media-dir requires linux/amd64')
   copy_linux_media(a.linux_media_dir, stage)
  fresh=pathlib.Path(temp)/'output'
  c['out']=str(fresh)
  (stage/'mygo.json').write_text(json.dumps(c,indent=2)+'\n')
  subprocess.run(['go','run','github.com/egoist/mygo/cmd/mygo','build','-platform',a.platform,'-sign','-','-skip-notarize']+(['-skip-dmg'] if a.skip_dmg else []),cwd=stage,env=env,check=True)
  # Final installer bytes precede checksum validation/delivery. App bundles and
  # signed update archives remain exactly as produced by MyGo.
  sums=read_checksums(fresh)
  for platform in a.platform.split(','):
   system,arch=platform.split('/'); folder=fresh/platform.replace('/', '-')
   artifact=None
   if system=='darwin': artifact=build_pkg(stage,folder,c,arch,env)
   elif system=='windows': artifact=build_windows_installer(stage,folder,c,arch)
   if artifact: sums[artifact.name]=file_digest(artifact)
   for delivered in folder.iterdir():
    if delivered.is_file() and delivered.name.endswith(('.pkg','.dmg','.exe','.deb','.tar.gz')):
     sums[delivered.name]=file_digest(delivered)
  (fresh/'SHA256SUMS').write_text(''.join(f'{sums[name]}  {name}\n' for name in sorted(sums)))
  validate_artifacts(fresh, c, a.platform, a.skip_dmg)
  if a.release: validate_release(fresh, c, a.platform)
  checksums=merged_checksums(fresh, out, a.platform, a.release)
  # Validate every requested target before delivering any of this build.
  out.mkdir(parents=True, exist_ok=True)
  for platform in a.platform.split(','):
   target=platform.replace('/', '-')
   destination=out/target
   if destination.exists(): shutil.rmtree(destination)
   shutil.copytree(fresh/target, destination)
  (out/'SHA256SUMS').write_text(checksums)
 print('Verified installer artifacts:',out)
if __name__=='__main__': main()
