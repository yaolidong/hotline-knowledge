#!/bin/sh
# 仅在用户明确授权安装 WSL 系统软件后执行。遵循 Docker 官方 Ubuntu 安装流程。
set -eu
[ "$(id -u)" = 0 ] || { echo '需要 WSL root 用户'; exit 1; }
. /etc/os-release
[ "$ID" = ubuntu ] || { echo '仅支持 Ubuntu'; exit 1; }
if command -v docker >/dev/null 2>&1; then
  docker version
  docker compose version
  exit 0
fi
for package in docker.io docker-compose docker-compose-v2 podman-docker containerd runc; do
  if dpkg-query -W -f='${Status}' "$package" 2>/dev/null | grep -q 'install ok installed'; then
    echo "检测到已有 $package；停止安装，保留现有软件。"
    exit 1
  fi
done
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends ca-certificates curl
install -m 0755 -d /etc/apt/keyrings
curl --connect-timeout 20 --max-time 120 -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
chmod a+r /etc/apt/keyrings/docker.asc
if [ -e /etc/apt/sources.list.d/docker.sources ]; then
  echo '已有 Docker 软件源配置，停止以免覆盖。'
  exit 1
fi
cat > /etc/apt/sources.list.d/docker.sources <<EOF
Types: deb
URIs: https://download.docker.com/linux/ubuntu
Suites: ${UBUNTU_CODENAME:-$VERSION_CODENAME}
Components: stable
Architectures: $(dpkg --print-architecture)
Signed-By: /etc/apt/keyrings/docker.asc
EOF
apt-get update
apt-get install -y --no-install-recommends docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
systemctl start docker
docker version
docker compose version
