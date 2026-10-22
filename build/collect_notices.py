"""Collect installed license texts for distributions actually resolved by Analysis.

On the first build this uses the core dependency list. After PyInstaller writes
Analysis-00.toc, run this again and rebuild the EXE so optional transitive modules
and vendored notices are represented in the embedded notices file as well.
No packages are imported, downloaded or installed by this script.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import importlib.metadata
import json
from pathlib import Path
import sys


CORE_DISTRIBUTIONS = ('numpy', 'pandas', 'pyarrow', 'scipy', 'pyinstaller',
                      'pyinstaller-hooks-contrib', 'python-dateutil', 'six', 'pytz', 'tzdata', 'python-pptx', 'pypdf', 'Pillow', 'openpyxl', 'lxml', 'et-xmlfile', 'typing_extensions', 'packaging', 'setuptools', 'certifi', 'charset-normalizer', 'idna', 'pywin32', 'Jinja2', 'MarkupSafe', 'threadpoolctl', 'xlsxwriter', 'cryptography', 'cffi', 'pycparser', 'pycryptodome', 'defusedxml')
TOC_TYPES = {'PYMODULE', 'PYSOURCE', 'BINARY', 'EXTENSION', 'DATA'}


def entries(value):
    if isinstance(value, (tuple, list)):
        if len(value) == 3 and all(isinstance(item, str) for item in value) and value[2] in TOC_TYPES:
            yield value
        else:
            for item in value:
                yield from entries(item)


def resolved_distributions(analysis):
    """Map actual site-packages paths, preserving the owner of vendored modules."""
    package_map = importlib.metadata.packages_distributions()
    roots = set()
    if analysis.is_file():
        for _, source, kind in entries(ast.literal_eval(analysis.read_text(encoding='utf-8'))):
            normalized = source.replace('\\', '/')
            if '/site-packages/' not in normalized:
                continue
            root = normalized.split('/site-packages/', 1)[1].split('/')[0]
            if root.endswith(('.dist-info', '.egg-info')):
                continue
            # Extension modules, wheel .libs directories and dist-info metadata
            # all resolve to the same installed distribution owner.
            root = root.split('.')[0]
            if kind in TOC_TYPES:
                roots.add(root)
    # Older wheels such as pywin32 do not publish top_level.txt and therefore
    # may be absent from packages_distributions(). Resolve those by file owner.
    file_owners = {}
    for distribution in importlib.metadata.distributions():
        for relative in distribution.files or []:
            root = str(relative).replace('\\', '/').split('/')[0].split('.')[0]
            if root in roots and root not in package_map:
                file_owners.setdefault(root, set()).add(distribution.metadata['Name'])
    mapping, unresolved = {}, []
    for root in sorted(roots):
        names = package_map.get(root, []) or sorted(file_owners.get(root, []))
        if not names:
            try:
                names = [importlib.metadata.distribution(root).metadata['Name']]
            except importlib.metadata.PackageNotFoundError:
                unresolved.append(root)
                continue
        mapping[root] = sorted(names)
    return mapping, unresolved


def collect(project, analysis):
    mapping, unresolved = resolved_distributions(analysis)
    # Keep explicit core/build-tool notices even when their runtime modules do
    # not appear in the frozen graph (for example the PyInstaller bootloader).
    names = set(CORE_DISTRIBUTIONS) | {name for values in mapping.values() for name in values}
    distributions = {}
    for name in sorted(names, key=str.casefold):
        try:
            item = importlib.metadata.distribution(name)
        except importlib.metadata.PackageNotFoundError:
            continue
        distributions[item.metadata['Name'].casefold()] = item
    sections = ['BatteryInsight integrated — bundled third-party notices\n'
                'These components are bundled to allow offline use without a separate Python installation.\n'
                'This file includes the resolved runtime distributions, embedded vendor notices, and build-tool notices.\n'
                'The application and conversion code do not perform package downloads or send measurement data over the network.\n']
    records = []
    for _, distribution in sorted(distributions.items()):
        name = distribution.metadata['Name']
        sections.append(f'\n===== {name} {distribution.version} =====\n')
        licenses = []
        for relative in sorted(distribution.files or [], key=str):
            filename = Path(relative).name.lower()
            # Include extensionless licenses and nested vendor dist-info files;
            # selecting only the top-level distribution would omit vendor terms.
            if not filename.startswith(('license', 'licence', 'copying', 'notice', 'copyright')):
                continue
            path = Path(distribution.locate_file(relative))
            if path.is_file() and path.stat().st_size < 4_000_000:
                data = path.read_bytes()
                sections.append(f'\n--- {relative} ---\n' + data.decode('utf-8', errors='replace'))
                licenses.append({'path': str(relative).replace('\\', '/'),
                                 'sha256': hashlib.sha256(data).hexdigest()})
        if not licenses:
            sections.append('License metadata: ' + str(distribution.metadata.get('License-Expression')
                                                     or distribution.metadata.get('License')
                                                     or 'See component distribution.'))
        records.append({'name': name, 'version': distribution.version, 'license_files': licenses})
    base = Path(sys.base_prefix)
    runtime_licenses = []
    for path in [base / 'LICENSE.txt', *sorted((base / 'tcl').glob('*/license.terms'))]:
        if path.is_file():
            data = path.read_bytes()
            sections.append(f'\n===== Python/Tcl/Tk {path.name} ({path.parent.name}) =====\n'
                            + data.decode('utf-8', errors='replace'))
            runtime_licenses.append({'component': path.parent.name, 'name': path.name,
                                     'sha256': hashlib.sha256(data).hexdigest()})
    notice = project / 'THIRD_PARTY_NOTICES.txt'
    notice.write_text('\n'.join(sections), encoding='utf-8')
    manifest = {'schema_version': 1, 'analysis_used': analysis.is_file(),
                'analysis_sha256': hashlib.sha256(analysis.read_bytes()).hexdigest() if analysis.is_file() else None,
                'package_distribution_mapping': mapping, 'unresolved_site_packages': unresolved,
                'distributions': records, 'python_tcl_tk_licenses': runtime_licenses,
                'notices_sha256': hashlib.sha256(notice.read_bytes()).hexdigest()}
    (project / 'build' / 'third_party_manifest.json').write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'notices': str(notice), 'analysis_used': manifest['analysis_used'],
                      'distributions': len(records), 'license_files': sum(len(r['license_files']) for r in records),
                      'unresolved': unresolved}, ensure_ascii=False))


if __name__ == '__main__':
    project = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--analysis', type=Path, default=project / 'build/pyinstaller/integrated/Analysis-00.toc')
    args = parser.parse_args()
    collect(project, args.analysis)
