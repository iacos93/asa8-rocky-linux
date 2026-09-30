#!/usr/bin/env python3
"""
asa8-panel — Pannello web stile Sybase Central per ASA 8 su Rocky 10.
Solo stdlib. Gestisce: lista DB, avvia/ferma engine, crea DB (dbinit),
crea servizi systemd, SQL batch (dbisqlc -q), log.
Ascolta su 0.0.0.0:8181
"""
import html
import hmac
import json
import glob
import os
import re
import secrets
import shutil
import subprocess
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

ASA = "/opt/sybase/SYBSsa8"
ASA_BIN = ASA + "/bin"
DB_DIRS = ["/srv/asa", ASA, "/dati", "/srv/asa-databases"]
IMPORT_DIR = "/srv/asa"
MAX_IMPORT = 4 * 1024 * 1024 * 1024  # 4 GB
PASSWORD_FILE = "/opt/asa8-panel/password.txt"
SESSION_TTL = 12 * 3600  # 12 ore
SESSIONS = {}  # token -> scadenza
FAILED = {}  # ip -> [tentativi, primo_tentativo]
PORT = 8181

def get_password():
    """Password di accesso (modificabile in PASSWORD_FILE, senza riavvio)."""
    for p in (PASSWORD_FILE,
              os.path.join(os.path.dirname(os.path.abspath(__file__)), "password.txt")):
        try:
            with open(p, "r", encoding="utf-8") as f:
                pw = f.read().strip()
                if pw:
                    return pw
        except OSError:
            pass
    return None

BASE_ENV = {
    "ASANY8": ASA,
    "ASANYSH8": "/opt/sybase/shared",
    "PATH": ASA_BIN + ":/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
    "LD_LIBRARY_PATH": ASA + "/lib:/usr/lib",
}

def env():
    e = dict(os.environ)
    e.update(BASE_ENV)
    return e

def run(cmd, timeout=60, license_yes=3):
    """Esegue comando ASA rispondendo Y alla licenza eval. Ritorna (rc, out)."""
    inp = ("Y\n" * license_yes).encode()
    try:
        p = subprocess.run(cmd, input=inp, capture_output=True, timeout=timeout, env=env())
        out = (p.stdout or b"").decode("utf-8", "replace") + (p.stderr or b"").decode("utf-8", "replace")
        return p.returncode, out[-8000:]
    except subprocess.TimeoutExpired as ex:
        o = ((ex.stdout or b"").decode("utf-8", "replace") if ex.stdout else "")
        o += ((ex.stderr or b"").decode("utf-8", "replace") if ex.stderr else "")
        return 124, "TIMEOUT dopo %ss\n%s" % (timeout, o[-4000:])
    except Exception as ex:
        return 1, "ERRORE: %s" % ex

def list_dbs(used=None):
    out = []
    seen = set()
    for d in DB_DIRS:
        for f in sorted(glob.glob(os.path.join(d, "*.db"))):
            if f in seen:
                continue
            seen.add(f)
            try:
                st = os.stat(f)
                out.append({"path": f, "size": st.st_size, "mtime": int(st.st_mtime),
                            "used": (f in used) if used is not None else False})
            except OSError:
                out.append({"path": f, "size": -1, "mtime": 0, "used": False})
    return out

def list_engines():
    """Parse ps aux per trovare dbsrv8/dbeng8 con -n engine e path db."""
    engs = []
    try:
        p = subprocess.run(["ps", "aux"], capture_output=True, timeout=10)
        txt = p.stdout.decode("utf-8", "replace")
        for line in txt.splitlines():
            if "dbsrv8" not in line and "dbeng8" not in line:
                continue
            if "grep" in line or "panel.py" in line:
                continue
            m_n = re.search(r"-n\s+(\S+)", line)
            m_db = re.search(r"(\/\S+\.db)", line)
            m_at = re.search(r"@(\/\S+)", line)
            m_log = re.search(r"-o\s+(\S+)", line)
            pid = line.split()[1] if len(line.split()) > 1 else "?"
            eng = m_n.group(1) if m_n else "?"
            if eng == "?" and m_at:
                # engine avviato via @file (stile DATABASE.txt): leggi -n dal file
                try:
                    with open(m_at.group(1), "r", errors="replace") as _f:
                        for _l in _f:
                            _m = re.search(r"^\s*-n\s*(\S+)", _l)
                            if _m:
                                eng = _m.group(1)
                                break
                except OSError:
                    pass
            engs.append({
                "pid": pid,
                "engine": eng,
                "db": m_db.group(1) if m_db else ("@" + m_at.group(1) if m_at else "?"),
                "log": m_log.group(1) if m_log else "",
                "raw": line.strip()[:300],
            })
    except Exception as ex:
        engs.append({"pid": "?", "engine": "err", "db": str(ex)[:100], "log": "", "raw": ""})
    return engs

