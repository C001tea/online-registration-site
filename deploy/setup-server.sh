#!/bin/bash
# Первая настройка нового сервера (Ubuntu 24.04, Oracle Cloud A1/ARM).
# Запускается из .github/workflows/setup-server.yml по SSH от имени ubuntu.
# Можно запускать повторно: уже сделанное пропускается.
set -euo pipefail

echo "== Система: $(lsb_release -ds), $(uname -m) =="

# 1. Обновления
export DEBIAN_FRONTEND=noninteractive
sudo apt-get update -q
sudo apt-get upgrade -y -q
sudo apt-get install -y -q ca-certificates curl git iptables-persistent

# 2. Docker из официального репозитория (есть сборка под ARM)
if ! command -v docker >/dev/null; then
    sudo install -m 0755 -d /etc/apt/keyrings
    sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
    sudo chmod a+r /etc/apt/keyrings/docker.asc
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
        | sudo tee /etc/apt/sources.list.d/docker.list >/dev/null
    sudo apt-get update -q
    sudo apt-get install -y -q docker-ce docker-ce-cli containerd.io docker-compose-plugin
fi
sudo systemctl enable --now docker
sudo usermod -aG docker "$USER"

# 3. Файрвол внутри машины. В образах Oracle INPUT по умолчанию пускает только
#    SSH, остальное отклоняет — открываем 80 и 443 перед правилом REJECT
for port in 80 443; do
    if ! sudo iptables -C INPUT -p tcp --dport "$port" -j ACCEPT 2>/dev/null; then
        sudo iptables -I INPUT 1 -p tcp --dport "$port" -j ACCEPT
    fi
done
sudo netfilter-persistent save

# 4. Каталог для бэкапов
mkdir -p "$HOME/backups"

echo "== Готово =="
sudo docker --version
sudo docker compose version
free -m | sed -n '1,2p'
df -h / | tail -1
sudo iptables -L INPUT -n --line-numbers | head -8
