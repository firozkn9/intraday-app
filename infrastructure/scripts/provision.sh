#!/usr/bin/env bash
# One-time VM provisioning (Ubuntu 24.04 LTS). Run as root on a fresh VM:
#   curl -fsSL <repo>/infrastructure/scripts/provision.sh | bash    (or scp it over)
# Installs Docker, a firewall (22/80/443 only), unattended security upgrades, fail2ban,
# swap, and an unprivileged "intraday" user owning /opt/intraday.
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "run as root"; exit 1; }

apt-get update
apt-get install -y ca-certificates curl git ufw fail2ban unattended-upgrades rclone
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
. /etc/os-release
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu ${VERSION_CODENAME} stable" \
  > /etc/apt/sources.list.d/docker.list
apt-get update
apt-get install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin

ufw default deny incoming
ufw default allow outgoing
ufw allow OpenSSH
ufw allow 80/tcp
ufw allow 443/tcp
ufw allow 443/udp
ufw --force enable
dpkg-reconfigure -f noninteractive unattended-upgrades
systemctl enable --now fail2ban

if ! swapon --show | grep -q /swapfile; then
  fallocate -l 2G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile
  echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

id intraday >/dev/null 2>&1 || useradd --create-home --shell /bin/bash intraday
usermod -aG docker intraday
mkdir -p /opt/intraday /var/backups/intraday
chown intraday:intraday /opt/intraday /var/backups/intraday
chmod 700 /var/backups/intraday
timedatectl set-timezone UTC
echo "provisioned. Next: as 'intraday', clone the repo into /opt/intraday (docs/deploy.md)."