def ping_engine(engine, user="dba", password="sql"):
    return run([ASA_BIN + "/dbping", "-c", f"uid={user};pwd={password};eng={engine}"], timeout=25, license_yes=1)

def stop_engine(engine, user="dba", password="sql"):
    return run([ASA_BIN + "/dbstop", "-c", f"uid={user};pwd={password};eng={engine}"], timeout=30, license_yes=1)

def start_engine(engine, db, port=None, logfile=None, extra=""):
    if not engine or not re.match(r"^[A-Za-z0-9_\-]+$", engine):
        return 1, "Nome engine non valido (solo lettere/numeri/_/-)"
    if not db or not os.path.isfile(db):
        return 1, f"File DB non trovato: {db}"
    logfile = logfile or f"/tmp/asa8-{engine}.log"
    cmd = [ASA_BIN + "/dbsrv8", "-ud", "-n", engine, "-o", logfile]
    if port:
        try:
            port = int(port)
            cmd += ["-x", f"tcpip(PORT={port})"]
        except ValueError:
            return 1, "Porta non valida"
    if extra:
        # whitelist semplice: split senza shell
        cmd += extra.strip().split()
    cmd += [db]
    return run(cmd, timeout=30, license_yes=1)

def create_db(path, page_size=None):
    if not path:
        return 1, "Percorso mancante"
    path = path.strip()
    if not path.endswith(".db"):
        path += ".db"
    if not os.path.isabs(path):
        path = "/srv/asa/" + path
    if os.path.exists(path):
        return 1, f"Esiste già: {path}"
    if ".." in path or not re.match(r"^[A-Za-z0-9_\-./]+$", path):
        return 1, "Percorso non valido"
    os.makedirs(os.path.dirname(path), exist_ok=True)
    cmd = [ASA_BIN + "/dbinit"]
    if page_size:
        cmd += ["-p", str(page_size)]
    cmd += [path]
    rc, out = run(cmd, timeout=120, license_yes=3)
    # dbinit ritorna 0 anche con warning libgcc; verifica file creato
    if os.path.exists(path):
        try:
            os.chmod(path, 0o644)
        except OSError:
            pass
        return 0, out + f"\n\nDB creato: {path}"
    return rc or 1, out

def run_sql(engine, sql, user="dba", password="sql"):
    sql = (sql or "").strip()
    if not sql:
        return 1, "SQL vuoto"
    if len(sql) > 20000:
        return 1, "SQL troppo lungo (max 20000 char)"
    with tempfile.NamedTemporaryFile("w", suffix=".sql", delete=False, dir="/tmp") as f:
        f.write(sql + "\n")
        qf = f.name
    try:
        rc, out = run([ASA_BIN + "/dbisqlc", "-q", "-c", f"uid={user};pwd={password};eng={engine}", qf],
                      timeout=60, license_yes=1)
        return rc, out
    finally:
        try:
            os.unlink(qf)
        except OSError:
            pass

def list_services():
    svcs = []
    try:
        p = subprocess.run(["systemctl", "list-units", "--type=service", "--all", "--no-pager", "--plain"],
                           capture_output=True, timeout=15)
        for line in p.stdout.decode("utf-8", "replace").splitlines():
            if "asa8-" in line or "asa8-panel" in line:
                parts = line.split()
                if parts:
                    svcs.append({"unit": parts[0], "load": parts[1] if len(parts) > 1 else "",
                                 "active": parts[2] if len(parts) > 2 else "",
                                 "sub": parts[3] if len(parts) > 3 else ""})
    except Exception:
        pass
    # anche unit files non attivi
    try:
        p = subprocess.run(["systemctl", "list-unit-files", "--no-pager", "--plain"],
                           capture_output=True, timeout=15)
        for line in p.stdout.decode("utf-8", "replace").splitlines():
            if "asa8-" in line and not any(s["unit"] in line for s in svcs):
                parts = line.split()
                svcs.append({"unit": parts[0], "load": "", "active": parts[1] if len(parts) > 1 else "", "sub": ""})
    except Exception:
        pass
    return sorted(svcs, key=lambda s: s["unit"])

