#!/usr/bin/env python3
"""
BOINC Dashboard - backend Flask
Legge lo stato del client BOINC locale (via boinccmd) e lo espone come API JSON
+ serve la pagina dashboard con grafici storici da SQLite.
"""

import subprocess
import re
import sqlite3
import os
import shutil
import time
import xml.etree.ElementTree as ET
from datetime import datetime
from flask import Flask, jsonify, render_template, request

APP_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(APP_DIR, "boinc_history.db")
BOINCCMD = "boinccmd"  # deve essere nel PATH sull'LXC (installato con boinc-client)
BOINC_SERVICE = "boinc-client"  # nome del servizio systemd, usato per leggere i log
CLIENT_STATE_XML = "/var/lib/boinc-client/client_state.xml"  # stato interno di BOINC

app = Flask(__name__)


def run_boinccmd(args):
    """Esegue boinccmd con gli argomenti dati e ritorna l'output testuale."""
    try:
        result = subprocess.run(
            [BOINCCMD] + args, capture_output=True, text=True, timeout=15
        )
        return result.stdout
    except Exception as e:
        return f"ERROR: {e}"


def parse_get_tasks(output):
    """Parsa l'output di `boinccmd --get_tasks` in una lista di dict."""
    tasks = []
    current = {}
    for line in output.splitlines():
        line = line.strip()
        if line.startswith(") -----------") or re.match(r"^\d+\)\s*-+$", line):
            if current:
                tasks.append(current)
            current = {}
            continue
        if ":" in line:
            key, _, value = line.partition(":")
            current[key.strip()] = value.strip()
    if current:
        tasks.append(current)
    return tasks


def parse_get_project_status(output):
    """Parsa l'output di `boinccmd --get_project_status` in una lista di dict."""
    projects = []
    current = {}
    for line in output.splitlines():
        line = line.strip()
        if re.match(r"^\d+\)\s*-+$", line):
            if current:
                projects.append(current)
            current = {}
            continue
        if ":" in line:
            key, _, value = line.partition(":")
            current[key.strip()] = value.strip()
    if current:
        projects.append(current)
    return projects


def parse_get_old_tasks(output):
    """Parsa l'output di `boinccmd --get_old_tasks` in una lista di dict."""
    tasks = []
    current = {}
    for line in output.splitlines():
        raw = line.rstrip("\n")
        stripped = raw.strip()
        if stripped.startswith("task "):
            if current:
                tasks.append(current)
            current = {"name": stripped[len("task "):].rstrip(":")}
            continue
        if ":" in stripped:
            key, _, value = stripped.partition(":")
            current[key.strip()] = value.strip()
    if current:
        tasks.append(current)
    return tasks


