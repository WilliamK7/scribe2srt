"""Keep Qt's normal GUI dependencies without unused PDF/virtual-keyboard plugins.

Scribe2SRT uses desktop widgets and does not load either plugin. In particular,
Qt Virtual Keyboard is GPL-only and must not be accidentally added by a broad
plugin glob to this application's LGPL Qt runtime.
"""

from pathlib import Path

from PyInstaller.utils.hooks.qt import add_qt6_dependencies

hiddenimports, binaries, datas = add_qt6_dependencies(__file__)
binaries = [(source, destination) for source, destination in binaries
            if Path(source).name.lower() not in {"qpdf.dll", "qtvirtualkeyboardplugin.dll"}]
