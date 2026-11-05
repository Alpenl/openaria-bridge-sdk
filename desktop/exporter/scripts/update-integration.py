#!/usr/bin/env python3
"""Real Updater Check/Install on isolated macOS app copies, no user recordings."""
import hashlib,json,os,pathlib,subprocess,tempfile,time,shutil,sys,plistlib
root=pathlib.Path(__file__).resolve().parents[1]
def run(args,**kw): return subprocess.run(args,cwd=root,check=True,**kw)
if sys.platform!='darwin': raise SystemExit('This acceptance harness requires macOS; cross-platform installers are tested separately.')
with tempfile.TemporaryDirectory(prefix='openaria-update-qa-') as temp:
 d=pathlib.Path(temp);old=d/'installed'/'Open Aria Exporter.app';new=d/'new'/'Open Aria Exporter.app'
 env=os.environ.copy();env['GOCACHE']='/tmp/openaria-gocache'
 def build(bundle,version,key):
  exe=bundle/'Contents/MacOS/Open Aria Exporter';exe.parent.mkdir(parents=True,exist_ok=True)
  (bundle/'Contents/Info.plist').write_bytes(plistlib.dumps({'CFBundleExecutable':'Open Aria Exporter','CFBundleIdentifier':'dev.alpen.openaria-qa','CFBundleName':'OpenAriaQA','CFBundleShortVersionString':version,'CFBundleVersion':version,'CFBundlePackageType':'APPL'}))
  flags=f'-X github.com/egoist/mygo.packageName=OpenAriaQA -X github.com/egoist/mygo.packageIdentifier=dev.alpen.openaria-qa -X github.com/egoist/mygo.production=1 -X github.com/egoist/mygo.packageVersion={version} -X github.com/egoist/mygo.packageUpdateFeed=fixture -X github.com/egoist/mygo.packageUpdateKey={key}'
  run(['go','build','-tags','updateqa','-ldflags',flags,'-o',str(exe),'.'],env=env)
  return exe
 build(new,'0.1.1','unused')
 run(['go','run','./tools/updatefixture',str(d),str(new)],env=env)
 exe=build(old,'0.1.0',(d/'public').read_text())
 before=hashlib.sha256(exe.read_bytes()).hexdigest();manifest=(d/'manifest.json').read_bytes()
 bad=json.loads(manifest);bad['signature']='A'*88;(d/'manifest.json').write_text(json.dumps(bad))
 result=subprocess.run([str(exe),'update-qa',str(d)],capture_output=True,text=True)
 assert result.returncode==3 and 'not signed' in result.stderr, result
 assert hashlib.sha256(exe.read_bytes()).hexdigest()==before
 assert not list(old.parent.glob('.*.update-*')),'failed signature left staging files'
 print('PASS: incorrect signature rejected; original executable unchanged; staging cleaned')
 (d/'manifest.json').write_bytes(manifest)
 # Capture output in files: updater helper inherits stdout and waits for parent exit.
 with (d/'install.log').open('w') as log: run([str(exe),'update-qa',str(d)],stdout=log,stderr=log)
 deadline=time.monotonic()+15
 while time.monotonic()<deadline:
  if exe.exists() and hashlib.sha256(exe.read_bytes()).hexdigest()!=before:break
  time.sleep(.1)
 assert exe.read_bytes()==(new/'Contents/MacOS/Open Aria Exporter').read_bytes(),'new version not installed'
 result=run([str(exe),'update-qa',str(d)],capture_output=True,text=True)
 assert 'RESTARTED 0.1.1' in result.stdout,result.stdout
 print('PASS: real Check/Install replaced isolated app; restarted new executable 0.1.1')
run(['go','test','-run','TestBusyExitProtection|TestUpdateUnavailable','-count=1','.'],env=env)
