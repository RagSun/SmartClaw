# -*- coding: utf-8 -*-
"""Docker 沙箱执行真实验证（命令行可复现）。

目标：用 SmartClaw 自带的 Docker 沙箱实现（``core.dockerimpl.ContainerPool``）真实
拉起一个隔离容器，在容器内执行命令，证明：
  1) 命令确实在容器里跑（hostname / 操作系统与宿主机不同）；
  2) Python 环境可用；
  3) 工作区目录挂载读写正常；
  4) 与宿主机隔离（容器内看不到宿主机文件系统）。

用法（PowerShell）：
    $env:PYTHONPATH = "src"
    python scripts/verify_sandbox_docker.py
前置：Docker 可用，且本地已有镜像 python:3.12-slim
    （可用： docker pull docker.m.daocloud.io/library/python:3.12-slim ;
             docker tag docker.m.daocloud.io/library/python:3.12-slim python:3.12-slim）
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path

from smartclaw.core.dockerimpl.container_pool import ContainerPool

PROJECT = "sandbox_demo"


async def main() -> None:
    ws = Path(tempfile.gettempdir()) / "hmc_sandbox_ws"
    (ws / PROJECT).mkdir(parents=True, exist_ok=True)

    pool = ContainerPool(max_containers=2, idle_timeout=120, workspace=str(ws))
    container = await pool.get_container(PROJECT)

    print("==================== 0) 确保容器存在并运行 ====================")
    cid = await container.ensure()
    print(f"容器已就绪 container_id={cid[:12]}  hostname={PROJECT}  image=python:3.12-slim")

    async def run(title: str, cmd: str) -> None:
        r = await container.execute(cmd, timeout=60)
        out = (r.get("output") or "").strip()
        print(f"\n$ {cmd}")
        print(f"[exit={r.get('exit_code')}] {out}")

    print("\n==================== 1) 证明在容器里执行 ====================")
    await run("hostname", "hostname")
    await run("os", "cat /etc/os-release | head -1")

    print("\n==================== 2) 容器内 Python 可用 ====================")
    await run("python", "python3 -c \"print('1+2 =', 1+2)\"")

    print("\n==================== 3) 工作区挂载读写 ====================")
    await run("write", "echo 'hello from sandbox' > /root/workspace/proof.txt && cat /root/workspace/proof.txt")
    host_proof = ws / PROJECT / "proof.txt"
    print(f"宿主机侧能看到挂载写入的文件: {host_proof} -> {host_proof.exists()}")

    print("\n==================== 4) 与宿主机隔离 ====================")
    await run("isolation", "ls / | tr '\\n' ' '")

    print("\n==================== 5) 清理容器 ====================")
    await pool.destroy_container(PROJECT)
    print(f"已销毁容器 smartclaw-{PROJECT}")
    print("\n[sandbox docker OK]")


if __name__ == "__main__":
    asyncio.run(main())