def make_service(name, engine, db, port=None, autostart=True):
    if not re.match(r"^[A-Za-z0-9_\-]+$", name or ""):
        return 1, "Nome servizio non valido (solo lettere/numeri/_/-)"
    if not re.match(r"^[A-Za-z0-9_\-]+$", engine or ""):
        return 1, "Nome engine non valido"
    if not db or not os.path.isfile(db):
        return 1, f"DB non trovato: {db}"
    logfile = f"/var/log/asa8-{name}.log"
    portnum = ""
    if port:
        try:
            portnum = str(int(port))
        except ValueError:
            return 1, "Porta non valida"
    unit = f"""[Unit]
Description=ASA8 {engine} ({db} :{portnum or 'default'})
After=network.target

[Service]
Type=forking
ExecStart=/opt/asa8-panel/asa-start.sh {engine} {db} {portnum} {logfile}
ExecStop=/opt/asa8-panel/asa-stop.sh {engine}
Restart=on-failure
RestartSec=10
User=root

[Install]
WantedBy=multi-user.target
"""
    path = f"/etc/systemd/system/asa8-{name}.service"
    try:
        with open(path, "w") as f:
            f.write(unit)
        outs = []
        for c in [["systemctl", "daemon-reload"]]:
            p = subprocess.run(c, capture_output=True, timeout=20)
            outs.append(p.stdout.decode("utf-8", "replace") + p.stderr.decode("utf-8", "replace"))
        if autostart:
            p = subprocess.run(["systemctl", "enable", "--now", f"asa8-{name}.service"],
                               capture_output=True, timeout=60)
            outs.append(p.stdout.decode("utf-8", "replace") + p.stderr.decode("utf-8", "replace"))
            if p.returncode != 0:
                return p.returncode, "Unit scritta in %s ma enable/start fallito:\n%s" % (path, "\n".join(outs)[-3000:])
        return 0, "Servizio creato: %s\n%s" % (path, "\n".join(outs)[-2000:])
    except Exception as ex:
        return 1, f"ERRORE: {ex}"

def svc_action(unit, action):
    if not re.match(r"^[A-Za-z0-9_\-@:.]+$", unit or ""):
        return 1, "Unit non valida"
    if action not in ("start", "stop", "restart", "enable", "disable", "status"):
        return 1, "Azione non valida"
    try:
        p = subprocess.run(["systemctl", action, unit], capture_output=True, timeout=60)
        out = p.stdout.decode("utf-8", "replace") + p.stderr.decode("utf-8", "replace")
        return p.returncode, out[-4000:] or "(ok, nessun output)"
    except Exception as ex:
        return 1, str(ex)

