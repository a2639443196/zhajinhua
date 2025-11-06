#!/bin/bash
set -e # 确保任何命令失败时，脚本都会立即退出

echo "=== LLM 炸金花 - 上帝模式启动脚本 ==="

# --- 1. 定义并检查 Python 3.10 路径 ---
# (参考了你提供的 run_backend.sh)
PYTHON_BIN="/usr/local/bin/python3.10" 

if [[ ! -x "$PYTHON_BIN" ]]; then
    echo "!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!"
    echo "!! [错误] 未找到 Python 3.10"
    echo "!! 脚本期望在 $PYTHON_BIN 找到它。" 
    echo "!! 请确认 Python 3.10 已安装在此路径。"
    echo "!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!"
    exit 1
fi
echo "[OK] 检查 Python 3.10... (已找到)"

# --- 2. 创建虚拟环境 (使用 Python 3.10) ---
if [ ! -d "venv" ]; then
    echo "-> 正在使用 $PYTHON_BIN 创建虚拟环境 'venv'..."
    $PYTHON_BIN -m venv venv
fi

# --- 3. 激活虚拟环境 ---
echo "-> 正在激活虚拟环境..."
source venv/bin/activate
# (激活后，'pip' 和 'python3' 命令将自动指向 venv 内部的 3.10 版本)

# --- 4. 升级 venv 内部的 PIP ---
echo "-> 正在升级 venv 内部的 pip..."
# (我们使用 python -m pip 来确保使用的是 venv 内部的 pip)
python -m pip install --upgrade pip

# --- 5. 安装/更新依赖 ---
echo "-> 正在根据 requirements.txt 安装依赖..."
pip install -r requirements.txt

# --- 6. 启动服务器 ---
echo ""
echo "================================================="
echo "  依赖安装完毕。正在启动 FastAPI 服务器..."
echo "  (Python 版本: $(python --version))"
echo "  服务将运行在: http://0.0.0.0:8000"
echo ""
echo "  在浏览器中打开 http://<你的服务器IP>:8000 即可观战"
echo "  (按 Ctrl+C 停止服务器)"
echo "================================================="
echo ""
python server.py