def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS completed_tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_name TEXT UNIQUE,
            project_url TEXT,
            app_name TEXT,
            elapsed_seconds REAL,
            exit_status INTEGER,
            completed_time TEXT,
            completed_time_iso TEXT,
            logged_at TEXT
        )
        """
    )
    try:
        conn.execute("ALTER TABLE completed_tasks ADD COLUMN completed_time_iso TEXT")
    except sqlite3.OperationalError:
        pass  # colonna già presente
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS credit_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_name TEXT,
            project_url TEXT,
            total_credit REAL,
            snapshot_at TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS transfer_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            file_name TEXT,
            task_key TEXT,
            project_name TEXT,
            direction TEXT,
            bytes INTEGER,
            transferred_at TEXT,
            UNIQUE(file_name, direction, transferred_at)
        )
        """
    )
    # Migrazione: se il DB esiste già da prima senza task_key/is_task, aggiungile
    for col_def in ("task_key TEXT", "is_task INTEGER"):
        try:
            conn.execute(f"ALTER TABLE transfer_log ADD COLUMN {col_def}")
        except sqlite3.OperationalError:
            pass  # colonna già presente
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS sync_state (
            key TEXT PRIMARY KEY,
            value TEXT
        )
        """
    )
    conn.commit()
    conn.close()


MONTHS_IT = {
    "Jan": "01", "Feb": "02", "Mar": "03", "Apr": "04",
    "May": "05", "Jun": "06", "Jul": "07", "Aug": "08",
    "Sep": "09", "Oct": "10", "Nov": "11", "Dec": "12",
}

TRANSFER_LINE_RE = re.compile(
    r"^(\d{2})-(\w{3})-(\d{4}) (\d{2}:\d{2}:\d{2}) \[([^\]]+)\] "
    r"Finished (download|upload) of (\S+) \((\d+) bytes\)"
)

# Un file di upload si chiama "<nometask>_r<id>_<n>" -> il nome del task è la parte prima di "_r<digits>_<digits>"
UPLOAD_TASKNAME_RE = re.compile(r"^(.+)_r\d+_\d+$")
# Un file di download si chiama "<nomeWU>_<nomeWU>.<ext>" -> il nome ripetuto due volte è la WU
DOWNLOAD_TASKNAME_RE = re.compile(r"^(.+)_\1(?:\.\w+)?$")
# Rimuove il numero di istanza finale (es. "..._1" di un resend, tipicamente 1-2 cifre)
# per raggruppare download+upload dello stesso lavoro. Non tocca ID numerici lunghi
# che fanno parte del nome vero della work unit (es. "..._4866").
INSTANCE_SUFFIX_RE = re.compile(r"^(.+)_(\d{1,2})$")

ELAPSED_TIME_RE = re.compile(r"[\d.]+")
BOINC_DATETIME_RE = re.compile(r"^(\d{2})-(\w{3})-(\d{4}) (\d{2}:\d{2}:\d{2})$")


def parse_elapsed_seconds(raw):
    """`boinccmd --get_old_tasks` restituisce 'elapsed time: 6315.202072 sec'
    (con l'unità in coda) - qui estraiamo solo il numero."""
    if not raw:
        return 0.0
    m = ELAPSED_TIME_RE.search(str(raw))
    return float(m.group(0)) if m else 0.0


def parse_boinc_datetime(raw):
    """Converte una data nel formato di boinccmd ('08-Aug-2026 20:07:26') in ISO,
    per poter filtrare/raggruppare correttamente per mese/anno reale di completamento
    (non la data in cui la dashboard ha sincronizzato)."""
    if not raw:
        return None
    m = BOINC_DATETIME_RE.match(str(raw).strip())
    if not m:
        return None
    day, mon_str, year, time_str = m.groups()
    month = MONTHS_IT.get(mon_str, "01")
    return f"{year}-{month}-{day}T{time_str}"


def compute_task_key(file_name, direction):
    """Ricava una chiave unica di raggruppamento (a livello di work unit) da un nome file,
    così download e upload dello stesso lavoro finiscono nella stessa riga, invece che
    sparsi come file separati. Ritorna (key, is_task): is_task è False per i file
    applicativi/di supporto (scaricati una tantum, non specifici di un task)."""
    base = re.sub(r"\.\w+$", "", file_name)  # rimuove estensione tipo .txt
    if direction == "upload":
        m = UPLOAD_TASKNAME_RE.match(file_name)
        if not m:
            return base, False
        key = m.group(1)
    else:
        m = DOWNLOAD_TASKNAME_RE.match(file_name)
        if not m:
            return base, False
        key = m.group(1)

    # normalizza rimuovendo il numero di istanza finale, se presente
    m2 = INSTANCE_SUFFIX_RE.match(key)
    if m2:
        key = m2.group(1)
    return key, True


def parse_transfer_logs(raw_output):
    """Parsa le righe di log del client BOINC per estrarre i trasferimenti completati."""
    rows = []
    for line in raw_output.splitlines():
        m = TRANSFER_LINE_RE.search(line.strip())
        if not m:
            continue
        day, mon_str, year, time_str, project, direction, fname, nbytes = m.groups()
        month = MONTHS_IT.get(mon_str, "01")
        iso_ts = f"{year}-{month}-{day}T{time_str}"
        task_key, is_task = compute_task_key(fname, direction)
        rows.append({
            "file_name": fname,
            "task_key": task_key,
            "is_task": is_task,
            "project_name": project,
            "direction": direction,
            "bytes": int(nbytes),
            "transferred_at": iso_ts,
        })
    return rows


def get_boinc_logs_since(since_iso):
    """Legge i log del servizio boinc-client da systemd, da una data ISO in poi."""
    args = ["journalctl", "-u", BOINC_SERVICE, "--no-pager", "-o", "cat"]
    if since_iso:
        args += ["--since", since_iso]
    else:
        args += ["--since", "-90 days"]
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=30)
        return result.stdout
    except Exception:
        return ""


@app.route("/api/live/tasks")
def api_live_tasks():
    output = run_boinccmd(["--get_tasks"])
    return jsonify(parse_get_tasks(output))


@app.route("/api/live/projects")
def api_live_projects():
    output = run_boinccmd(["--get_project_status"])
    return jsonify(parse_get_project_status(output))


_app_names_cache = {"data": {}, "at": 0}


