"""Run installer binding logic against isolated fixtures; never touch system paths."""
import os, pathlib, subprocess, tempfile, unittest
ROOT=pathlib.Path(__file__).resolve().parents[1]
class InstallerTests(unittest.TestCase):
 def run_postinstall(self, old_uid, console_uid, symlink_app=False):
  with tempfile.TemporaryDirectory() as tmp:
   root=pathlib.Path(tmp);config=root/'uid';helper=root/'helper';plist=root/'daemon.plist';logs=root/'logs'
   app=root/'Open Aria Exporter.app';outside=root/'outside';outside.mkdir()
   if symlink_app:app.symlink_to(outside, target_is_directory=True)
   else:
    app.mkdir();(app/'binary').write_text('app');(app/'external').symlink_to(outside, target_is_directory=True)
   calls=root/'ownership'
   chown=root/'chown'
   chown.write_text('#!/bin/sh\nprintf "%s\n" "$@" > "'+str(calls)+'"\n')
   chown.chmod(0o755)
   if old_uid is not None:config.write_text(old_uid+'\n')
   helper.write_text('#!/bin/sh\n[ "$1" = --check ]\n');helper.chmod(0o755);plist.touch()
   source=(ROOT/'scripts/installer/postinstall').read_text()
   for a,b in [('/Library/PrivilegedHelperTools/dev.alpen.openaria-exporter.uid',str(config)),('/Library/PrivilegedHelperTools/dev.alpen.openaria-exporter.helper',str(helper)),('/Library/LaunchDaemons/dev.alpen.openaria-exporter.plist',str(plist)),('/Library/Logs/OpenAriaExporter',str(logs)),('/Applications/Open Aria Exporter.app',str(app))]:source=source.replace(a,b)
   # Replace only privileged OS operations. UID selection and preserve branches
   # are the actual shipped shell script, executed without administrator rights.
   source=source.replace('/usr/bin/stat -f %u /dev/console',f'/usr/bin/printf {console_uid}')
   source=source.replace('/usr/bin/id -nu "$uid"','/usr/bin/true')
   source=source.replace('/usr/sbin/chown root:wheel','/usr/bin/true')
   source=source.replace('/usr/bin/install -d -o root -g wheel -m 700','/bin/mkdir -p')
   source=source.replace('/bin/launchctl','/usr/bin/true')
   source=source.replace('/usr/sbin/chown -R',str(chown)+' -R')
   p=root/'postinstall';p.write_text(source)
   result=subprocess.run(['/bin/sh',str(p),'','','/'],capture_output=True,text=True)
   if result.returncode==0:
    self.assertEqual(calls.read_text().splitlines(),['-R','-P','-h','-x','-n',old_uid or console_uid,str(app)])
   if symlink_app:self.assertFalse(calls.exists())
   return result,config.read_text().strip() if config.exists() else None
 def test_upgrade_preserves_original_uid(self):
  result,uid=self.run_postinstall('501','502');self.assertEqual(result.returncode,0,result.stderr);self.assertEqual(uid,'501');self.assertIn('handshake succeeded',result.stdout)
 def test_first_install_binds_console(self):
  result,uid=self.run_postinstall(None,'502');self.assertEqual(result.returncode,0,result.stderr);self.assertEqual(uid,'502')
 def test_no_console_fails(self):
  for uid in ('0','4294967295'):
   result,binding=self.run_postinstall(None,uid);self.assertNotEqual(result.returncode,0);self.assertIsNone(binding)
 def test_symlink_app_rejected(self):
  result,_=self.run_postinstall('501','502',symlink_app=True)
  self.assertNotEqual(result.returncode,0);self.assertIn('real directory',result.stderr)
 def test_physical_chown_does_not_traverse_external_symlink(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=pathlib.Path(tmp);app=root/'App';app.mkdir();outside=root/'outside';outside.mkdir()
   sentinel=outside/'sentinel';sentinel.write_text('unchanged');(app/'link').symlink_to(outside,target_is_directory=True)
   before=sentinel.stat()
   result=subprocess.run(['/usr/sbin/chown','-R','-P','-h','-x','-n','-v',str(os.getuid()),str(app)],capture_output=True,text=True,check=True)
   self.assertNotIn(str(sentinel),result.stdout)
   after=sentinel.stat();self.assertEqual((before.st_uid,before.st_gid,before.st_ctime_ns),(after.st_uid,after.st_gid,after.st_ctime_ns))
 def test_script_syntax_and_uninstall_scope(self):
  for p in (ROOT/'scripts/installer').iterdir():subprocess.run(['/bin/sh','-n',str(p)],check=True)
  text=(ROOT/'scripts/installer/uninstall-service.sh').read_text();self.assertNotIn('rm -rf',text);self.assertNotIn('/Applications/',text)
if __name__=='__main__':unittest.main()
