#!/bin/bash
# 更新现有 rootfs，添加 vsock 服务端

set -e

ROOTFS="/opt/smartclaw/images/rootfs.ext4"
MOUNT_DIR="/mnt/smartclaw-rootfs"

echo "=== 更新 rootfs ==="

# 挂载 rootfs
mkdir -p "$MOUNT_DIR"
mount -o loop "$ROOTFS" "$MOUNT_DIR"

# 创建目录
mkdir -p "$MOUNT_DIR/opt/smartclaw/lib/python/smartclaw/sandbox/vsock"
mkdir -p "$MOUNT_DIR/opt/smartclaw/bin"

# 复制文件
cp src/smartclaw/__init__.py "$MOUNT_DIR/opt/smartclaw/lib/python/smartclaw/"
cp src/smartclaw/console.py "$MOUNT_DIR/opt/smartclaw/lib/python/smartclaw/"
cp src/smartclaw/sandbox/vsock/*.py "$MOUNT_DIR/opt/smartclaw/lib/python/smartclaw/sandbox/vsock/"

# 创建启动脚本
cat > "$MOUNT_DIR/opt/smartclaw/bin/vsock-server" << 'SCRIPT'
#!/usr/bin/env python3
import sys
sys.path.insert(0, "/opt/smartclaw/lib/python")
from smartclaw.sandbox.vsock.server import main
main()
SCRIPT
chmod +x "$MOUNT_DIR/opt/smartclaw/bin/vsock-server"

# 更新 init
cat > "$MOUNT_DIR/init" << 'INIT'
#!/bin/sh
mount -t proc none /proc
mount -t sysfs none /sys
mount -t devtmpfs none /dev

echo "=== SmartClaw microVM ==="
export PYTHONPATH=/opt/smartclaw/lib/python
/opt/smartclaw/bin/vsock-server --port 1234 &
echo "vsock 服务端已启动"
exec /bin/sh
INIT
chmod +x "$MOUNT_DIR/init"

# 卸载
sync
umount "$MOUNT_DIR"

echo "=== rootfs 更新完成 ==="
