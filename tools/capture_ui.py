"""截取 Bili2Text Plus 主窗口截图，保存到 docs/screenshots/。"""

from __future__ import annotations

import ctypes
import os
import subprocess
import time
from ctypes import wintypes

PY = r"C:\Users\Lenovo\WorkBuddy\2026-10-27-21-45-29\bili2text\.venv\Scripts\python.exe"
ROOT = r"C:\Users\Lenovo\WorkBuddy\2026-10-27-21-45-29\bili2text-plus"
OUT_DIR = os.path.join(ROOT, "docs", "screenshots")
os.makedirs(OUT_DIR, exist_ok=True)
TARGET = os.path.join(OUT_DIR, "ui-main.png")
WINDOW_TITLE = "Bili2Text Plus — 多平台视频转文字助手"


def window_rect(hwnd):
    rect = wintypes.RECT()
    ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(rect))
    return rect.left, rect.top, rect.right, rect.bottom


def main() -> None:
    from PIL import ImageGrab

    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass

    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    proc = subprocess.Popen([PY, "window.py"], cwd=ROOT, env=env)
    try:
        time.sleep(6)
        hwnd = ctypes.windll.user32.FindWindowW(None, WINDOW_TITLE)
        if hwnd:
            HWND_TOPMOST = -1
            HWND_NOTOPMOST = -2
            SWP_FLAGS = 0x0001 | 0x0002  # SWP_NOSIZE | SWP_NOMOVE
            ctypes.windll.user32.ShowWindow(hwnd, 9)
            ctypes.windll.user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_FLAGS)
            ctypes.windll.user32.SetForegroundWindow(hwnd)
            time.sleep(2)
            box = window_rect(hwnd)
            topmost = True
        else:
            print("WINDOW NOT FOUND, capturing full screen")
            box = None
            topmost = False
        image = ImageGrab.grab()
        physical_width = image.size[0]
        logical_width = ctypes.windll.user32.GetSystemMetrics(0) or physical_width
        scale = physical_width / logical_width
        print(
            "DEBUG grab =",
            image.size,
            "logical_width =",
            logical_width,
            "scale =",
            scale,
            "raw_box =",
            window_rect(hwnd) if hwnd else None,
        )
        image = ImageGrab.grab(
            bbox=tuple(int(value * scale) for value in box) if box else None
        )
        if topmost and hwnd:
            ctypes.windll.user32.SetWindowPos(hwnd, HWND_NOTOPMOST, 0, 0, 0, 0, SWP_FLAGS)
        image.save(TARGET)
        print("SAVED =", TARGET, image.size)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    main()