def parse_app_friendly_names_from_xml():
    """Legge client_state.xml (lo stato interno che BOINC stesso mantiene per
    ogni progetto collegato) ed estrae la mappa nome-breve (es. 'mcm1') ->
    nome descrittivo (es. 'Mapping Cancer Markers'). Generico per qualsiasi
    progetto presente o futuro: BOINC scrive qui i nomi automaticamente ogni
    volta che si collega a un nuovo progetto, senza bisogno di mappe statiche."""
    names = {}
    try:
        tree = ET.parse(CLIENT_STATE_XML)
        for app in tree.getroot().iter("app"):
            name_el = app.find("name")
            friendly_el = app.find("user_friendly_name")
            if name_el is not None and friendly_el is not None and name_el.text:
                names[name_el.text.strip()] = (friendly_el.text or "").strip()
    except Exception:
        pass  # file non leggibile o non ancora presente: fallback a nessun nome
    return names


@app.route("/api/live/app-names")
def api_live_app_names():
    """Nomi descrittivi delle applicazioni (es. mcm1 -> Mapping Cancer Markers).
    Cache di 60s: il file XML può essere pesante e questi nomi cambiano raramente."""
    now = time.time()
    if now - _app_names_cache["at"] > 60:
        _app_names_cache["data"] = parse_app_friendly_names_from_xml()
        _app_names_cache["at"] = now
    return jsonify(_app_names_cache["data"])


@app.route("/api/live/disk")
def api_live_disk():
    output = run_boinccmd(["--get_disk_usage"])
    lines = output.splitlines()
    data = {"total": None, "free": None, "projects": []}
    current = {}
    for line in lines:
        s = line.strip()
        if s.startswith("total:"):
            data["total"] = s.split(":", 1)[1].strip()
        elif s.startswith("free:"):
            data["free"] = s.split(":", 1)[1].strip()
        elif re.match(r"^\d+\)\s*-+$", s):
            if current:
                data["projects"].append(current)
            current = {}
        elif ":" in s:
            k, _, v = s.partition(":")
            current[k.strip()] = v.strip()
    if current:
        data["projects"].append(current)
    return jsonify(data)


