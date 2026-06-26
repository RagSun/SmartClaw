#!/bin/bash
# 构建 SmartClaw microVM rootfs
# 包含 vsock 服务端和 Python 运行时

set -e

ROOTFS_DIR=${1:-"/opt/smartclaw/images/rootfs"}
ROOTFS_SIZE=${2:-256}  # MB
ALPINE_VER="3.19"

echo "=== 构建 SmartClaw rootfs ==="
echo "目录: $ROOTFS_DIR"
echo "大小: ${ROOTFS_SIZE}MB"

# 创建空文件系统
dd if=/dev/zero of=/tmp/smartclaw-rootfs.ext4 bs=1M count=$ROOTFS_SIZE
mkfs.ext4 /tmp/smartclaw-rootfs.ext4

# 挂载
mkdir -p /mnt/smartclaw-rootfs
mount -o loop /tmp/smartclaw-rootfs.ext4 /mnt/smartclaw-rootfs

# 下载并解压 Alpine rootfs
cd /tmp
curl -fsSL "https://dl-cdn.alpinelinux.org/alpine/v${ALPINE_VER}/releases/x86_64/alpine-minirootfs-${ALPINE_VER}.1-x86_64.tar.gz" -o alpine.tgz
tar xzf alpine.tgz -C /mnt/smartclaw-rootfs

# 安装 Python
chroot /mnt/smartclaw-rootfs /sbin/apk add --no-cache python3 py3-pip

# 创建 SmartClaw 目录
mkdir -p /mnt/smartclaw-rootfs/opt/smartclaw/lib/python
mkdir -p /mnt/smartclaw-rootfs/opt/smartclaw/tools
mkdir -p /mnt/smartclaw-rootfs/opt/smartclaw/data

# 复制 vsock 服务端
cp src/smartclaw/sandbox/vsock/*.py /mnt/smartclaw-rootfs/opt/smartclaw/lib/python/smartclaw/sandbox/vsock/
cp src/smartclaw/sandbox/vsock_agent.py /mnt/smartclaw-rootfs/opt/smartclaw/bin/smartclaw-vsock-agent
chmod +x /mnt/smartclaw-rootfs/opt/smartclaw/bin/smartclaw-vsock-agent

# 复制 console 模块（服务端依赖）
mkdir -p /mnt/smartclaw-rootfs/opt/smartclaw/lib/python/smartclaw
cp src/smartclaw/__init__.py /mnt/smartclaw-rootfs/opt/smartclaw/lib/python/smartclaw/
cp src/smartclaw/console.py /mnt/smartclaw-rootfs/opt/smartclaw/lib/python/smartclaw/

# 创建 init 脚本
cat > /mnt/smartclaw-rootfs/init << 'INIT'
#!/bin/sh
# SmartClaw microVM init

mount -t proc none /proc
mount -t sysfs none /sys
mount -t devtmpfs none /dev

echo "=== SmartClaw microVM ==="
echo "启动 vsock 服务端..."

# 启动 vsock 服务端
export PYTHONPATH=/opt/smartclaw/lib/python
python3 /opt/smartclaw/bin/smartclaw-vsock-agent --port 1234 &

echo "vsock 服务端已启动 (port=1234)"
echo "进入交互模式..."

exec /bin/sh
INIT

chmod +x /mnt/smartclaw-rootfs/init

# 卸载
sync
umount /mnt/smartclaw-rootfs

# 移动到目标位置
mv /tmp/smartclaw-rootfs.ext4 "$ROOTFS_DIR"

echo "=== rootfs 构建完成 ==="
echo "文件: $ROOTFS_DIR"
ls -lh "$ROOTFS_DIR"
