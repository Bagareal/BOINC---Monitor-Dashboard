# BOINC Dashboard

A lightweight, self-hosted web dashboard for monitoring a BOINC client
running on a local machine (originally built for a Debian LXC container),
without needing to open BOINC Manager and reconnect every time.

<img width="2533" height="1266" alt="desktop" src="https://github.com/user-attachments/assets/87e1f819-1f03-4cc5-850c-97fdf56d3039" />

## What this is

This is a small Flask + SQLite web app that sits next to a running
`boinc-client` and shows, from any browser on the local network:

- **Live overview** — active tasks, completed tasks (all time), free disk
  space, total credit
- **LXC/host hardware** — CPU usage, RAM usage, storage usage, load average
  of the machine BOINC is running on
- **Active tasks** — name, status, progress %, estimated time remaining,
  and which project *and* which specific research application it belongs to
  (e.g. "World Community Grid · Mapping Cancer Markers", not just the raw
  internal app code)
- **Attached projects** — credit, disk usage, status, with a link to the
  project's site
- **Data transferred** — a permanent, filterable (by month/year) history of
  every task, showing bytes downloaded/uploaded, independent of BOINC's own
  internal task history (which BOINC prunes over time)

It was built because BOINC Manager is great for local use, but tedious for
checking on a headless machine over the network — this gives you an
always-on page you can just refresh.

## How it works / where the data comes from

No BOINC modification, no extra daemons beyond the dashboard itself. All
data is read locally on the same machine as `boinc-client`:

| Data | Source |
|---|---|
| Live tasks, projects, disk usage | `boinccmd --get_tasks` / `--get_project_status` / `--get_disk_usage` |
| Friendly application names (e.g. "Mapping Cancer Markers") | `client_state.xml` (BOINC's own internal state file — works for **any** project, current or future, since BOINC populates it automatically) |
| Permanent transfer history (bytes down/up per task) | `journalctl -u boinc-client` logs, parsed and stored in SQLite |
| Host hardware (CPU/RAM/disk) | `/proc/stat`, `/proc/meminfo`, `/proc/loadavg`, filesystem stats |

The reason transfer history and completed-task counts are read from systemd
logs into a local SQLite database, rather than trusted to BOINC's own
`--get_old_tasks`, is that BOINC only keeps a short-lived internal record of
completed tasks — it gets pruned over time. The dashboard's own database is
the durable copy.

## Requirements

- A Linux machine (tested on Debian) already running `boinc-client`
- Python 3 + `venv`
- `systemd` (for the optional auto-start service and log reading)

## Deploy

1. Copy the `boinc-dashboard` folder to the machine, e.g. `/opt/boinc-dashboard`
2. Install dependencies:
   ```bash
   cd /opt/boinc-dashboard
   python3 -m venv venv
   ./venv/bin/pip install -r requirements.txt
   ```
3. Run it once to check it works:
   ```bash
   ./venv/bin/python3 app.py
   ```
   Then open `http://localhost:5000`. The dashboard has no authentication, so it
listens on `127.0.0.1` by default. To reach it from another device on a trusted
LAN, set `BOINC_DASH_HOST=0.0.0.0` (and optionally `BOINC_DASH_PORT`), or keep
the default and use an SSH tunnel: `ssh -L 5000:localhost:5000 user@host`.

## Customization

By default the header just shows a generic "boinc-client" label under the
logo. If you want it to show your own machine's name (like "myhost ·
boinc-client"), edit this line in `templates/index.html`:
```html
<span id="host-label">boinc-client</span>
```
and replace it with whatever you'd like, e.g.:
```html
<span id="host-label">myhost · boinc-client</span>
```
It's a static label, not read from the system — this is intentional, so
nothing machine-specific ends up hardcoded in this public repo by default.

## Making it permanent

**Auto-start on boot** (systemd service):
```bash
cp boinc-dashboard.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now boinc-dashboard
```

**Automatic history sync** (cron, every hour — the dashboard also has a
manual "Sync now" button for on-demand updates):
```bash
crontab -e
# add:
0 * * * * curl -s -X POST http://localhost:5000/api/history/sync > /dev/null
```

**Keep logs around long enough to be captured** (systemd's journal is not
persistent by default, and gets cleared on reboot/rotation — this makes sure
transfer history isn't lost before the dashboard has a chance to read it):
```bash
mkdir -p /var/log/journal
sed -i 's/#Storage=auto/Storage=persistent/' /etc/systemd/journald.conf
systemctl restart systemd-journald
```

## Security — LAN use only

This dashboard has **no authentication**. Anyone who can reach the port
(default `5000`) on your network can view it. It is meant to be run on a
trusted local/home network only — **do not expose it directly to the
internet**.

If you want to restrict it to specific devices on your LAN, you can use
`ufw`:
```bash
apt install -y ufw
ufw allow from <your-pc-ip> to any port 5000
ufw allow from <your-phone-ip> to any port 5000
ufw enable
```

## About the BOINC name and logo

This is an independent, unofficial personal project. It is **not affiliated
with, endorsed by, or sponsored by UC Berkeley or the BOINC project**. The
BOINC name and logo, used here in the dashboard header/favicon for
identification purposes (loaded directly from `boinc.berkeley.edu`, not
redistributed), belong to their respective owners.

If anyone from the official BOINC project would prefer this project not use
their name/logo this way, please reach out — via an issue on
[github.com/BOINC/boinc](https://github.com/BOINC/boinc/issues) or the
[BOINC message boards](https://boinc.berkeley.edu) — and I will remove or
change it promptly.

## License

The code in this repository is licensed under the MIT License — see
[LICENSE](LICENSE). This does not cover the BOINC name or logo, see above.
