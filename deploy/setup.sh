#!/bin/bash
# Amazon Scraper v4 - Server 部署脚本
# 用法: bash deploy/setup.sh

set -e

PROJECT_DIR="/opt/amazon-scraper-v4"
VENV_DIR="$PROJECT_DIR/.venv"

echo "=== Amazon Scraper v4 Server Setup ==="

# 1. 创建项目目录
sudo mkdir -p "$PROJECT_DIR"
sudo chown "$USER:$USER" "$PROJECT_DIR"

# 2. 复制文件
echo "复制项目文件..."
rsync -av --exclude='.git' --exclude='.venv' --exclude='__pycache__' \
    --exclude='data/*.db' --exclude='worker/' \
    . "$PROJECT_DIR/"

# 3. 创建虚拟环境
echo "创建 Python 虚拟环境..."
python3 -m venv "$VENV_DIR"
"$VENV_DIR/bin/pip" install --upgrade pip
"$VENV_DIR/bin/pip" install -r "$PROJECT_DIR/requirements.txt"

# 4. 创建数据目录
mkdir -p "$PROJECT_DIR/data"
mkdir -p "$PROJECT_DIR/server/static/screenshots"

# 5. 配置 .env
if [ ! -f "$PROJECT_DIR/.env" ]; then
    cp "$PROJECT_DIR/.env.example" "$PROJECT_DIR/.env"
    echo "请编辑 $PROJECT_DIR/.env 填写代理凭证"
fi

# 6. 安装 systemd 服务
echo "安装 systemd 服务..."
sudo cp "$PROJECT_DIR/deploy/server.service" /etc/systemd/system/amazon-scraper.service
sudo systemctl daemon-reload
sudo systemctl enable amazon-scraper

echo ""
echo "=== 部署完成 ==="
echo "启动: sudo systemctl start amazon-scraper"
echo "状态: sudo systemctl status amazon-scraper"
echo "日志: journalctl -u amazon-scraper -f"
echo "端口: 8899"
