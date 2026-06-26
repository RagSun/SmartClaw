"""教学/验证用 CLI 启动器：等价于 `smartclaw` 命令，但强制使用本仓库 src 源码。

用法（PowerShell）：
    $env:PYTHONPATH="src"; python scripts/fc.py <smartclaw 子命令...>
因为本仓库未安装为 console script（环境里的 `smartclaw` 可能指向别的副本），
用本启动器可确保跑的是当前项目代码。
"""

from smartclaw.cli import app

if __name__ == "__main__":
    app()
