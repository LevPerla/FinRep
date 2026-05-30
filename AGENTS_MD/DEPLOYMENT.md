# FinRep Private Docker Deployment

This deployment is intended for a private server reachable only through SSH, VPN, or a trusted LAN.

## Files

- `Dockerfile` builds the Dash app image.
- `docker-compose.yml` runs the app and mounts private data from the server.
- `.env.example` documents deployment settings. Copy it to `.env` on the server.
- `.dockerignore` keeps `data/`, `reports/`, `.env`, and local caches out of the Docker build context.

## Server Setup

1. Install Docker and Docker Compose on the server.
2. Copy the repository to the server.
3. Copy `.env.example` to `.env`.
4. Set `FINREP_DATA_DIR` and `FINREP_REPORTS_DIR` to server directories that contain the real data and report output.
5. Set `FINREP_BIND_ADDRESS` to one of:
   - `127.0.0.1` for SSH tunnel access;
   - the server VPN IP;
   - a trusted LAN IP.

Do not use `0.0.0.0` for `FINREP_BIND_ADDRESS` unless the host firewall blocks public access to the port.

## Run

```bash
docker compose up -d --build
docker compose ps
```

Open Dash through the private address:

```text
http://<vpn-or-lan-ip>:8050
```

For SSH tunnel access with the default bind address:

```bash
ssh -L 8050:127.0.0.1:8050 user@server
```

Then open:

```text
http://127.0.0.1:8050
```

## Healthcheck

The app exposes:

```text
/healthz
```

Docker checks this endpoint from inside the container. A healthy response is JSON with `status=ok`.

## Data Safety

The image does not copy `data/` or `reports/`. Runtime state lives in mounted server directories:

```text
${FINREP_DATA_DIR}:/app/data
${FINREP_REPORTS_DIR}:/app/reports
```

Restarting or replacing the container preserves source CSVs, staging files, backups, cached rates, crypto caches, and generated reports as long as those host directories are preserved.