PAGE = """<!DOCTYPE html><html lang="it"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>ASA8 Panel — Sybase Central web</title>
<style>
body{font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;background:#0f172a;color:#e2e8f0;margin:0}
header{background:#1e293b;padding:14px 20px;border-bottom:2px solid #38bdf8}
h1{margin:0;font-size:20px}h1 small{color:#38bdf8;font-weight:400}
.wrap{max-width:1100px;margin:0 auto;padding:18px}
.card{background:#1e293b;border-radius:10px;padding:14px 16px;margin-bottom:14px;border:1px solid #334155}
.card h2{margin:0 0 10px;font-size:16px;color:#7dd3fc}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{text-align:left;padding:6px 8px;border-bottom:1px solid #334155;vertical-align:top}
input,select,textarea{background:#0f172a;color:#e2e8f0;border:1px solid #475569;border-radius:6px;padding:7px 9px;width:100%;box-sizing:border-box}
textarea{font-family:ui-monospace,monospace;min-height:90px}
button{background:#0284c7;color:#fff;border:0;border-radius:6px;padding:8px 14px;cursor:pointer;margin:4px 4px 4px 0}
button:hover{background:#0369a1}button.danger{background:#b91c1c}button.danger:hover{background:#991b1b}
button.ghost{background:#334155}.row{display:grid;grid-template-columns:1fr 1fr;gap:10px}@media(max-width:700px){.row{grid-template-columns:1fr}}
pre{background:#0b1220;padding:10px;border-radius:6px;overflow:auto;max-height:320px;font-size:12px;white-space:pre-wrap}
.ok{color:#4ade80}.ko{color:#f87171}.muted{color:#94a3b8;font-size:12px}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:14px}@media(max-width:900px){.grid2{grid-template-columns:1fr}}
</style></head><body>
<header><h1>ASA8 Panel <small>— stile Sybase Central (web) · asa8-prod</small></h1>
<div class="muted">Avvia / ferma engine · crea database (dbinit) · crea servizi systemd · SQL · log</div></header>
<div class="wrap">
<div class="card"><h2>Stato server</h2><div id="stato" class="muted">caricamento…</div>
<div style="margin-top:8px"><button onclick="refresh()">🔄 Aggiorna</button>
<button class="ghost" onclick="pingDefault()">📡 Ping engine</button></div></div>
<div class="grid2">
<div class="card"><h2>▶ Avvia engine (dbsrv8)</h2>
<label>Engine (-n)<input id="s_eng" value="asademo"></label>
<label>Database (.db)<input id="s_db" value="/opt/sybase/SYBSsa8/asademo.db"></label>
<div class="row"><div><label>Porta TCP (vuoto=default 2638)<input id="s_port" placeholder="2638"></label></div>
<div><label>File log (-o)<input id="s_log" placeholder="/tmp/asa8-asademo.log"></label></div></div>
<label>Opzioni extra (facolt.)<input id="s_extra" placeholder="es. -c 64M"></label>
<button onclick="doStart()">Avvia</button><pre id="s_out"></pre></div>
<div class="card"><h2>⏹ Ferma engine (dbstop)</h2>
<label>Engine<input id="k_eng" value="asademo"></label>
<div class="row"><div><label>User<input id="k_user" value="dba"></label></div>
<div><label>Password<input id="k_pwd" value="sql" type="password"></label></div></div>
<button class="danger" onclick="doStop()">Ferma</button><pre id="k_out"></pre></div>
</div>
<div class="grid2">
<div class="card"><h2>➕ Crea nuovo database (dbinit)</h2>
<label>Percorso nuovo .db<input id="c_path" value="/srv/asa/nuovodb.db"></label>
<div class="muted">Crea in /srv/asa/ (es. /srv/asa/clienti.db). Poi avvialo da sopra o crea un servizio.</div>
<button onclick="doCreate()">Crea DB</button><pre id="c_out"></pre></div>
<div class="card"><h2>⚙ Crea servizio systemd (auto-avvio)</h2>
<label>Nome servizio (asa8-…)<input id="v_name" value="clienti"></label>
<label>Engine<input id="v_eng" value="clienti"></label>
<label>Database<input id="v_db" value="/srv/asa/clienti.db"></label>
<label>Porta (vuoto=default)<input id="v_port" placeholder="2638"></label>
<label style="display:block;margin-top:6px"><input type="checkbox" id="v_auto" checked style="width:auto"> abilita e avvia subito</label>
<button onclick="doService()">Crea servizio</button><pre id="v_out"></pre></div>
</div>
<div class="card"><h2>💬 SQL (dbisqlc batch)</h2>
<div class="row"><div><label>Engine<input id="q_eng" value="asademo"></label></div>
<div><label>User<input id="q_user" value="dba"></label></div></div>
<label>SQL<textarea id="q_sql">select * from systable;</textarea></label>
<button onclick="doSql()">Esegui</button><pre id="q_out"></pre></div>
<div class="card"><h2>🧾 Servizi systemd ASA</h2><div id="svc" class="muted">…</div>
<div style="margin-top:6px"><input id="svc_unit" placeholder="asa8-nome.service" style="max-width:260px">
<button class="ghost" onclick="svcGo('start')">start</button><button class="ghost" onclick="svcGo('stop')">stop</button><button class="ghost" onclick="svcGo('restart')">restart</button><button class="ghost" onclick="svcGo('status')">status</button></div>
<pre id="svc_out"></pre></div>
<div class="card"><h2>📄 Log engine</h2>
<div><input id="log_path" placeholder="/tmp/asa8-asademo.log" style="max-width:320px">
<button class="ghost" onclick="doLog()">Leggi ultime 100 righe</button></div><pre id="log_out"></pre></div>
</div>
<script>
async function j(url,body){const r=await fetch(url,{method:body? 'POST':'GET',headers:{'Content-Type':'application/json'},body:body?JSON.stringify(body):undefined});return r.json();}
async function refresh(){
 const o=await j('/api/overview');let h='';
 h+='<b>IP container:</b> '+(o.ip||'?')+' · <b>Data:</b> '+o.now+'<br><br>';
 h+='<b>Database trovati ('+o.dbs.length+')</b><table><tr><th>Path</th><th>Size</th></tr>';
 for(const d of o.dbs){h+='<tr><td><code>'+d.path+'</code></td><td>'+(d.size/1048576).toFixed(1)+' MB</td></tr>';}
 h+='</table><br><b>Engine in esecuzione ('+o.engines.length+')</b><table><tr><th>Engine</th><th>PID</th><th>DB</th><th>Ping</th></tr>';
 for(const e of o.engines){h+='<tr><td><b>'+e.engine+'</b></td><td>'+e.pid+'</td><td><code>'+e.db+'</code></td><td>'+(e.ping||'')+'</td></tr>';}
 document.getElementById('stato').innerHTML=h||'(vuoto)';
 let s='<table><tr><th>Unit</th><th>Active</th><th>Sub</th></tr>';
 for(const u of o.services){s+='<tr><td><code>'+u.unit+'</code></td><td>'+u.active+'</td><td>'+u.sub+'</td></tr>';}
 document.getElementById('svc').innerHTML=s;}
async function pingDefault(){const e=document.getElementById('s_eng').value;const o=await j('/api/ping',{engine:e});alert(o.output.slice(-500));}
async function doStart(){document.getElementById('s_out').textContent='avvio…';const o=await j('/api/start',{engine:v('s_eng'),db:v('s_db'),port:v('s_port'),log:v('s_log'),extra:v('s_extra')});document.getElementById('s_out').textContent=(o.ok?'OK\\n':'ERRORE\\n')+o.output;refresh();}
async function doStop(){document.getElementById('k_out').textContent='stop…';const o=await j('/api/stop',{engine:v('k_eng'),user:v('k_user'),password:document.getElementById('k_pwd').value});document.getElementById('k_out').textContent=(o.ok?'OK\\n':'ERRORE\\n')+o.output;refresh();}
async function doCreate(){document.getElementById('c_out').textContent='creazione…';const o=await j('/api/createdb',{path:v('c_path')});document.getElementById('c_out').textContent=(o.ok?'OK\\n':'ERRORE\\n')+o.output;refresh();}
async function doService(){document.getElementById('v_out').textContent='creazione servizio…';const o=await j('/api/mkservice',{name:v('v_name'),engine:v('v_eng'),db:v('v_db'),port:v('v_port'),autostart:document.getElementById('v_auto').checked});document.getElementById('v_out').textContent=(o.ok?'OK\\n':'ERRORE\\n')+o.output;refresh();}
async function doSql(){document.getElementById('q_out').textContent='esecuzione…';const o=await j('/api/sql',{engine:v('q_eng'),user:v('q_user'),sql:document.getElementById('q_sql').value});document.getElementById('q_out').textContent=(o.ok?'OK\\n':'ERRORE\\n')+o.output;}
async function svcGo(a){const u=document.getElementById('svc_unit').value||'';if(!u){alert('inserisci unit');return;}document.getElementById('svc_out').textContent=a+'…';const o=await j('/api/svc-action',{unit:u,action:a});document.getElementById('svc_out').textContent=(o.ok?'OK\\n':'ERRORE\\n')+o.output;refresh();}
async function doLog(){const p=document.getElementById('log_path').value;const o=await j('/api/logs?file='+encodeURIComponent(p));document.getElementById('log_out').textContent=(o.ok?'':'ERRORE\\n')+o.output;}
function v(id){return document.getElementById(id).value.trim();}
refresh();setInterval(refresh,15000);
</script></body></html>"""

