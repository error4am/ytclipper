"""A system-wide clip hotkey (Windows RegisterHotKey).

No extra packages and no admin rights: the hotkey works even when a game or
another app is focused.
"""
from __future__ import annotations

import ctypes
import os
import threading
from ctypes import wintypes

from .util import LogBus

VK = {
    "F1": 0x70, "F2": 0x71, "F3": 0x72, "F4": 0x73, "F5": 0x74, "F6": 0x75,
    "F7": 0x76, "F8": 0x77, "F9": 0x78, "F10": 0x79, "F11": 0x7A, "F12": 0x7B,
    "INSERT": 0x2D, "HOME": 0x24, "END": 0x23, "PGUP": 0x21, "PGDN": 0x22,
}
MODS = {"alt": 0x0001, "ctrl": 0x0002, "control": 0x0002, "shift": 0x0004, "win": 0x0008}
MOD_NOREPEAT = 0x4000
WM_HOTKEY = 0x0312
WM_QUIT = 0x0012


def parse(spec: str) -> tuple[int, int]:
    parts = [p.strip().lower() for p in (spec or "").split("+") if p.strip()]
    if not parts:
        raise ValueError("empty hotkey")
    mods = 0
    key = None
    for part in parts:
        if part in MODS:
            mods |= MODS[part]
        else:
            key = part
    if key is None:
        raise ValueError(f"no key in hotkey '{spec}'")
    upper = key.upper()
    if upper in VK:
        vk = VK[upper]
    elif len(upper) == 1 and upper.isalnum():
        vk = ord(upper)
    else:
        raise ValueError(f"unsupported key '{key}'")
    return mods, vk


class GlobalHotkey(threading.Thread):
    def __init__(self, spec: str, callback, bus: LogBus) -> None:
        super().__init__(name="hotkey", daemon=True)
        self.spec = (spec or "").strip()
        self.callback = callback
        self.bus = bus
        self.registered = False
        self.error = ""
        self._stop_event = threading.Event()
        self._thread_id: int | None = None
        self._user32 = None

    def run(self) -> None:
        if os.name != "nt":
            self.error = "global hotkey is Windows-only"
            self.bus.warn(self.error)
            return
        try:
            mods, vk = parse(self.spec)
        except ValueError as exc:
            self.error = str(exc)
            self.bus.warn(f"hotkey disabled: {exc}")
            return

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        self._user32 = user32
        self._thread_id = ctypes.windll.kernel32.GetCurrentThreadId()
        if not user32.RegisterHotKey(None, 1, mods | MOD_NOREPEAT, vk):
            self.error = "already in use"
            self.bus.warn(
                f"hotkey {self.spec.upper()} is already taken by another app "
                "- use --hotkey to pick a different one"
            )
            return

        self.registered = True
        self.bus.info(f"global hotkey: {self.spec.upper()} clips from any window")

        message = wintypes.MSG()
        while not self._stop_event.is_set():
            result = user32.GetMessageW(ctypes.byref(message), None, 0, 0)
            if result == 0 or result == -1:
                break
            if message.message == WM_HOTKEY and message.wParam == 1:
                try:
                    self.callback()
                except Exception as exc:  # never kill the hotkey thread
                    self.bus.warn(f"hotkey action failed: {exc}")

        user32.UnregisterHotKey(None, 1)
        self.registered = False

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread_id and self._user32 is not None:
            try:
                ctypes.windll.user32.PostThreadMessageW(
                    wintypes.DWORD(self._thread_id), WM_QUIT, 0, 0
                )
            except Exception:
                pass
