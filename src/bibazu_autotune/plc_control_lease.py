from __future__ import annotations

import ctypes
import os
from dataclasses import dataclass

PLC_CONTROL_LEASE_NAME = r"Local\BiBaZuPlcControl"
ERROR_ALREADY_EXISTS = 183


def _close_windows_handle(handle: int) -> None:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = (ctypes.c_void_p,)
    close_handle.restype = ctypes.c_bool
    close_handle(ctypes.c_void_p(handle))


@dataclass(slots=True)
class PlcControlLease:
    """Exclusive process lease for applications that can write to the BiBaZu PLC."""

    _handle: int | None

    @classmethod
    def acquire(cls, name: str = PLC_CONTROL_LEASE_NAME) -> PlcControlLease | None:
        if os.name != "nt":
            return cls(None)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        create_mutex = kernel32.CreateMutexW
        create_mutex.argtypes = (ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p)
        create_mutex.restype = ctypes.c_void_p
        ctypes.set_last_error(0)
        handle = create_mutex(None, False, name)
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
            _close_windows_handle(int(handle))
            return None
        return cls(int(handle))

    def close(self) -> None:
        if self._handle is None or os.name != "nt":
            return
        _close_windows_handle(self._handle)
        self._handle = None
