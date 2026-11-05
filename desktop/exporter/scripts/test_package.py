"""Packaging orchestration regressions; no compiler, DMG or real artifacts touched."""
import base64
import hashlib
import json
import pathlib
import tempfile
import unittest
from unittest.mock import patch
import package


class FreshOutputTests(unittest.TestCase):
    def test_stale_installer_cannot_satisfy_missing_current_installer(self):
        self.exercise(False)

    def test_success_delivers_only_fresh_target_contents(self):
        self.exercise(True)

    def exercise(self, produces_installer):
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp) / 'source'
            root.mkdir()
            config = {'name': 'Test', 'slug': 'test', 'version': '1.2.3'}
            (root / 'mygo.json').write_text(json.dumps(config))
            out = pathlib.Path(temp) / 'delivery'
            target = out / 'windows-amd64'
            target.mkdir(parents=True)
            installer = target / 'test-setup-1.2.3-amd64.exe'
            installer.write_bytes(b'old installer with exact current version')
            stale = target / 'test-setup-0.9.0-amd64.exe'
            stale.write_bytes(b'old version')

            def build(args, cwd, **kwargs):
                generated = json.loads((cwd / 'mygo.json').read_text())
                fresh = pathlib.Path(generated['out'])
                self.assertNotEqual(fresh, out)
                self.assertFalse(fresh.exists())
                dest = fresh / 'windows-amd64'
                dest.mkdir(parents=True)
                (dest / 'Test.exe').write_bytes(b'fresh executable')
                # MyGo exits successfully when NSIS is absent, leaving no installer.
                if produces_installer:
                    (dest / installer.name).write_bytes(b'fresh installer')
                    (fresh / 'SHA256SUMS').write_text(hashlib.sha256(b'fresh installer').hexdigest()+'  '+installer.name+'\n')

            argv = ['package.py', '--platform', 'windows/amd64', '--out', str(out)]
            with patch.object(package, 'ROOT', root), patch('sys.argv', argv), patch.object(package.subprocess, 'run', side_effect=build), patch.object(package, 'build_windows_installer', return_value=None):
                if produces_installer:
                    package.main()
                    self.assertEqual(installer.read_bytes(), b'fresh installer')
                    self.assertIn(installer.name, (out / 'SHA256SUMS').read_text())
                    self.assertFalse(stale.exists())
                    self.assertEqual((target / 'Test.exe').read_bytes(), b'fresh executable')
                else:
                    with self.assertRaisesRegex(SystemExit, 'Missing required installer'):
                        package.main()
                    self.assertEqual(installer.read_bytes(), b'old installer with exact current version')
                    self.assertTrue(stale.exists())
                    self.assertFalse((target / 'Test.exe').exists())

    def test_wrong_version_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            out = pathlib.Path(temp)
            target = out / 'windows-amd64'
            target.mkdir()
            (target / 'test-setup-1.2.2-amd64.exe').write_bytes(b'wrong version')
            with self.assertRaisesRegex(SystemExit, 'test-setup-1.2.3-amd64.exe'):
                package.validate_artifacts(out, {'slug': 'test', 'version': '1.2.3'}, 'windows/amd64')


