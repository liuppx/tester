"""Regression checks for the actual Linux browser namespace boundary."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
WRAPPER = ROOT / 'scripts/browser-runtime.sh'


class BrowserRuntimeArgumentTests(unittest.TestCase):
    def test_missing_command_is_rejected(self):
        result = subprocess.run(['bash', str(WRAPPER)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)

    def test_linux_without_bubblewrap_never_runs_target(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            uname = root / 'uname'
            uname.write_text('#!/bin/sh\nprintf "Linux\\n"\n')
            uname.chmod(0o700)
            marker = root / 'target-ran'
            result = subprocess.run(
                [shutil.which('bash'), str(WRAPPER), sys.executable, '-c',
                 'from pathlib import Path; Path(' + repr(str(marker)) + ').touch()'],
                env={**os.environ, 'PATH': folder}, capture_output=True, text=True)
            self.assertEqual(result.returncode, 3)
            self.assertIn('requires Bubblewrap', result.stderr)
            self.assertFalse(marker.exists())


@unittest.skipUnless(sys.platform.startswith('linux') and shutil.which('bwrap')
                     and (os.environ.get('DISPLAY') or shutil.which('xvfb-run')),
                     'Linux with Bubblewrap and a display/Xvfb required')
class BrowserNamespaceTests(unittest.TestCase):
    def test_namespace_masks_real_sysfs_and_preserves_argv_environment_and_exit(self):
        probe = r'''
import errno, json, os, sys
from pathlib import Path
assert not list(Path('/sys').iterdir())
assert not Path('/sys/devices').exists()
assert not Path('/sys/bus/pci/devices').exists()
assert not Path('/dev/dri').exists()
assert not list(Path('/dev').glob('nvidia*'))
assert not list(Path('/run/udev').iterdir())
try:
    Path('/sys/tester-probe').touch()
except OSError as error:
    assert error.errno == errno.EROFS
else:
    raise AssertionError('/sys is writable')
assert os.environ['TESTER_CPU_BROWSER'] == '1'
assert os.environ['LIBGL_ALWAYS_SOFTWARE'] == '1'
assert os.environ['DISPLAY']
assert sys.argv[1] == 'argument with spaces'
print(json.dumps({'isolated': True}))
sys.exit(7)
'''
        sysfs_before = sorted(p.name for p in Path('/sys').iterdir())
        result = subprocess.run(['bash', str(WRAPPER), sys.executable, '-c', probe,
                                 'argument with spaces'],
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 7, result.stderr)
        self.assertTrue(json.loads(result.stdout)['isolated'])
        self.assertEqual(sysfs_before, sorted(p.name for p in Path('/sys').iterdir()))


if __name__ == '__main__':
    unittest.main()
