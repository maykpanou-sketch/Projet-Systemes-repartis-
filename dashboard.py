"""
Dashboard Web - Interface d'administration du serveur de supervision
Utilise Flask pour afficher les métriques en temps réel.
Lancer avec : python dashboard.py
Accès : http://localhost:5000
"""

from flask import Flask, jsonify, render_template_string, request
import sqlite3
import threading
import socket
import json

app = Flask(__name__)
DB_PATH = "supervision.db"

# ─────────────────────────────────────────────
# ACCÈS BASE DE DONNÉES
# ─────────────────────────────────────────────

def query_db(sql: str, params=()):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]

def exec_db(sql: str, params=()):
    conn = sqlite3.connect(DB_PATH)
    conn.execute(sql, params)
    conn.commit()
    conn.close()

# ─────────────────────────────────────────────
# API JSON
# ─────────────────────────────────────────────

@app.route("/api/nodes")
def api_nodes():
    rows = query_db("""
        SELECT n.node_id, n.os, n.cpu_type, n.last_seen, n.status,
               m.cpu, m.memory, m.disk, m.uptime
        FROM nodes n
        LEFT JOIN metrics m ON m.id = (
            SELECT MAX(id) FROM metrics WHERE node_id = n.node_id
        )
        ORDER BY n.last_seen DESC
    """)
    return jsonify(rows)

@app.route("/api/alerts")
def api_alerts():
    rows = query_db("""
        SELECT node_id, timestamp, message
        FROM alerts ORDER BY id DESC LIMIT 30
    """)
    return jsonify(rows)

@app.route("/api/metrics/<node_id>")
def api_metrics(node_id):
    rows = query_db("""
        SELECT timestamp, cpu, memory, disk
        FROM metrics WHERE node_id = ?
        ORDER BY id DESC LIMIT 20
    """, (node_id,))
    return jsonify(rows[::-1])  # ordre chronologique

@app.route("/api/services/<node_id>")
def api_services(node_id):
    rows = query_db("""
        SELECT s.name, s.status
        FROM services s
        WHERE s.metric_id = (
            SELECT MAX(id) FROM metrics WHERE node_id = ?
        )
    """, (node_id,))
    return jsonify(rows)

@app.route("/api/ports/<node_id>")
def api_ports(node_id):
    rows = query_db("""
        SELECT p.port, p.status
        FROM ports p
        WHERE p.metric_id = (
            SELECT MAX(id) FROM metrics WHERE node_id = ?
        )
    """, (node_id,))
    return jsonify(rows)

