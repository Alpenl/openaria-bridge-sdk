import hashlib
import io
import json
import pathlib
import tempfile
import unittest
import zipfile
from unittest.mock import patch
import prepare_media


class DownloadTests(unittest.TestCase):
    def fixture(self):
        stream=io.BytesIO()
        with zipfile.ZipFile(stream,'w') as z:
            z.writestr('release/bin/ffmpeg.exe',b'ffmpeg')
            z.writestr('release/bin/ffprobe.exe',b'ffprobe')
            z.writestr('release/LICENSE.txt',b'license')
            z.writestr('../outside',b'must not extract')
            z.writestr('release/setup.py',b'must not execute')
        return stream.getvalue()

    def test_only_named_payloads_are_extracted(self):
        self.exercise(False)

    def test_checksum_mismatch_stops_before_extraction(self):
        self.exercise(True)

    def exercise(self,bad):
        data=self.fixture()
        with tempfile.TemporaryDirectory() as tmp:
            root=pathlib.Path(tmp);dest=root/'media';dest.mkdir();lock=root/'lock.json'
            lock.write_text(json.dumps({'release':'https://example.invalid/release','source':'https://example.invalid/source',
                'windows-amd64':{'url':'https://example.invalid/media.zip','sha256':'0'*64 if bad else hashlib.sha256(data).hexdigest()}}))
            with patch.object(prepare_media,'LOCK',lock),patch.object(prepare_media.urllib.request,'urlopen',return_value=io.BytesIO(data)):
                if bad:
                    with self.assertRaisesRegex(RuntimeError,'SHA256 mismatch'):prepare_media.download_media('windows-amd64',dest)
                    self.assertEqual(list(dest.iterdir()),[])
                else:
                    prepare_media.download_media('windows-amd64',dest)
                    self.assertEqual({p.name for p in dest.iterdir()},{'ffmpeg.exe','ffprobe.exe','LICENSE','README.txt'})
                    self.assertEqual((dest/'ffmpeg.exe').read_bytes(),b'ffmpeg')
                    self.assertFalse((root/'outside').exists())


if __name__=='__main__':unittest.main()