class ReleaseDeliveryTests(unittest.TestCase):
    def write_file(self, root, target, name, data):
        p = root / target / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        return hashlib.sha256(data).hexdigest()

    def sums(self, root, entries):
        root.mkdir(parents=True, exist_ok=True)
        (root / 'SHA256SUMS').write_text(''.join(f'{v}  {k}\n' for k, v in entries.items()))

    def test_single_target_rebuild_merges_and_removes_only_replaced_entries(self):
        with tempfile.TemporaryDirectory() as temp:
            out, fresh = pathlib.Path(temp)/'out', pathlib.Path(temp)/'fresh'
            old = self.write_file(out, 'darwin-arm64', 'app-1.0-darwin-arm64.tar.gz', b'old')
            other = self.write_file(out, 'linux-amd64', 'app-1.0-linux-amd64.tar.gz', b'linux')
            shared = self.write_file(out, 'darwin-arm64', 'install.sh', b'shared')
            self.write_file(out, 'linux-amd64', 'install.sh', b'shared')
            new = self.write_file(fresh, 'darwin-arm64', 'app-2.0-darwin-arm64.tar.gz', b'new')
            self.sums(out, {'app-1.0-darwin-arm64.tar.gz': old, 'app-1.0-linux-amd64.tar.gz': other, 'install.sh': shared})
            self.sums(fresh, {'app-2.0-darwin-arm64.tar.gz': new})
            merged = package.merged_checksums(fresh, out, 'darwin/arm64', True)
            self.assertNotIn('app-1.0-darwin', merged)
            self.assertIn(f'{other}  app-1.0-linux-amd64.tar.gz', merged)
            self.assertIn(f'{new}  app-2.0-darwin-arm64.tar.gz', merged)
            self.assertIn(f'{shared}  install.sh', merged)
            # An in-place version rebuild must replace the checksum too.
            new = self.write_file(fresh, 'darwin-arm64', 'app-2.0-darwin-arm64.tar.gz', b'rebuilt')
            self.sums(fresh, {'app-2.0-darwin-arm64.tar.gz': new})
            self.assertIn(new, package.merged_checksums(fresh, out, 'darwin/arm64', True))
            self.write_file(fresh, 'darwin-arm64', 'install.sh', b'conflicting')
            self.sums(fresh, {'install.sh': hashlib.sha256(b'conflicting').hexdigest()})
            with self.assertRaisesRegex(SystemExit, 'Conflicting feed basename'):
                package.merged_checksums(fresh, out, 'darwin/arm64')

    def test_release_requires_valid_root_checksums(self):
        with tempfile.TemporaryDirectory() as temp:
            fresh, out = pathlib.Path(temp)/'fresh', pathlib.Path(temp)/'out'
            digest = self.write_file(fresh, 'darwin-arm64', 'app.tar.gz', b'archive')
            with self.assertRaisesRegex(SystemExit, 'Missing release SHA256SUMS'):
                package.merged_checksums(fresh, out, 'darwin/arm64', True)
            self.sums(fresh, {'app.tar.gz': '0'*64})
            with self.assertRaisesRegex(SystemExit, 'Fresh checksum mismatch'):
                package.merged_checksums(fresh, out, 'darwin/arm64', True)
            self.sums(fresh, {'darwin-arm64/app.tar.gz': digest})
            with self.assertRaisesRegex(SystemExit, 'Invalid or duplicate'):
                package.merged_checksums(fresh, out, 'darwin/arm64', True)

    def test_release_manifest_and_archive_contract(self):
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            config = {'slug': 'app', 'version': '1.2.3', 'updates': {'url': 'https://updates.example.invalid/openaria'}}
            targets = 'darwin/arm64,windows/amd64,linux/amd64'
            for target in targets.split(','):
                target = target.replace('/', '-')
                name = f'app-1.2.3-{target}.tar.gz'
                self.write_file(root, target, name, b'archive')
                m = {'version': '1.2.3', 'url': config['updates']['url']+'/'+name,
                     'size': 7, 'signature': base64.b64encode(b's'*64).decode()}
                path = root/target/f'update-{target}.json'
                path.write_text(json.dumps(m))
            package.validate_release(root, config, targets)
            path = root/'linux-amd64/update-linux-amd64.json'
            valid = json.loads(path.read_text())
            for field, value in [('version', '1.2.2'), ('url', 'https://updates.example.invalid/wrong.tar.gz'), ('size', 8), ('signature', '')]:
                with self.subTest(field=field):
                    path.write_text(json.dumps(dict(valid, **{field: value})))
                    with self.assertRaisesRegex(SystemExit, 'Invalid release artifact'):
                        package.validate_release(root, config, targets)
            path.unlink()
            with self.assertRaisesRegex(SystemExit, 'Invalid release artifact'):
                package.validate_release(root, config, targets)
            path.write_text(json.dumps(valid))
            (root/'linux-amd64/app-1.2.3-linux-amd64.tar.gz').unlink()
            with self.assertRaisesRegex(SystemExit, 'Invalid release artifact'):
                package.validate_release(root, config, targets)


