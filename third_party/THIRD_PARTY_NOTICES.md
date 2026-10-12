# Third-party software in Scribe2SRT

Scribe2SRT uses Python, PySide6/Qt, Shiboken, Requests and its dependencies,
OpenCC, and the PyInstaller bootloader. Their licenses and original NOTICE
files are preserved under `licenses/` in the Windows ZIP. These components
retain their respective authors' copyrights and licenses.

## Qt, PySide6 and Shiboken

The unmodified Qt, PySide6 and Shiboken 6.12.0 libraries used by this desktop
application are distributed under LGPL version 3, with their applicable
third-party licenses. Original LGPL/GPL texts, Qt third-party attribution
metadata, versions and copyright notices are in
`licenses/official-sources/Qt-6.12.0/`. Source URLs and SHA-256 hashes of those
notice files are recorded in `licenses/sources.json`.

Corresponding library source is available from the official version tags:

- [Qt Base 6.12.0](https://github.com/qt/qtbase/tree/v6.12.0)
- [Qt SVG 6.12.0](https://github.com/qt/qtsvg/tree/v6.12.0)
- [Qt Image Formats 6.12.0](https://github.com/qt/qtimageformats/tree/v6.12.0)
- [Qt Declarative 6.12.0](https://github.com/qt/qtdeclarative/tree/v6.12.0)
- [PySide6 and Shiboken 6.12.0](https://github.com/pyside/pyside-setup/tree/6.12.0)

Each source page provides a download of that version. The Qt runtime DLLs are
dynamically linked. You may modify and replace these libraries and rebuild
the application with compatible replacements using the complete application
source and [Windows build instructions](https://github.com/WilliamK7/scribe2srt/blob/main/docs/windows-release.md).
For such a rebuild, install your replacement PySide6/Qt distribution in the
build environment and run the documented PyInstaller command, then launch
the generated EXE. The release script's exact-version verification is for
official releases; use the direct build command when changing libraries.
Reverse engineering for debugging modifications to these libraries is
permitted. Scribe2SRT imposes no restriction on exercising LGPL rights.

The application does not use Qt Virtual Keyboard or the PDF image plugin;
its packaging hook excludes both. The source attribution directory also
contains notices for optional code and other platforms in the same official
Qt sources. Those notices do not imply that every component is in this EXE;
the exact bundled file inventory identifies the distributed runtime.

## OpenCC

`opencc-python-reimplemented` 0.1.7 and its dictionaries are provided under
the Apache License, version 2.0. Its original LICENSE and the OpenCC
`NOTICE.txt` are preserved, including the upstream dictionary credits.

## Other components and source provenance

Original wheel license files are copied from the pinned build environment,
including Requests' NOTICE and PyInstaller's bootloader exception. Python's
original LICENSE.txt from the uv-managed interpreter is included for the actual
interpreter version. The Windows CPython 3.15.0 build uses OpenSSL 3.5.9,
Expat 2.8.5 and zlib-ng 2.2.4 (reporting zlib compatibility 1.3.1.zlib-ng).
The zlib-ng fork has its own preserved LICENSE.md; its version is recorded
separately from that compatibility string. Native runtime license sources and
their hashes are recorded alongside those files.
Microsoft runtime files retain Microsoft's terms and copyright notices.

The application is a fork of [cylind/scribe2srt](https://github.com/cylind/scribe2srt).
The upstream README declares MIT; the audited upstream revision contains no
standalone LICENSE file. That declaration and original Git history are
preserved without inventing an upstream copyright holder or changing author
attribution. Third-party licenses apply independently of that declaration.
