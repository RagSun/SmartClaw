"""
跨平台子进程文本输出解码。

在 Windows 上，``subprocess.run(..., text=True)`` 未指定 ``encoding`` 时会使用系统
ANSI 代码页（中文环境常为 GBK）。子进程若向管道写入 UTF-8 字节，可能在
``subprocess`` 内部读管道线程中触发 ``UnicodeDecodeError``，且该异常不在
``run()`` 的 try/except 覆盖范围内。

统一使用 UTF-8 + replace 在各系统上保持稳定；乱码字节被替换而非崩溃。
"""

from __future__ import annotations

import subprocess
from typing import Any

# 与 ``text=True`` 搭配：subprocess.run(..., text=True, **SUBPROCESS_TEXT_KWARGS)
SUBPROCESS_TEXT_KWARGS: dict[str, str] = {
    "encoding": "utf-8",
    "errors": "replace",
}


def run_text(*popenargs: Any, **kwargs: Any) -> Any:
    """``subprocess.run``，在文本模式下强制 UTF-8 解码（不覆盖调用方已传的 encoding/errors）。"""
    kw = dict(kwargs)
    kw.setdefault("text", True)
    if kw.get("text"):
        for key, val in SUBPROCESS_TEXT_KWARGS.items():
            kw.setdefault(key, val)
    return subprocess.run(*popenargs, **kw)


__all__ = ["SUBPROCESS_TEXT_KWARGS", "run_text"]
