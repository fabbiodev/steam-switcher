"""Collect redistributable dependency notices for the Windows package."""
from importlib.metadata import distribution, PackageNotFoundError
import json
from pathlib import Path
import shutil
import sys


def collect(root):
    root = Path(root)
    output = root / 'build/third-party-licenses'
    output.mkdir(parents=True, exist_ok=True)
    minhook = root / 'integrations/device/third_party/minhook/LICENSE.txt'
    if minhook.is_file():
        shutil.copyfile(minhook, output / 'MinHook-LICENSE.txt')
    packages = ('PyQt6', 'PyQt6-Qt6', 'PyQt6-sip', 'requests', 'urllib3', 'idna',
                'certifi', 'charset-normalizer', 'psutil', 'pyzipper', 'pycryptodomex',
                'pyinstaller', 'packaging', 'cryptography', 'cffi', 'pycparser', 'pyOpenSSL')
    for name in packages:
        try:
            package = distribution(name)
        except PackageNotFoundError:
            continue
        for file in package.files or ():
            path = Path(str(file))
            if '.dist-info/' not in path.as_posix() or not any(word in path.name.lower() for word in ('license', 'copying', 'notice')):
                continue
            source = Path(package.locate_file(file))
            if source.is_file():
                destination = output / f'{name}-{package.version}' / path.name
                destination.parent.mkdir(exist_ok=True)
                shutil.copyfile(source, destination)
    python_license = Path(sys.base_prefix) / 'LICENSE.txt'
    if python_license.is_file():
        shutil.copyfile(python_license, output / 'Python-LICENSE.txt')
    assets = json.loads((root / 'integrations/steamkit/obj/project.assets.json').read_text(encoding='utf-8'))
    for framework in assets.get('project', {}).get('frameworks', {}).values():
        for dependency in framework.get('downloadDependencies', []):
            name = dependency['name'].lower()
            if name != 'microsoft.netcore.app.runtime.win-x64':
                continue
            version = dependency['version'].strip('[]').split(',')[0].strip()
            for folder in assets.get('packageFolders', {}):
                source = Path(folder) / name / version
                for notice in ('LICENSE.TXT', 'THIRD-PARTY-NOTICES.TXT'):
                    file = source / notice
                    if file.is_file():
                        shutil.copyfile(file, output / f'dotnet-{version}-{notice}')
    return output
