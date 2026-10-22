"""Dependency direction is an architectural contract, not a naming convention."""
import ast
from pathlib import Path
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]


def imports(path):
    package = path.relative_to(ROOT).with_suffix('').parts[:-1]
    for node in ast.walk(ast.parse(path.read_text(encoding='utf-8'))):
        if isinstance(node, ast.Import):
            yield from (n.name for n in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                parent = package[:len(package) - node.level + 1]
                yield '.'.join((*parent, node.module or ''))
            else:
                yield node.module or ''


class ArchitectureTests(unittest.TestCase):
    def test_domain_and_application_have_no_gui_windows_network_or_adapter_dependencies(self):
        forbidden = {'PyQt6', 'requests', 'psutil', 'ctypes', 'winreg', 'subprocess', 'pyzipper'}
        for layer in ('domain', 'application'):
            for file in (ROOT/'steam_sda'/layer).glob('*.py'):
                for module in imports(file):
                    with self.subTest(layer=layer, file=file.name, module=module):
                        self.assertNotIn(module.split('.')[0], forbidden)
                        if module.startswith('steam_sda.'):
                            self.assertIn(module.split('.')[1], {'domain'} if layer == 'domain' else {'domain', 'application'})

    def test_adapters_never_import_application_or_presentation(self):
        for file in (ROOT/'steam_sda/infrastructure').rglob('*.py'):
            for module in imports(file):
                if module.startswith('steam_sda.'):
                    self.assertIn(module.split('.')[1], {'domain', 'infrastructure'}, (str(file), module))

    def test_qt_view_and_workers_have_no_direct_adapters_or_process_file_operations(self):
        for name in ('window.py', 'dialogs.py', 'workers.py', 'widgets.py'):
            file = ROOT/'steam_sda/presentation/qt'/name
            for module in imports(file):
                self.assertFalse(module.startswith(('steam_sda.infrastructure', 'steam_sda.bootstrap')), (name, module))
            for node in ast.walk(ast.parse(file.read_text(encoding='utf-8'))):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                    self.assertNotIn(node.func.id, {'Store', 'AccountVault', 'NativeSteam', 'SteamKitGateway', 'NativeDeviceGateway', 'open'}, name)
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                    owner = node.func.value
                    if isinstance(owner, ast.Attribute) and isinstance(owner.value, ast.Name) and owner.value.id == 'self':
                        self.assertNotIn(owner.attr, {'native', 'vault', 'store'}, (name, node.func.attr))

    def test_application_imports_work_without_loading_qt_or_external_adapters(self):
        script = ('import sys; import steam_sda.application.accounts; import steam_sda.application.authentication; '
                  'import steam_sda.application.devices; import steam_sda.application.switching; '
                  'assert not any(n.startswith(("PyQt6", "requests", "psutil", "steam_sda.infrastructure")) for n in sys.modules)')
        result = subprocess.run([sys.executable, '-c', script], cwd=ROOT, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr.decode())
