# Third-party components

Steam Switcher is distributed under GPL-3.0-only. The application source and build
scripts are available at https://github.com/fabbiodev/steam-switcher.

- **SteamKit2 3.4.0** — LGPL-2.1. Source: https://github.com/SteamRE/SteamKit.
  License: `assets/licenses/SteamKit2-LICENSE.txt` in the source tree,
  `_internal/assets/licenses/SteamKit2-LICENSE.txt` in the Windows package.
  The C# helper source is in `steamkit/`. Build it with `dotnet publish` or
  `python tools/build.py`; the helper is a separate executable and can be replaced.
- **PyQt6** — GPL v3. Source and license: https://www.riverbankcomputing.com/software/pyqt/.
- **Qt** — LGPL/GPL licenses, depending on the module. Source and license:
  https://www.qt.io/qt-licensing.
- **.NET runtime** — MIT. Source and license: https://github.com/dotnet/runtime.
- **Requests** — Apache-2.0. https://github.com/psf/requests.
- **psutil** — BSD-3-Clause. https://github.com/giampaolo/psutil.
- **pyzipper** — MIT and Python Software Foundation licenses.
  https://github.com/danifus/pyzipper.
- **PyCryptodome / PyCryptodomex** — BSD and public-domain components.
  https://github.com/Legrandin/pycryptodome.
- **PyInstaller bootloader** — GPL with an exception permitting distribution of
  bundled applications. https://github.com/pyinstaller/pyinstaller.
- **Inter** — SIL Open Font License 1.1. License: `assets/fonts/OFL.txt` in the source
  tree and `_internal/assets/fonts/OFL.txt` in the Windows package.

Dependencies retain their own copyright and license notices. The Windows package
includes collected dependency license texts under `_internal/licenses/`.

Steam and Steam Guard are trademarks of Valve Corporation. This project is not
affiliated with Valve.

# Нативный модуль устройства

MinHook 1.3.4 используется для перехвата сборщиков идентификатора в процессе Steam.
Исходники и условия BSD находятся в `native/vendor/minhook/`; копия лицензии входит
в сборку. Ревизия исходников: `c3fcafdc10146beb5919319d0683e44e3c30d537`.
