"""
跨平台判断本地进程 PID 是否仍在运行（用于 pid 文件防重复启动）。

Windows 上不可用 os.kill(pid, 0) 做探活，会触发 WinError 87。
"""

from __future__ import annotations

import os


def pid_is_running(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        return _pid_is_running_windows(pid)
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _pid_is_running_windows(pid: int) -> bool:
    import ctypes

    kernel32 = ctypes.windll.kernel32
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    ERROR_ACCESS_DENIED = 5

    # 必须先清零，否则 GetLastError 可能继承进程内其它调用的陈旧值，导致误判「仍在运行」
    kernel32.SetLastError(0)
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, pid)
    if handle:
        kernel32.CloseHandle(handle)
        return True
    err = kernel32.GetLastError()
    if err == ERROR_ACCESS_DENIED:
        return True
    return False


__all__ = ["pid_is_running"]