@app.route("/api/command", methods=["POST"])
def api_command():
    """Envoie une commande UP à un nœud via le serveur."""
    data = request.json
    node_id = data.get("node_id")
    service = data.get("service")
    if not node_id or not service:
        return jsonify({"error": "node_id et service requis"}), 400
    # On envoie la commande via socket au serveur principal
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.connect(("127.0.0.1", 9998))  # port admin interne
            s.sendall(json.dumps({"cmd": "UP", "node_id": node_id, "service": service}).encode())
        return jsonify({"status": "commande envoyée"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ─────────────────────────────────────────────
# PAGE HTML (dashboard)
# ─────────────────────────────────────────────

HTML = """
<!DOCTYPE html>
<html lang="fr">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Supervision Réseau</title>
  <script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.0/chart.umd.min.js"></script>
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body { font-family: 'Segoe UI', sans-serif; background: #0f1117; color: #e0e0e0; }

    header {
      background: #1a1d27;
      padding: 16px 32px;
      display: flex;
      align-items: center;
      gap: 12px;
      border-bottom: 1px solid #2a2d3e;
    }
    header h1 { font-size: 1.3rem; font-weight: 600; color: #fff; }
    .dot { width: 10px; height: 10px; border-radius: 50%; background: #22c55e;
           box-shadow: 0 0 8px #22c55e; animation: pulse 2s infinite; }
    @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:0.4} }

    .container { padding: 24px 32px; max-width: 1400px; margin: 0 auto; }

    .stats-row { display: grid; grid-template-columns: repeat(4, 1fr); gap: 16px; margin-bottom: 24px; }
    .stat-card {
      background: #1a1d27; border-radius: 12px; padding: 20px;
      border: 1px solid #2a2d3e;
    }
    .stat-card .label { font-size: 0.75rem; color: #888; text-transform: uppercase; letter-spacing: 1px; }
    .stat-card .value { font-size: 2rem; font-weight: 700; margin-top: 6px; }
    .value.green { color: #22c55e; }
    .value.red   { color: #ef4444; }
    .value.orange{ color: #f59e0b; }
    .value.blue  { color: #3b82f6; }

    .grid-2 { display: grid; grid-template-columns: 1fr 1fr; gap: 20px; margin-bottom: 24px; }
    .card {
      background: #1a1d27; border-radius: 12px; padding: 20px;
      border: 1px solid #2a2d3e;
    }
    .card h2 { font-size: 0.95rem; color: #aaa; margin-bottom: 16px; font-weight: 600;
               text-transform: uppercase; letter-spacing: 1px; }

    table { width: 100%; border-collapse: collapse; font-size: 0.85rem; }
    th { text-align: left; padding: 8px 12px; color: #666; font-weight: 500;
         border-bottom: 1px solid #2a2d3e; font-size: 0.75rem; text-transform: uppercase; }
    td { padding: 10px 12px; border-bottom: 1px solid #1e2132; }
    tr:hover td { background: #1e2132; }

    .badge {
      display: inline-block; padding: 3px 10px; border-radius: 20px;
      font-size: 0.72rem; font-weight: 600; letter-spacing: 0.5px;
    }
    .badge.active { background: #14532d; color: #22c55e; }
    .badge.down   { background: #450a0a; color: #ef4444; }
    .badge.ok     { background: #14532d; color: #22c55e; }
    .badge.warn   { background: #451a03; color: #f59e0b; }
    .badge.open   { background: #1e3a5f; color: #3b82f6; }
    .badge.closed { background: #1f1f1f; color: #666; }

    .progress-bar { background: #2a2d3e; border-radius: 4px; height: 6px; margin-top: 4px; }
    .progress-fill { height: 6px; border-radius: 4px; transition: width 0.5s; }
    .fill-green  { background: #22c55e; }
    .fill-orange { background: #f59e0b; }
    .fill-red    { background: #ef4444; }

    .node-row { cursor: pointer; }
    .node-row:hover td { background: #252838 !important; }

    #detail-panel {
      background: #1a1d27; border-radius: 12px; padding: 24px;
      border: 1px solid #2a2d3e; margin-bottom: 24px; display: none;
    }
    #detail-panel h2 { font-size: 1rem; color: #fff; margin-bottom: 16px; }
    .detail-grid { display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 16px; }

    .alert-item { padding: 10px 14px; border-left: 3px solid #f59e0b;
                  background: #1e1a0e; border-radius: 0 8px 8px 0; margin-bottom: 8px; }
    .alert-item .alert-msg  { font-size: 0.85rem; color: #fcd34d; }
    .alert-item .alert-meta { font-size: 0.72rem; color: #666; margin-top: 2px; }

    .btn {
      padding: 6px 14px; border-radius: 6px; border: none; cursor: pointer;
      font-size: 0.78rem; font-weight: 600; transition: opacity 0.2s;
    }
    .btn:hover { opacity: 0.8; }
    .btn-green { background: #16a34a; color: #fff; }
    .btn-blue  { background: #2563eb; color: #fff; }

    .refresh-info { font-size: 0.75rem; color: #444; text-align: right; margin-bottom: 8px; }

    canvas { max-height: 200px; }
  </style>
</head>
<body>

<header>
  <div class="dot"></div>
  <h1>🖥️ Supervision Réseau — Dashboard</h1>
  <span style="margin-left:auto;font-size:0.8rem;color:#555" id="last-update"></span>
</header>

<div class="container">

  <!-- Statistiques globales -->
  <div class="stats-row">
    <div class="stat-card">
      <div class="label">Nœuds actifs</div>
      <div class="value green" id="count-active">–</div>
    </div>
    <div class="stat-card">
      <div class="label">Nœuds en panne</div>
      <div class="value red" id="count-down">–</div>
    </div>
    <div class="stat-card">
      <div class="label">Total alertes</div>
      <div class="value orange" id="count-alerts">–</div>
    </div>
    <div class="stat-card">
      <div class="label">Nœuds total</div>
      <div class="value blue" id="count-total">–</div>
    </div>
  </div>

  <div class="refresh-info">Actualisation automatique toutes les 10 secondes</div>

  <!-- Tableau des nœuds -->
  <div class="card" style="margin-bottom:20px">
    <h2>Nœuds supervisés</h2>
    <table>
      <thead>
        <tr>
          <th>Node ID</th><th>OS</th><th>CPU</th><th>Mémoire</th>
          <th>Disque</th><th>Uptime</th><th>Statut</th><th>Dernière vue</th>
        </tr>
      </thead>
      <tbody id="nodes-tbody"></tbody>
    </table>
  </div>

  <!-- Panneau de détail d'un nœud -->
  <div id="detail-panel">
    <h2 id="detail-title">Détails du nœud</h2>
    <div class="detail-grid">
      <div class="card">
        <h2>Services</h2>
        <table><tbody id="detail-services"></tbody></table>
        <div style="margin-top:12px">
          <input id="svc-input" placeholder="nom du service..." style="
            background:#0f1117;border:1px solid #2a2d3e;color:#fff;
            padding:6px 10px;border-radius:6px;font-size:0.82rem;width:60%">
          <button class="btn btn-green" onclick="sendUp()" style="margin-left:8px">▶ UP</button>
        </div>
      </div>
      <div class="card">
        <h2>Ports</h2>
        <table><tbody id="detail-ports"></tbody></table>
      </div>
      <div class="card">
        <h2>CPU (historique)</h2>
        <canvas id="chart-cpu"></canvas>
      </div>
    </div>
  </div>

  <!-- Grille alertes + graphique mémoire -->
  <div class="grid-2">
    <div class="card">
      <h2>Alertes récentes</h2>
      <div id="alerts-list"></div>
    </div>
    <div class="card">
      <h2>Charge moyenne CPU / Mémoire</h2>
      <canvas id="chart-avg"></canvas>
    </div>
  </div>

</div>

<script>
let selectedNode = null;
let cpuChart = null;
let avgChart = null;

// ── Utilitaires ──────────────────────────────
function fmt(v) { return v != null ? v.toFixed(1) + "%" : "–"; }
function uptime(s) {
  if (!s) return "–";
  const h = Math.floor(s/3600), m = Math.floor((s%3600)/60);
  return `${h}h ${m}m`;
}
function fillColor(v) {
  if (v >= 90) return "fill-red";
  if (v >= 70) return "fill-orange";
  return "fill-green";
}
function bar(v) {
  const c = fillColor(v ?? 0);
  return `<div class="progress-bar"><div class="progress-fill ${c}" style="width:${v??0}%"></div></div>`;
}
function badge(status, map) {
  const cls = map[status?.toUpperCase()] ?? "closed";
  return `<span class="badge ${cls}">${status}</span>`;
}

// ── Chargement des nœuds ────────────────────
async function loadNodes() {
  const res = await fetch("/api/nodes");
  const nodes = await res.json();

  let active = 0, down = 0;
  let tbody = "";
  let cpuSum = 0, memSum = 0, cnt = 0;

  for (const n of nodes) {
    if (n.status === "ACTIVE") active++; else down++;
    const statusBadge = badge(n.status, {ACTIVE:"active", DOWN:"down"});
    if (n.cpu != null) { cpuSum += n.cpu; memSum += n.memory; cnt++; }
    tbody += `
      <tr class="node-row" onclick="selectNode('${n.node_id}')">
        <td><b>${n.node_id}</b></td>
        <td>${n.os ?? "–"}</td>
        <td>${fmt(n.cpu)}${bar(n.cpu)}</td>
        <td>${fmt(n.memory)}${bar(n.memory)}</td>
        <td>${fmt(n.disk)}${bar(n.disk)}</td>
        <td>${uptime(n.uptime)}</td>
        <td>${statusBadge}</td>
        <td style="color:#555;font-size:0.78rem">${(n.last_seen??"–").replace("T"," ").slice(0,19)}</td>
      </tr>`;
  }

  document.getElementById("nodes-tbody").innerHTML = tbody;
  document.getElementById("count-active").textContent = active;
  document.getElementById("count-down").textContent   = down;
  document.getElementById("count-total").textContent  = nodes.length;

  // Graphique moyenne
  updateAvgChart(cpuSum/cnt||0, memSum/cnt||0);
  document.getElementById("last-update").textContent =
    "Dernière mise à jour : " + new Date().toLocaleTimeString();
}

// ── Chargement des alertes ──────────────────
async function loadAlerts() {
  const res = await fetch("/api/alerts");
  const alerts = await res.json();
  document.getElementById("count-alerts").textContent = alerts.length;
  document.getElementById("alerts-list").innerHTML = alerts.length
    ? alerts.map(a => `
        <div class="alert-item">
          <div class="alert-msg">${a.message}</div>
          <div class="alert-meta">${a.node_id} · ${(a.timestamp??"").replace("T"," ").slice(0,19)}</div>
        </div>`).join("")
    : `<p style="color:#555;font-size:0.85rem">Aucune alerte.</p>`;
}

// ── Sélection d'un nœud ──────────────────────
async function selectNode(nodeId) {
  selectedNode = nodeId;
  document.getElementById("detail-panel").style.display = "block";
  document.getElementById("detail-title").textContent = "Détails : " + nodeId;

  // Services
  const svcs = await (await fetch(`/api/services/${nodeId}`)).json();
  document.getElementById("detail-services").innerHTML = svcs.map(s => `
    <tr>
      <td>${s.name}</td>
      <td>${badge(s.status, {OK:"ok", DOWN:"down"})}</td>
    </tr>`).join("") || "<tr><td colspan='2' style='color:#555'>Aucune donnée</td></tr>";

  // Ports
  const ports = await (await fetch(`/api/ports/${nodeId}`)).json();
  document.getElementById("detail-ports").innerHTML = ports.map(p => `
    <tr>
      <td>Port ${p.port}</td>
      <td>${badge(p.status, {OPEN:"open", CLOSED:"closed"})}</td>
    </tr>`).join("") || "<tr><td colspan='2' style='color:#555'>Aucune donnée</td></tr>";

  // Historique CPU
  const metrics = await (await fetch(`/api/metrics/${nodeId}`)).json();
  const labels = metrics.map(m => m.timestamp.slice(11,19));
  const cpuData = metrics.map(m => m.cpu);

  if (cpuChart) cpuChart.destroy();
  cpuChart = new Chart(document.getElementById("chart-cpu"), {
    type: "line",
    data: {
      labels,
      datasets: [{
        label: "CPU %", data: cpuData,
        borderColor: "#3b82f6", backgroundColor: "rgba(59,130,246,0.1)",
        tension: 0.4, fill: true, pointRadius: 3
      }]
    },
    options: {
      plugins: { legend: { display: false } },
      scales: {
        x: { ticks: { color:"#555", font:{size:10} }, grid:{color:"#1e2132"} },
        y: { min:0, max:100, ticks:{color:"#555"}, grid:{color:"#1e2132"} }
      }
    }
  });

  document.getElementById("detail-panel").scrollIntoView({ behavior: "smooth" });
}

// ── Graphique moyenne global ─────────────────
function updateAvgChart(avgCpu, avgMem) {
  if (avgChart) avgChart.destroy();
  avgChart = new Chart(document.getElementById("chart-avg"), {
    type: "bar",
    data: {
      labels: ["CPU moyen", "Mémoire moyenne"],
      datasets: [{
        data: [avgCpu.toFixed(1), avgMem.toFixed(1)],
        backgroundColor: ["#3b82f6","#8b5cf6"],
        borderRadius: 6
      }]
    },
    options: {
      plugins: { legend: { display: false } },
      scales: {
        x: { ticks:{color:"#555"}, grid:{color:"#1e2132"} },
        y: { min:0, max:100, ticks:{color:"#555"}, grid:{color:"#1e2132"} }
      }
    }
  });
}

// ── Commande UP ──────────────────────────────
async function sendUp() {
  const service = document.getElementById("svc-input").value.trim();
  if (!service || !selectedNode) return alert("Sélectionne un nœud et entre un service.");
  const res = await fetch("/api/command", {
    method: "POST",
    headers: {"Content-Type":"application/json"},
    body: JSON.stringify({ node_id: selectedNode, service })
  });
  const data = await res.json();
  alert(data.status ?? data.error);
}

// ── Boucle de rafraîchissement ───────────────
async function refresh() {
  await loadNodes();
  await loadAlerts();
  if (selectedNode) await selectNode(selectedNode);
}

refresh();
setInterval(refresh, 10000);
</script>
</body>
</html>
"""

@app.route("/")
def index():
    return render_template_string(HTML)

# ─────────────────────────────────────────────
# LANCEMENT
# ─────────────────────────────────────────────
if __name__ == "__main__":
    print("Dashboard démarré → http://localhost:5000")
    app.run(host="0.0.0.0", port=5000, debug=False)