@app.route("/api/history/sync", methods=["POST"])
def api_history_sync():
    """Legge i task completati e i crediti attuali e li salva nello storico (idempotente)."""
    init_db()
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    # Backfill: righe di trasferimento salvate prima dell'introduzione di task_key/is_task
    # vengono ricalcolate qui.
    backfilled = 0
    for row_id, fname, direction in cur.execute(
        "SELECT id, file_name, direction FROM transfer_log WHERE task_key IS NULL OR task_key = '' OR is_task IS NULL"
    ).fetchall():
        key, is_task = compute_task_key(fname, direction)
        cur.execute(
            "UPDATE transfer_log SET task_key = ?, is_task = ? WHERE id = ?",
            (key, 1 if is_task else 0, row_id),
        )
        backfilled += 1

    # Task completati
    old_output = run_boinccmd(["--get_old_tasks"])
    old_tasks = parse_get_old_tasks(old_output)
    inserted = 0
    for t in old_tasks:
        name = t.get("name") or t.get("task")
        if not name:
            continue
        try:
            completed_iso = parse_boinc_datetime(t.get("completed time", ""))
            cur.execute(
                """INSERT OR IGNORE INTO completed_tasks
                   (task_name, project_url, app_name, elapsed_seconds, exit_status, completed_time, completed_time_iso, logged_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    name,
                    t.get("project URL", ""),
                    t.get("app name", ""),
                    parse_elapsed_seconds(t.get("elapsed time")),
                    int(t.get("exit status", 0) or 0),
                    t.get("completed time", ""),
                    completed_iso,
                    datetime.now().isoformat(),
                ),
            )
            if cur.rowcount:
                inserted += 1
        except Exception:
            continue

    # Snapshot crediti (per grafico crescita nel tempo)
    proj_output = run_boinccmd(["--get_project_status"])
    projects = parse_get_project_status(proj_output)
    for p in projects:
        name = p.get("name", "")
        url = p.get("master URL", "")
        credit = p.get("user_total_credit", "0")
        try:
            credit_f = float(credit)
        except ValueError:
            credit_f = 0.0
        cur.execute(
            """INSERT INTO credit_snapshots (project_name, project_url, total_credit, snapshot_at)
               VALUES (?, ?, ?, ?)""",
            (name, url, credit_f, datetime.now().isoformat()),
        )

    # Trasferimenti (dati scaricati/caricati) dai log di systemd
    last_sync_row = cur.execute(
        "SELECT value FROM sync_state WHERE key = 'last_transfer_sync'"
    ).fetchone()
    since_iso = last_sync_row[0] if last_sync_row else None
    raw_logs = get_boinc_logs_since(since_iso)
    transfer_rows = parse_transfer_logs(raw_logs)
    inserted_transfers = 0
    for t in transfer_rows:
        try:
            cur.execute(
                """INSERT OR IGNORE INTO transfer_log
                   (file_name, task_key, is_task, project_name, direction, bytes, transferred_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (t["file_name"], t["task_key"], 1 if t["is_task"] else 0, t["project_name"], t["direction"], t["bytes"], t["transferred_at"]),
            )
            if cur.rowcount:
                inserted_transfers += 1
        except Exception:
            continue
    cur.execute(
        """INSERT INTO sync_state (key, value) VALUES ('last_transfer_sync', ?)
           ON CONFLICT(key) DO UPDATE SET value = excluded.value""",
        (datetime.now().strftime("%Y-%m-%d %H:%M:%S"),),
    )

    conn.commit()
    conn.close()
    return jsonify({
        "inserted_tasks": inserted,
        "projects_snapshotted": len(projects),
        "inserted_transfers": inserted_transfers,
        "backfilled_task_keys": backfilled,
    })


@app.route("/api/history/transfers")
def api_history_transfers():
    """Lista task (raggruppati per work unit, non per singolo file) con dati
    scaricati+caricati sommati insieme, filtro ?year=YYYY&month=MM.
    Esclude i file applicativi/di supporto (scaricati una tantum, non specifici
    di un task) - quelli sono disponibili separatamente su /api/history/support-files."""
    init_db()
    year = request.args.get("year")
    month = request.args.get("month")
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    query = """
        SELECT
               COALESCE(NULLIF(task_key, ''), file_name) AS task_name,
               project_name,
               SUM(CASE WHEN direction='download' THEN bytes ELSE 0 END) AS downloaded_bytes,
               SUM(CASE WHEN direction='upload' THEN bytes ELSE 0 END) AS uploaded_bytes,
               MIN(transferred_at) AS first_seen,
               MAX(transferred_at) AS last_seen
        FROM transfer_log
        WHERE is_task = 1
    """
    params = []
    if year:
        query += " AND strftime('%Y', transferred_at) = ?"
        params.append(str(year))
    if month:
        query += " AND strftime('%m', transferred_at) = ?"
        params.append(str(month).zfill(2))
    query += " GROUP BY task_name, project_name ORDER BY last_seen DESC"
    rows = cur.execute(query, params).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@app.route("/api/history/support-files")
def api_history_support_files():
    """Riepilogo dei file applicativi/di supporto (non task specifici) scaricati,
    filtro ?year=YYYY&month=MM. Sono binari/dati condivisi scaricati una tantum
    per ogni sotto-progetto (es. le varianti arp1/hst1/mcm1/mip1/opn1/scc1 di
    World Community Grid), non lavoro di calcolo assegnato a un singolo task."""
    init_db()
    year = request.args.get("year")
    month = request.args.get("month")
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    query = """
        SELECT COUNT(*) AS file_count,
               SUM(CASE WHEN direction='download' THEN bytes ELSE 0 END) AS downloaded_bytes,
               SUM(CASE WHEN direction='upload' THEN bytes ELSE 0 END) AS uploaded_bytes
        FROM transfer_log
        WHERE is_task = 0
    """
    params = []
    if year:
        query += " AND strftime('%Y', transferred_at) = ?"
        params.append(str(year))
    if month:
        query += " AND strftime('%m', transferred_at) = ?"
        params.append(str(month).zfill(2))
    row = cur.execute(query, params).fetchone()
    conn.close()
    return jsonify(dict(row) if row else {"file_count": 0, "downloaded_bytes": 0, "uploaded_bytes": 0})


@app.route("/api/system/info")
def api_system_info():
    """Info hardware dell'LXC: core CPU, RAM, storage, con utilizzo attuale."""
    # CPU: numero di core + carico attuale (% media su breve campionamento)
    cpu_count = os.cpu_count() or 1
    cpu_percent = _sample_cpu_percent()
    load1, load5, load15 = os.getloadavg()

    # RAM da /proc/meminfo
    mem_total_kb = mem_available_kb = 0
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    mem_total_kb = int(line.split()[1])
                elif line.startswith("MemAvailable:"):
                    mem_available_kb = int(line.split()[1])
    except Exception:
        pass
    mem_total_mb = mem_total_kb / 1024
    mem_available_mb = mem_available_kb / 1024
    mem_used_mb = mem_total_mb - mem_available_mb

    # Storage
    disk_total, disk_used, disk_free = shutil.disk_usage("/")

    return jsonify({
        "cpu": {
            "cores": cpu_count,
            "percent": cpu_percent,
            "load_avg": {"1m": round(load1, 2), "5m": round(load5, 2), "15m": round(load15, 2)},
        },
        "ram": {
            "total_mb": round(mem_total_mb, 1),
            "used_mb": round(mem_used_mb, 1),
            "available_mb": round(mem_available_mb, 1),
            "percent": round((mem_used_mb / mem_total_mb) * 100, 1) if mem_total_mb else 0,
        },
        "disk": {
            "total_gb": round(disk_total / (1024**3), 2),
            "used_gb": round(disk_used / (1024**3), 2),
            "free_gb": round(disk_free / (1024**3), 2),
            "percent": round((disk_used / disk_total) * 100, 1) if disk_total else 0,
        },
    })


def _sample_cpu_percent(interval=0.25):
    """Campiona /proc/stat due volte per calcolare l'uso CPU reale (non solo load avg)."""
    def read_stat():
        with open("/proc/stat") as f:
            parts = f.readline().split()[1:]
        vals = list(map(int, parts))
        idle = vals[3] + (vals[4] if len(vals) > 4 else 0)
        total = sum(vals)
        return idle, total

    try:
        idle1, total1 = read_stat()
        time.sleep(interval)
        idle2, total2 = read_stat()
        d_idle = idle2 - idle1
        d_total = total2 - total1
        if d_total <= 0:
            return 0.0
        return round((1 - d_idle / d_total) * 100, 1)
    except Exception:
        return 0.0


@app.route("/api/history/tasks")
def api_history_tasks():
    """Ritorna task completati, con filtro opzionale ?year=YYYY&month=MM
    (filtra sulla data reale di completamento, non su quando è stato sincronizzato)."""
    init_db()
    year = request.args.get("year")
    month = request.args.get("month")
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    date_col = "COALESCE(completed_time_iso, logged_at)"
    query = f"SELECT * FROM completed_tasks WHERE 1=1"
    params = []
    if year:
        query += f" AND strftime('%Y', {date_col}) = ?"
        params.append(str(year))
    if month:
        query += f" AND strftime('%m', {date_col}) = ?"
        params.append(str(month).zfill(2))
    query += f" ORDER BY {date_col} DESC"
    rows = cur.execute(query, params).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@app.route("/api/history/summary")
def api_history_summary():
    """Aggregati per mese: numero task completati, per grafico.
    Conta i task dal log dei trasferimenti (transfer_log), non da
    `boinccmd --get_old_tasks` - quest'ultimo riflette solo la memoria interna
    recente di BOINC, che viene svuotata nel tempo; il log dei trasferimenti
    invece resta permanente, quindi è la fonte affidabile per lo storico.
    Un task è "completato" quando ha un upload registrato (= risultato inviato)."""
    init_db()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    rows = cur.execute(
        """
        SELECT strftime('%Y-%m', transferred_at) AS period,
               COUNT(DISTINCT task_key) AS tasks_completed
        FROM transfer_log
        WHERE is_task = 1 AND direction = 'upload'
        GROUP BY period
        ORDER BY period ASC
        """
    ).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@app.route("/api/history/projects-breakdown")
def api_history_projects_breakdown():
    """Numero di task completati per progetto (per il grafico a ciambella).
    Stessa fonte permanente di /api/history/summary (transfer_log), non la
    memoria interna di BOINC che viene svuotata nel tempo."""
    init_db()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    rows = cur.execute(
        """
        SELECT project_name, COUNT(DISTINCT task_key) AS tasks_completed
        FROM transfer_log
        WHERE is_task = 1 AND direction = 'upload'
        GROUP BY project_name
        ORDER BY tasks_completed DESC
        """
    ).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@app.route("/api/history/credits")
def api_history_credits():
    """Serie storica dei crediti per progetto, per grafico di crescita."""
    init_db()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    rows = cur.execute(
        """
        SELECT project_name, total_credit, snapshot_at
        FROM credit_snapshots
        ORDER BY snapshot_at ASC
        """
    ).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@app.route("/")
def index():
    return render_template("index.html")


if __name__ == "__main__":
    init_db()
    # The dashboard has no authentication: bind to localhost unless told otherwise.
    app.run(
        host=os.environ.get("BOINC_DASH_HOST", "127.0.0.1"),
        port=int(os.environ.get("BOINC_DASH_PORT", "5000")),
        debug=False,
    )
