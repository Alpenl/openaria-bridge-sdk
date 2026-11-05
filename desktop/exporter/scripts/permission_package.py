"""Project-owned privileged installer payloads; never install during packaging."""
import os, pathlib, plistlib, shutil, subprocess, tempfile

def build_pkg(stage, folder, config, arch, env):
 label='dev.alpen.openaria-exporter'
 with tempfile.TemporaryDirectory(prefix='aria-pkg-') as tmp:
  tmp=pathlib.Path(tmp); root=tmp/'root'; scripts=tmp/'scripts'; scripts.mkdir()
  app=root/'Applications'/(config['name']+'.app'); app.parent.mkdir(parents=True)
  shutil.copytree(folder/app.name,app,symlinks=True)
  helpers=root/'Library/PrivilegedHelperTools'; helpers.mkdir(parents=True)
  helper=helpers/(label+'.helper')
  buildenv=env.copy(); buildenv.update(GOOS='darwin',CGO_ENABLED='1')
  if arch=='universal':
   parts=[]
   for target in ('arm64','amd64'):
    path=tmp/target; buildenv['GOARCH']=target
    subprocess.run(['go','build','-trimpath','-o',str(path),'./tools/roothelper'],cwd=stage,env=buildenv,check=True);parts.append(str(path))
   subprocess.run(['lipo','-create',*parts,'-output',str(helper)],check=True)
  else:
   buildenv['GOARCH']=arch
   subprocess.run(['go','build','-trimpath','-o',str(helper),'./tools/roothelper'],cwd=stage,env=buildenv,check=True)
  helper.chmod(0o755)
  subprocess.run(['codesign','--force','--sign','-',str(helper)],check=True)
  daemons=root/'Library/LaunchDaemons';daemons.mkdir()
  plist={'Label':label,'ProgramArguments':['/Library/PrivilegedHelperTools/'+helper.name],
   'UserName':'root','GroupName':'wheel','Sockets':{'DeviceSocket':{'SockPathName':'/var/run/'+label+'.sock','SockPathMode':0o666,'SockType':'stream'}},
   'ProcessType':'Background','ThrottleInterval':5,'ExitTimeOut':20,'Umask':0o077}
  (daemons/(label+'.plist')).write_bytes(plistlib.dumps(plist))
  for name in ('preinstall','postinstall'):shutil.copy2(stage/'scripts/installer'/name,scripts/name)
  component=tmp/'components.plist'
  subprocess.run(['pkgbuild','--analyze','--root',str(root),str(component)],check=True)
  components=plistlib.loads(component.read_bytes())
  for item in components:
   item['BundleIsRelocatable']=False; item['BundleOverwriteAction']='upgrade'
  component.write_bytes(plistlib.dumps(components))
  target=folder/f"{config['slug']}-{config['version']}-{arch}.pkg"
  subprocess.run(['pkgbuild','--root',str(root),'--component-plist',str(component),'--scripts',str(scripts),'--ownership','recommended','--identifier',label+'.installer','--version',config['version'],'--install-location','/',str(target)],check=True)
  expanded=tmp/'expanded'
  subprocess.run(['pkgutil','--expand-full',str(target),str(expanded)],check=True)
  bom=subprocess.check_output(['lsbom',str(expanded/'Bom')],text=True)
  for line in bom.splitlines():
   fields=line.split('\t')
   if len(fields)>=3 and fields[2]!='0/0': raise RuntimeError('Non-root package owner: '+line)
  info=(expanded/'PackageInfo').read_text()
  assert 'relocatable="false"' in info and 'install-location="/"' in info
  for path,mode in [('Library/PrivilegedHelperTools/'+helper.name,0o755),('Library/LaunchDaemons/'+label+'.plist',0o644)]:
   assert (expanded/'Payload'/path).stat().st_mode & 0o777 == mode
  # Payload byte identity and modes, plus root BOM ownership above.
  payload=expanded/'Payload'
  assert (payload/'Library/PrivilegedHelperTools'/helper.name).read_bytes()==helper.read_bytes()
  assert not (payload/'Library/PrivilegedHelperTools'/(label+'.uid')).exists()
  assert (expanded/'Scripts/postinstall').read_bytes()==(scripts/'postinstall').read_bytes()
  subprocess.run(['codesign','--verify','--deep','--strict',str(payload/'Applications'/app.name)],check=True)
  return target

def build_windows_installer(stage, folder, config, arch):
 # App binary has already reached its final signed state; replacing the NSIS
 # wrapper never mutates the update archive or executable. Hashes follow this.
 exe=folder/(config['name']+'.exe')
 if not exe.is_file():
  exes=[p for p in folder.glob('*.exe') if '-setup-' not in p.name]
  if len(exes)!=1:raise RuntimeError('Cannot identify Windows executable')
  exe=exes[0]
 target=folder/f"{config['slug']}-setup-{config['version']}-{arch}.exe"
 runtime_names=['icon.png','ffmpeg.exe','ffprobe.exe','FFmpeg-LICENSE.txt','FFmpeg-README.txt']
 runtime_files=[folder/name for name in runtime_names if (folder/name).is_file()]
 if any(p.name in ('ffmpeg.exe','ffprobe.exe') for p in runtime_files):
  for name in runtime_names[1:]:
   if not (folder/name).is_file(): raise RuntimeError('Incomplete FFmpeg runtime payload: '+name)
 install_files='\n'.join(f' File "{p}"' for p in runtime_files)
 uninstall_files='\n'.join(f' Delete "$INSTDIR\\{p.name}"' for p in runtime_files)
 with tempfile.TemporaryDirectory(prefix='aria-nsis-') as tmp:
  script=pathlib.Path(tmp)/'installer.nsi'
  # All interpolated fields originate from fixed project config / validated version.
  script.write_text(f'''Unicode true
!include "MUI2.nsh"
Name "Open Aria Exporter"
OutFile "{target}"
InstallDir "$PROGRAMFILES64\\Open Aria Exporter"
RequestExecutionLevel admin
!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH
!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES
!insertmacro MUI_LANGUAGE "English"
Section
 SetShellVarContext all
 SetOutPath "$INSTDIR"
 File "{exe}"
{install_files}
 WriteUninstaller "$INSTDIR\\Uninstall.exe"
 CreateShortcut "$SMPROGRAMS\\Open Aria Exporter.lnk" "$INSTDIR\\{exe.name}"
 WriteRegStr HKLM "Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\OpenAriaExporter" "DisplayName" "Open Aria Exporter"
 WriteRegStr HKLM "Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\OpenAriaExporter" "DisplayVersion" "{config['version']}"
 WriteRegStr HKLM "Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\OpenAriaExporter" "UninstallString" '$\\"$INSTDIR\\Uninstall.exe$\\"'
SectionEnd
Section "Uninstall"
 SetShellVarContext all
 Delete "$SMPROGRAMS\\Open Aria Exporter.lnk"
 Delete "$INSTDIR\\{exe.name}"
{uninstall_files}
 Delete "$INSTDIR\\Uninstall.exe"
 RMDir "$INSTDIR"
 DeleteRegKey HKLM "Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\OpenAriaExporter"
SectionEnd
''')
  subprocess.run(['makensis',str(script)],check=True)
 return target
