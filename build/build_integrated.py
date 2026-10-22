"""Build from locally installed dependencies without installing/downloading."""
from pathlib import Path
import importlib.metadata
import json
import os
import platform
import struct
import subprocess
import sys

root = Path(__file__).resolve().parents[1]
if sys.platform != 'win32' or struct.calcsize('P') != 8:
    raise SystemExit('Build on Windows x64 with Python x64.')
names = ('numpy', 'pandas', 'pyarrow', 'scipy', 'openpyxl', 'python-pptx', 'pypdf', 'Pillow', 'pyinstaller', 'pyinstaller-hooks-contrib')
versions = {name: importlib.metadata.version(name) for name in names}
env = dict(os.environ, OPENBLAS_NUM_THREADS='1', OMP_NUM_THREADS='1')
subprocess.run([sys.executable, '-m', 'PyInstaller', '--noconfirm', '--distpath', str(root / 'dist'),
    '--workpath', str(root / 'build/pyinstaller'), str(root / 'build/integrated.spec')], env=env, cwd=root, check=True)
(root / 'build/build_environment.json').write_text(json.dumps({'python': sys.version, 'platform': platform.platform(),
    'architecture_bits': 64, 'dependencies': versions, 'network_used_by_builder': False,
    'format': 'single-file windowed EXE'}, indent=2), encoding='utf-8')
