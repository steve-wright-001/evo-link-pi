#!/usr/bin/env bash
# Installs (or updates) Evo Link's Pi station as a service that starts with the Pi. On the Pi, one line does it:
#   curl -fsSL https://raw.githubusercontent.com/steve-wright-001/evo-link-pi/main/install.sh | sudo bash
# (or, with the files beside it, sudo ./install.sh). Run the same line again to update.
# Needs Raspberry Pi OS Bookworm or newer (it uses NetworkManager) and the Pi on your network by its Ethernet cable.
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "Run it with sudo: sudo ./pi/install.sh"; exit 1; }
RAW=https://raw.githubusercontent.com/steve-wright-001/evo-link-pi/main
here="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || echo /nonexistent)"
src="$here/evolink-pi.py"
if [ ! -f "$src" ]; then
  # Run through curl | bash: fetch the program from the public evo-link-pi repo.
  src="$(mktemp)"
  curl -fsSL "$RAW/evolink-pi.py" -o "$src" || { echo "Couldn't download the program. Is the Pi on the internet?"; exit 1; }
fi
command -v nmcli >/dev/null || { echo "NetworkManager (nmcli) not found: Evo Link needs Raspberry Pi OS Bookworm or newer."; exit 1; }
command -v iw >/dev/null || apt-get install -y iw
rfkill unblock wifi 2>/dev/null || true
nmcli radio wifi on || true
install -d -m 755 /opt/evolink-pi
install -m 755 "$src" /opt/evolink-pi/evolink-pi.py
install -d -m 700 /var/lib/evolink-pi
cat > /etc/systemd/system/evolink-pi.service <<'UNIT'
[Unit]
Description=Evo Link Pi station
After=NetworkManager.service network-online.target
Wants=network-online.target

[Service]
ExecStart=/usr/bin/python3 /opt/evolink-pi/evolink-pi.py serve
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable evolink-pi >/dev/null
systemctl restart evolink-pi
sleep 1
eth=$(ip -4 -o addr show scope global | awk '$2 !~ /^wlan/ {print $4}' | cut -d/ -f1 | head -1)
echo
if [ -z "$eth" ]; then echo "Warning: no wired network address found. Plug the Pi into your network by cable."; fi
echo "Evo Link Pi station is running${eth:+ at $eth}, port 8710."
echo "In the app: Settings → Pi station → Find, then Pair."
