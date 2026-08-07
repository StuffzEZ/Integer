# Integer

Integer is a self-hosted multiple-choice examination platform. The browser is only the candidate surface; answer keys, single-use IDs, scoring, and attempt invalidation live in the Python service.

## Linux install

```bash
chmod +x integer-backend.sh
./integer-backend.sh install
```

The installer creates a virtual environment, installs `aiohttp`, writes a systemd service, and prints the local URL and generated admin password. Set `INTEGER_ADMIN_PASSWORD` before starting for a known password. Use `./integer-backend.sh start`, `status`, `tunnel`, or `uninstall` for lifecycle commands.

Open `/backend.html` to create exams. Each question supports two to six answers, A-F answer keys, alternate variations, and letter blocking. Open `/exam.html?server=https://your-host&exam=ID` for candidates. A candidate's user ID is consumed when an attempt starts; an administrator can delete it from the control room.

## Cloudflare

`./integer-backend.sh tunnel` starts a temporary `trycloudflare.com` tunnel if `cloudflared` is installed. For a named tunnel, authenticate `cloudflared` yourself and set `INTEGER_TUNNEL_NAME`; the script runs that tunnel by name. Put the service behind HTTPS before sharing it.
