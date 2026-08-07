#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$ROOT/.integer-venv"
SERVICE="integer-backend"
PORT="${INTEGER_PORT:-8765}"

install_backend() {
	command -v python3 >/dev/null || { echo "python3 is required" >&2; exit 1; }
	python3 -m venv "$VENV"
	# Ensure pip is available (handles minimal LXC images)
	"$VENV/bin/python" -m ensurepip --upgrade 2>/dev/null || true
	"$VENV/bin/pip" install --upgrade pip aiohttp
	if [[ -z "${INTEGER_ADMIN_PASSWORD:-}" ]]; then
		export INTEGER_ADMIN_PASSWORD="$(python3 -c 'import secrets; print(secrets.token_urlsafe(18))')"
	fi
	sudo tee "/etc/systemd/system/$SERVICE.service" >/dev/null <<EOF
[Unit]
Description=Integer examination backend
After=network.target
[Service]
WorkingDirectory=$ROOT
ExecStart=$VENV/bin/python $ROOT/server.py
Environment=INTEGER_PORT=$PORT
Environment=INTEGER_ADMIN_PASSWORD=$INTEGER_ADMIN_PASSWORD
Restart=on-failure
[Install]
WantedBy=multi-user.target
EOF
	sudo systemctl daemon-reload
	sudo systemctl enable --now "$SERVICE"
	echo "Integer is running at http://127.0.0.1:$PORT"
	echo "Admin password: $INTEGER_ADMIN_PASSWORD"
}

case "${1:-install}" in
	install) install_backend ;;
	start) sudo systemctl start "$SERVICE" ;;
	stop) sudo systemctl stop "$SERVICE" ;;
	restart) sudo systemctl restart "$SERVICE" ;;
	status) sudo systemctl status "$SERVICE" --no-pager ;;
	tunnel)
		command -v cloudflared >/dev/null || { echo "Install cloudflared first: https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/"; exit 1; }
		if [[ -n "${INTEGER_TUNNEL_NAME:-}" ]]; then cloudflared tunnel run "$INTEGER_TUNNEL_NAME"; else cloudflared tunnel --url "http://127.0.0.1:$PORT"; fi
		;;
	uninstall)
		sudo systemctl disable --now "$SERVICE" 2>/dev/null || true
		sudo rm -f "/etc/systemd/system/$SERVICE.service"
		sudo systemctl daemon-reload
		rm -rf "$VENV"
		echo "Integer backend service and virtual environment removed. Data remains in $ROOT/integer-data.json."
		;;
	*) echo "Usage: $0 {install|start|stop|restart|status|tunnel|uninstall}"; exit 2 ;;
esac
