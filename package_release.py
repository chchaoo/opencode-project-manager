"""Build release ZIPs using an explicit public-file allowlist. Never glob user data."""
from pathlib import Path
import hashlib
import zipfile

ROOT = Path(__file__).resolve().parent
VERSION = '0.2.0'
SOURCE_FILES = [
    'project_manager.py', 'full_cleanup.py', 'test_project_manager.py', 'test_full_cleanup.py', 'package_release.py',
    'tests/core_schema.json', 'tests/desktop_schema.json',
    'build.ps1', 'requirements-build.txt', 'README.md', 'LICENSE',
    '使用说明.md', '启动项目管理器.cmd', 'RELEASE_NOTES.md',
    '.gitignore', '.github/workflows/build.yml',
]


def package():
    output = ROOT / 'release'
    output.mkdir(exist_ok=True)
    source = output / f'ProjectRecordManager-Source-v{VERSION}.zip'
    binary = output / f'ProjectRecordManager-Windows-x64-v{VERSION}.zip'
    with zipfile.ZipFile(source, 'w', zipfile.ZIP_DEFLATED) as archive:
        for name in SOURCE_FILES:
            archive.write(ROOT / name, name)
    with zipfile.ZipFile(binary, 'w', zipfile.ZIP_DEFLATED) as archive:
        archive.write(ROOT / 'dist/ProjectRecordManager.exe', 'ProjectRecordManager.exe')
        for name in ['README.md', 'LICENSE', '使用说明.md', 'RELEASE_NOTES.md']:
            archive.write(ROOT / name, name)
    hashes = []
    for path in (source, binary):
        with zipfile.ZipFile(path) as archive:
            assert archive.testzip() is None
        hashes.append(hashlib.sha256(path.read_bytes()).hexdigest() + '  ' + path.name)
    (output / 'SHA256SUMS.txt').write_text('\n'.join(hashes) + '\n', encoding='utf-8')
    print('\n'.join(hashes))


if __name__ == '__main__':
    package()
