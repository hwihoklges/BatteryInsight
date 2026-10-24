from pathlib import Path
project = Path(SPECPATH).resolve().parent
package = project / 'battery_insight'
import cyc2parquet
cyc2parquet_dir = Path(cyc2parquet.__file__).resolve().parent
datas = []
datas += [(str(path), 'battery_insight') for path in package.glob('*.py')]
datas += [(str(path), 'cyc2parquet') for path in cyc2parquet_dir.glob('*.py')]
for directory in ('assets', 'resources'):
    for path in (package / directory).rglob('*'):
        if path.is_file() and '__pycache__' not in path.parts:
            datas.append((str(path), 'battery_insight/' + path.parent.relative_to(package).as_posix()))
datas.append((str(project / 'THIRD_PARTY_NOTICES.txt'), '.'))
a = Analysis([str(project / 'launch_battery_insight.py')], pathex=[str(project)], binaries=[], datas=datas,
    hiddenimports=['pyarrow._parquet', 'pyarrow._compute', 'scipy.signal', 'tkinter', 'tkinter.ttk',
                   'tkinter.filedialog', 'tkinter.messagebox', 'pptx', 'pypdf', 'openpyxl', 'PIL.Image',
                   'cyc2parquet', 'cyc2parquet.reader', 'cyc2parquet.service', 'cyc2parquet.selftest'],
    runtime_hooks=[str(project / 'build/runtime_hook.py')],
    excludes=['matplotlib', 'seaborn', 'IPython', 'jupyter', 'notebook', 'torch', 'tensorflow', 'sklearn',
              'xgboost', 'lightgbm', 'PyQt5', 'PyQt6', 'PySide2', 'PySide6', 'pytest', 'numba', 'llvmlite',
              'sympy', 'dask', 'distributed', 'bokeh', 'plotly', 'streamlit', 'requests', 'urllib3', 'fsspec'],
    noarchive=False)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, a.binaries, a.datas, [], name='BatteryInsight', debug=False,
    bootloader_ignore_signals=False, strip=False, upx=False, runtime_tmpdir=None, console=False,
    disable_windowed_traceback=False, version=str(project / 'build/version_info.txt'))