# Se esiste index.html esterno (UI SaaS), usalo al posto di PAGE (fallback: PAGE integrata).
def _load_external_page():
    for _p in ("/opt/asa8-panel/index.html",
               os.path.join(os.path.dirname(os.path.abspath(__file__)), "index.html")):
        try:
            if os.path.isfile(_p):
                with open(_p, "r", encoding="utf-8") as _f:
                    _t = _f.read()
                if "<html" in _t.lower():
                    return _t
        except OSError:
            pass
    return None

try:
    _ext = _load_external_page()
    if _ext:
        PAGE = _ext
except Exception:
    pass

def dbs_used(engs):
    """DB con flag 'used': serviti da un engine o elencati in DATABASE.txt."""
    cfg = set()
    for e in (engs or []):
        if (e.get("db") or "").endswith(".db"):
            cfg.add(e["db"])
    try:
        with open(os.path.join(IMPORT_DIR, "DATABASE.txt"), "r", errors="replace") as f:
            for line in f:
                s = line.strip()
                if s.endswith(".db"):
                    cfg.add(s)
    except OSError:
        pass
    return list_dbs(used=cfg)

LOGIN_PAGE = """<!DOCTYPE html><html lang="it"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Accedi — ASA8 Console</title>
<style>
:root{--bg:#eef2f7;--surface:#fff;--ink:#0f1b2d;--muted:#5b6b82;--line:#dfe7f0;--brand:#0e7c7b;--brand-dark:#0a5f5e;--danger:#dc2626}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);min-height:100vh;display:grid;place-items:center;
  font:14px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Inter,Roboto,sans-serif;padding:16px}
.card{background:var(--surface);border:1px solid var(--line);border-radius:14px;padding:28px;width:100%;max-width:360px;
  box-shadow:0 1px 2px rgba(15,27,45,.06),0 8px 30px rgba(15,27,45,.10)}
.mark{width:42px;height:42px;border-radius:11px;background:linear-gradient(135deg,#0e7c7b,#14b8a6);
  display:grid;place-items:center;color:#fff;font-weight:800;font-size:18px;margin-bottom:12px}
h1{font-size:18px;margin:0 0 2px}.sub{color:var(--muted);font-size:12.5px;margin:0 0 18px}
label{display:block;font-size:12.5px;font-weight:600;margin-bottom:4px}
input{width:100%;border:1px solid #c6d2e0;border-radius:8px;padding:10px 12px;font-size:15px;min-height:44px;margin-bottom:12px}
input:focus{outline:2px solid var(--brand);outline-offset:1px;border-color:var(--brand)}
button{width:100%;background:var(--brand);color:#fff;border:0;border-radius:8px;padding:11px;font-size:14px;font-weight:700;cursor:pointer;min-height:44px}
button:hover{background:var(--brand-dark)}button:disabled{opacity:.55;cursor:wait}
.err{display:none;background:#fee2e2;color:#991b1b;border-radius:8px;padding:9px 12px;font-size:13px;margin-bottom:12px}
.err.on{display:block}
</style></head><body>
<div class="card">
  <div class="mark">A8</div>
  <h1>Accedi</h1>
  <p class="sub">ASA8 Console · asa8-prod</p>
  <div class="err" id="err" role="alert"></div>
  <form id="f">
    <label for="pw">Password</label>
    <input id="pw" type="password" autocomplete="current-password" autofocus>
    <button id="b">Entra</button>
  </form>
</div>
<script>
const f=document.getElementById("f"),pw=document.getElementById("pw"),err=document.getElementById("err"),b=document.getElementById("b");
f.addEventListener("submit",async e=>{
  e.preventDefault();b.disabled=true;err.classList.remove("on");
  try{
    const r=await fetch("/api/login",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({password:pw.value})});
    const o=await r.json();
    if(o.ok){location.href="/";}else{err.textContent=o.output||"Password errata";err.classList.add("on");pw.select();}
  }catch(ex){err.textContent="Server non raggiungibile";err.classList.add("on");}
  b.disabled=false;
});
</script></body></html>"""

