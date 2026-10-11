"""Unicode shell links via IShellLinkW/IPersistFile, independent of Windows ACP.

Windows Script Host can lose non-ANSI characters in the .lnk filename. These
wide-character COM interfaces keep both the shortcut path and its properties.
No COM package or extra runtime dependency is required.
"""

import ctypes
from ctypes import wintypes
from pathlib import Path
import uuid


class _GUID(ctypes.Structure):
    _fields_ = [("data1", ctypes.c_uint32), ("data2", ctypes.c_uint16),
                ("data3", ctypes.c_uint16), ("data4", ctypes.c_ubyte * 8)]

    @classmethod
    def parse(cls, value):
        return cls.from_buffer_copy(uuid.UUID(value).bytes_le)


_CLSID_SHELL_LINK = _GUID.parse("00021401-0000-0000-c000-000000000046")
_IID_SHELL_LINK_W = _GUID.parse("000214f9-0000-0000-c000-000000000046")
_IID_PERSIST_FILE = _GUID.parse("0000010b-0000-0000-c000-000000000046")
_HRESULT = ctypes.c_int32


def _check(result):
    if result < 0:
        raise OSError(f"Shell link COM failed: HRESULT 0x{result & 0xffffffff:08X}")


class ShellLink:
    """Own COM initialization and both interface references on the calling thread."""

    def __init__(self):
        self.link = ctypes.c_void_p()
        self.persist = ctypes.c_void_p()
        self.initialized = False

    def __enter__(self):
        self.ole = ctypes.WinDLL("ole32")
        self.ole.CoInitializeEx.argtypes = [ctypes.c_void_p, wintypes.DWORD]
        self.ole.CoInitializeEx.restype = _HRESULT
        self.ole.CoCreateInstance.argtypes = [ctypes.POINTER(_GUID), ctypes.c_void_p,
                                            wintypes.DWORD, ctypes.POINTER(_GUID),
                                            ctypes.POINTER(ctypes.c_void_p)]
        self.ole.CoCreateInstance.restype = _HRESULT
        self.ole.CoUninitialize.argtypes = []
        self.ole.CoUninitialize.restype = None
        result = self.ole.CoInitializeEx(None, 2)  # COINIT_APARTMENTTHREADED
        # Qt or another host may have initialized this thread in a different mode.
        if result != -2147417850:  # RPC_E_CHANGED_MODE: keep the existing apartment.
            _check(result)
            self.initialized = True
        try:
            _check(self.ole.CoCreateInstance(ctypes.byref(_CLSID_SHELL_LINK), None, 1,
                                             ctypes.byref(_IID_SHELL_LINK_W), ctypes.byref(self.link)))
            self._call(self.link, 0, [ctypes.POINTER(_GUID), ctypes.POINTER(ctypes.c_void_p)],
                       ctypes.byref(_IID_PERSIST_FILE), ctypes.byref(self.persist))
            return self
        except Exception:
            self.__exit__(None, None, None)
            raise

    @staticmethod
    def _method(pointer, index, arguments, result=_HRESULT):
        table = ctypes.cast(pointer, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
        return ctypes.WINFUNCTYPE(result, ctypes.c_void_p, *arguments)(table[index])

    def _call(self, pointer, index, types, *arguments):
        _check(self._method(pointer, index, types)(pointer, *arguments))

    def __exit__(self, *exception):
        for pointer in (self.persist, self.link):
            if pointer.value:
                self._method(pointer, 2, [], ctypes.c_uint32)(pointer)  # IUnknown::Release
                pointer.value = None
        if self.initialized:
            self.ole.CoUninitialize()
            self.initialized = False

    def load(self, path):
        self._call(self.persist, 5, [wintypes.LPCWSTR, wintypes.DWORD], str(path), 0)

    def save(self, path):
        self._call(self.persist, 6, [wintypes.LPCWSTR, wintypes.BOOL], str(path), True)

    def text(self, index):
        buffer = ctypes.create_unicode_buffer(32768)
        self._call(self.link, index, [wintypes.LPWSTR, ctypes.c_int], buffer, len(buffer))
        return buffer.value

    def target(self):
        buffer = ctypes.create_unicode_buffer(32768)
        self._call(self.link, 3, [wintypes.LPWSTR, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD],
                   buffer, len(buffer), None, 4)  # SLGP_RAWPATH; do not resolve moved targets.
        return buffer.value

    def configure(self, spec, description):
        for index, value in ((20, spec.target), (11, spec.arguments),
                             (9, spec.working_directory), (7, description)):
            self._call(self.link, index, [wintypes.LPCWSTR], value)
        self._call(self.link, 17, [wintypes.LPCWSTR, ctypes.c_int], spec.target, 0)
        self._call(self.link, 15, [ctypes.c_int], 1)  # SW_SHOWNORMAL


def read_shortcut(path):
    with ShellLink() as link:
        link.load(Path(path))
        return {"target": link.target(), "arguments": link.text(10),
                "directory": link.text(8), "description": link.text(6)}
