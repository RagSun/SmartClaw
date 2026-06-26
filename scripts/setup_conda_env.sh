#!/bin/bash
# 创建 conda 环境并安装 SmartClaw

set -e

echo "=== SmartClaw Conda 环境管理 ==="
echo ""

# 检查 conda
if ! command -v conda &> /dev/null; then
    echo "❌ conda 未安装"
    echo ""
    echo "安装方法："
    echo "1. 下载 Miniconda:"
    echo "   wget https://repo.anaconda.com/miniconda/Miniconda3-latest-Linux-x86_64.sh"
    echo "2. 安装:"
    echo "   bash Miniconda3-latest-Linux-x86_64.sh"
    echo "3. 激活:"
    echo "   source ~/.bashrc"
    echo ""
    exit 1
fi

echo "✅ conda 已安装: $(conda --version)"
echo ""

# 创建 Python 3.12 环境
echo "=== 创建 Python 3.12 环境 ==="
conda create -n smartclaw python=3.12 uv -y
conda activate smartclaw
uv pip install -e .
uv pip install pytest pytest-asyncio pytest-cov ruff black mypy
echo "✅ Python 3.12 环境就绪"
echo ""

# 测试环境
echo "=== 测试 Python 3.12 ==="
python --version
pytest tests/ -v --tb=short -q
echo ""

echo "=== 完成 ==="
conda info --envs