def _throttled(ip):
    n, t0 = FAILED.get(ip, (0, 0.0))
    if time.time() - t0 > 600:
        FAILED.pop(ip, None)
        return False
    return n >= 10

class H(BaseHTTPRequestHandler):
    server_version = "ASA8-Panel/2.0"
    def log_message(self, *a):
        pass
    def send_json(self, obj, code=200, headers=None):
        b = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(b)
    def _session_ok(self):
        m = re.search(r"asa8sess=([A-Za-z0-9_\-]+)", self.headers.get("Cookie") or "")
        if not m:
            return False
        if SESSIONS.get(m.group(1), 0) < time.time():
            SESSIONS.pop(m.group(1), None)
            return False
        SESSIONS[m.group(1)] = time.time() + SESSION_TTL
        return True
    def _need_auth(self):
        self.send_json({"ok": False, "output": "Accesso richiesto: effettua il login"}, 401)
    def body(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        if n <= 0 or n > 200000:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8", "replace") or "{}")
        except Exception:
            return {}
    def do_import(self):
        """Riceve un file .db grezzo (body = bytes, nome in query)."""
        from urllib.parse import urlparse as _up, parse_qs as _pq
        qq = _pq(_up(self.path).query)
        fname = (qq.get("filename") or [""])[0]
        overwrite = (qq.get("overwrite") or ["0"])[0] == "1"
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        if n <= 0:
            return {"ok": False, "output": "File vuoto o dimensione mancante"}
        if n > MAX_IMPORT:
            return {"ok": False, "output": "File troppo grande (max 4 GB)"}
        if not re.match(r"^[A-Za-z0-9_][A-Za-z0-9_\-.]*\.db$", fname or "", re.IGNORECASE):
            return {"ok": False, "output": "Nome non valido: solo lettere, numeri, _ - . e deve finire con .db"}
        # normalizza estensione minuscola per coerenza
        if not fname.endswith(".db"):
            fname = fname[:-3] + ".db"
        os.makedirs(IMPORT_DIR, exist_ok=True)
        dest = os.path.join(IMPORT_DIR, fname)
        if os.path.exists(dest) and not overwrite:
            return {"ok": False, "output": f"Esiste già: {fname}. Spunta Sovrascrivi per sostituirlo."}
        if shutil.disk_usage(IMPORT_DIR).free < n + 500 * 1024 * 1024:
            return {"ok": False, "output": "Spazio insufficiente sul server"}
        tmp = dest + ".part-%d" % os.getpid()
        try:
            left = n
            with open(tmp, "wb") as f:
                while left > 0:
                    chunk = self.rfile.read(min(1024 * 1024, left))
                    if not chunk:
                        break
                    f.write(chunk)
                    left -= len(chunk)
            if left != 0:
                raise IOError("trasferimento interrotto")
            os.replace(tmp, dest)
            os.chmod(dest, 0o644)
            return {"ok": True, "output": f"Importato: {dest} ({n / 1048576:.1f} MB)"}
        except Exception as ex:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            return {"ok": False, "output": f"Errore: {ex}"}
    def _do_login(self):
        ip = self.client_address[0]
        if _throttled(ip):
            self.send_json({"ok": False, "output": "Troppi tentativi: riprova tra qualche minuto"}, 429)
            return
        try:
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n) if 0 < n <= 4096 else b""
            data = json.loads(raw.decode("utf-8", "replace") or "{}")
        except Exception:
            data = {}
        pw = get_password()
        if pw and hmac.compare_digest(str(data.get("password", "")), pw):
            FAILED.pop(ip, None)
            tok = secrets.token_urlsafe(32)
            SESSIONS[tok] = time.time() + SESSION_TTL
            self.send_json({"ok": True, "output": "Accesso effettuato"},
                           headers={"Set-Cookie": f"asa8sess={tok}; Path=/; Max-Age={SESSION_TTL}; HttpOnly; SameSite=Lax"})
        else:
            n0, t0 = FAILED.get(ip, (0, time.time()))
            FAILED[ip] = (n0 + 1, t0 if n0 else time.time())
            self.send_json({"ok": False,
                            "output": "Password non configurata sul server" if not pw else "Password errata"}, 401)
    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/login":
            if self._session_ok():
                self.send_response(302)
                self.send_header("Location", "/")
                self.end_headers()
            else:
                b = LOGIN_PAGE.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)
            return
        if not self._session_ok():
            if u.path.startswith("/api/"):
                self._need_auth()
            else:
                self.send_response(302)
                self.send_header("Location", "/login")
                self.end_headers()
            return
        if u.path in ("/", "/index.html"):
            b = PAGE.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)
            return
        if u.path == "/api/overview":
            try:
                hn = subprocess.run(["hostname", "-I"], capture_output=True, timeout=5)
                ip = hn.stdout.decode().strip().split()[0] if hn.stdout else ""
            except Exception:
                ip = ""
            import datetime
            engs = list_engines()
            # ping rapido best-effort (max 2 engine per non rallentare)
            for e in engs[:3]:
                if e["engine"] not in ("?", "err"):
                    rc, out = ping_engine(e["engine"])
                    e["ping"] = "✅ ok" if ("successful" in out) else ("❌ " + out.strip().splitlines()[-1][:80] if out.strip() else "❌")
            self.send_json({"dbs": dbs_used(engs), "engines": engs, "services": list_services(),
                            "ip": ip, "now": datetime.datetime.now().strftime("%d/%m/%Y %H:%M")})
            return
        if u.path == "/api/logs":
            q = parse_qs(u.query)
            fp = (q.get("file") or [""])[0]
            if not fp or ".." in fp or not os.path.isabs(fp):
                self.send_json({"ok": False, "output": "Percorso log non valido"})
                return
            try:
                p = subprocess.run(["tail", "-n", "100", fp], capture_output=True, timeout=10)
                out = p.stdout.decode("utf-8", "replace") + p.stderr.decode("utf-8", "replace")
                self.send_json({"ok": True, "output": out[-8000:] or "(vuoto)"})
            except Exception as ex:
                self.send_json({"ok": False, "output": str(ex)})
            return
        self.send_response(404)
        self.end_headers()
    def do_POST(self):
        if self.path == "/api/login":
            self._do_login()
            return
        if self.path == "/api/logout":
            m = re.search(r"asa8sess=([A-Za-z0-9_\-]+)", self.headers.get("Cookie") or "")
            if m:
                SESSIONS.pop(m.group(1), None)
            self.send_json({"ok": True, "output": "Uscito"},
                           headers={"Set-Cookie": "asa8sess=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax"})
            return
        if not self._session_ok():
            # per /api/import: non leggere il body se non autenticato
            self._need_auth()
            return
        # /api/import ha body binario: NON consumarlo con body()
        if self.path.startswith("/api/import"):
            self.send_json(self.do_import())
            return
        b = self.body()
        if self.path == "/api/ping":
            rc, out = ping_engine(b.get("engine", ""), b.get("user", "dba") or "dba", b.get("password", "sql") or "sql")
            self.send_json({"ok": rc == 0 or "successful" in out, "output": out})
        elif self.path == "/api/stop":
            rc, out = stop_engine(b.get("engine", ""), b.get("user", "dba") or "dba", b.get("password", "sql") or "sql")
            self.send_json({"ok": rc == 0, "output": out})
        elif self.path == "/api/start":
            rc, out = start_engine(b.get("engine", ""), b.get("db", ""), b.get("port") or None,
                                   b.get("log") or None, b.get("extra") or "")
            # verifica con ping se sembra partito
            if rc == 0:
                import time
                time.sleep(8)
                rc2, out2 = ping_engine(b.get("engine", ""))
                out += "\n--- ping ---\n" + out2
                self.send_json({"ok": "successful" in out2, "output": out})
            else:
                self.send_json({"ok": False, "output": out})
        elif self.path == "/api/createdb":
            rc, out = create_db(b.get("path", ""))
            self.send_json({"ok": rc == 0, "output": out})
        elif self.path == "/api/sql":
            rc, out = run_sql(b.get("engine", ""), b.get("sql", ""), b.get("user", "dba") or "dba",
                              b.get("password", "sql") or "sql")
            self.send_json({"ok": rc == 0, "output": out})
        elif self.path == "/api/mkservice":
            rc, out = make_service(b.get("name", ""), b.get("engine", ""), b.get("db", ""),
                                   b.get("port") or None, bool(b.get("autostart", True)))
            self.send_json({"ok": rc == 0, "output": out})
        elif self.path == "/api/svc-action":
            rc, out = svc_action(b.get("unit", ""), b.get("action", ""))
            self.send_json({"ok": rc == 0, "output": out})
        else:
            self.send_response(404)
            self.end_headers()

def main():
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), H)
    print(f"ASA8 panel su http://0.0.0.0:{PORT}", flush=True)
    srv.serve_forever()

if __name__ == "__main__":
    main()