class WindowsRuntimeTests(unittest.TestCase):
    def test_media_payload_reaches_installer_and_uninstaller(self):
        from permission_package import build_windows_installer
        with tempfile.TemporaryDirectory() as temp:
            root=pathlib.Path(temp); src=root/'download'; src.mkdir(); stage=root/'stage'; stage.mkdir()
            pe=bytearray(64); pe[:2]=b'MZ'; pe[60:64]=(64).to_bytes(4,'little'); pe.extend(b'PE\0\0\x64\x86')
            for name in ('ffmpeg.exe','ffprobe.exe'): (src/name).write_bytes(pe)
            (src/'LICENSE').write_text('license'); (src/'README.txt').write_text('source and build information')
            package.copy_windows_media(src,stage)
            folder=stage/'resources/windows-amd64'; (folder/'Test.exe').write_bytes(b'app')
            def build(args, **kwargs):
                script=pathlib.Path(args[1]).read_text()
                for name in ('ffmpeg.exe','ffprobe.exe','FFmpeg-LICENSE.txt','FFmpeg-README.txt'):
                    self.assertIn('File "'+str(folder/name)+'"',script)
                    self.assertIn('Delete "$INSTDIR\\'+name+'"',script)
            with patch('permission_package.subprocess.run',side_effect=build):
                build_windows_installer(stage,folder,{'name':'Test','slug':'test','version':'1.2.3'},'amd64')
            (folder/'ffprobe.exe').unlink()
            with self.assertRaisesRegex(RuntimeError,'Incomplete FFmpeg'):
                build_windows_installer(stage,folder,{'name':'Test','slug':'test','version':'1.2.3'},'amd64')

    def test_media_source_requires_license_and_windows_architecture(self):
        with tempfile.TemporaryDirectory() as temp:
            root=pathlib.Path(temp); src=root/'download'; src.mkdir()
            for name in ('ffmpeg.exe','ffprobe.exe'): (src/name).write_bytes(b'not a Windows binary')
            with self.assertRaisesRegex(SystemExit,'Invalid Windows executable'):
                package.copy_windows_media(src,root/'stage')
            self.assertFalse((root/'stage').exists())


class LinuxRuntimeTests(unittest.TestCase):
    def test_validated_media_and_license_preserve_executable_mode(self):
        with tempfile.TemporaryDirectory() as temp:
            root=pathlib.Path(temp); src=root/'download'; src.mkdir()
            elf=bytearray(64); elf[:6]=b'\x7fELF\x02\x01'; elf[18:20]=(62).to_bytes(2,'little')
            for name in ('ffmpeg','ffprobe'): (src/name).write_bytes(elf)
            (src/'LICENSE').write_text('license'); (src/'README.txt').write_text('source and digest')
            package.copy_linux_media(src,root/'stage')
            folder=root/'stage/resources/linux-amd64'
            self.assertEqual((folder/'ffmpeg').read_bytes(),elf)
            self.assertEqual((folder/'ffprobe').stat().st_mode & 0o777,0o755)
            self.assertEqual((folder/'FFmpeg-LICENSE.txt').read_text(),'license')
            (src/'LICENSE').unlink()
            with self.assertRaisesRegex(SystemExit,'Missing Linux media dependency'):
                package.copy_linux_media(src,root/'missing-license-stage')
            self.assertFalse((root/'missing-license-stage').exists())

    def test_wrong_architecture_is_rejected_before_staging(self):
        with tempfile.TemporaryDirectory() as temp:
            root=pathlib.Path(temp); src=root/'download'; src.mkdir()
            elf=bytearray(64); elf[:6]=b'\x7fELF\x02\x01'; elf[18:20]=(183).to_bytes(2,'little')
            (src/'ffmpeg').write_bytes(elf)
            with self.assertRaisesRegex(SystemExit,'Linux ELF amd64'):
                package.copy_linux_media(src,root/'stage')
            self.assertFalse((root/'stage').exists())


if __name__ == '__main__':
    unittest.main()
