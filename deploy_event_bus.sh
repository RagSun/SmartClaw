#!/bin/bash

# SmartClaw Event Bus + Subagent 部署脚本

set -e

echo "🚀 开始部署 SmartClaw Event Bus + Subagent 架构..."

# 1. 检查核心文件
echo "✅ 检查核心文件..."
FILES=(
    "src/smartclaw/core/__init__.py"
    "src/smartclaw/core/event_bus.py"
    "src/smartclaw/core/subagent_registry.py"
    "src/smartclaw/core/subagent_spawn.py"
)

for file in "${FILES[@]}"; do
    if [ -f "$file" ]; then
        echo "  ✓ $file"
    else
        echo "  ✗ $file 缺失！"
        exit 1
    fi
done

# 2. 创建测试文件
echo "✅ 检查测试文件..."
TEST_FILES=(
    "tests/core/__init__.py"
    "tests/core/test_event_bus.py"
    "tests/core/test_subagent_registry.py"
)

for file in "${TEST_FILES[@]}"; do
    if [ -f "$file" ]; then
        echo "  ✓ $file"
    else
        echo "  ✗ $file 缺失！"
        exit 1
    fi
done

# 3. 运行单元测试
echo ""
echo "🧪 运行单元测试..."
python -m pytest tests/core/ -v

# 4. 检查依赖
echo ""
echo "📦 检查依赖..."
uv pip install aiofiles pydantic pytest pytest-asyncio

echo ""
echo "✅ 部署完成！"
echo ""
echo "📖 快速开始："
echo "  1. 运行示例: python -m smartclaw.core.integration_example"
echo "  2. 查看测试: pytest tests/core/ -v"
echo ""
