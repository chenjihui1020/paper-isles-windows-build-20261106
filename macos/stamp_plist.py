#!/usr/bin/env python3
"""Stamp frozen Finder.app Info.plist *before* code signing."""
from pathlib import Path
import plistlib
import re
import sys


def stamp(plist_path: Path, version: str, identifier: str):
    if not re.fullmatch(r'\d+\.\d+\.\d+', version):
        raise ValueError('VERSION must look like 0.4.2')
    major, minor, patch = (int(part) for part in version.split('.'))
    build_number = major * 10000 + minor * 100 + patch
    info = plistlib.loads(plist_path.read_bytes())
    info.update({
        'CFBundleDevelopmentRegion': 'zh_CN',
        'CFBundleName': 'Paper Isles',
        'CFBundleDisplayName': '纸屿 · Paper Isles',
        'CFBundleIdentifier': identifier,
        'CFBundleShortVersionString': version,
        'CFBundleVersion': str(build_number),
        'LSMinimumSystemVersion': '13.0',
        'NSHighResolutionCapable': True,
        'NSDocumentsFolderUsageDescription': '纸屿读取您主动选择导入的文献 PDF。',
        'NSDownloadsFolderUsageDescription': '纸屿读取您主动选择导入的文献 PDF。',
        'NSHumanReadableCopyright': 'Paper Isles',
        'LSApplicationCategoryType': 'public.app-category.education',
        'LSMultipleInstancesProhibited': True,
    })
    # PyInstaller creates CFBundleExecutable and CFBundleIconFile - preserve them.
    assert info.get('CFBundleExecutable'), 'PyInstaller executable missing'
    assert info.get('CFBundleIconFile'), 'Uploaded app icon not embedded'
    plist_path.write_bytes(plistlib.dumps(info, sort_keys=True))


if __name__ == '__main__':
    stamp(Path(sys.argv[1]), sys.argv[2], sys.argv[3])
