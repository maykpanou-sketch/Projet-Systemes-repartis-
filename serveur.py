"""
Serveur central de supervision - Multi-clients avec pool de threads
Reçoit les métriques des agents, les stocke en BD, détecte les pannes.
"""

import socket
import json
import sqlite3
import threading
import logging
import time
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor
from queue import Queue, Empty

# ─────────────────────────────────────────────
# CONFIGURATION
# ─────────────────────────────────────────────
HOST            = "0.0.0.0"   # Écoute sur toutes les interfaces
PORT            = 9999
MAX_WORKERS     = 20           # Taille du pool de threads
DB_PATH         = "supervision.db"
TIMEOUT_NODE    = 90           # Secondes sans données → nœud en panne
LOG_FILE        = "server.log"

# ─────────────────────────────────────────────
# LOGGING
# ─────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [SERVEUR] %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────
# BASE DE DONNÉES (SQLite + pool de connexions)
# ─────────────────────────────────────────────

# Pool de connexions SQLite simulé avec une Queue
DB_POOL_SIZE = 5
db_pool: Queue = Queue(maxsize=DB_POOL_SIZE)

def init_db_pool():
    """Crée le pool de connexions à la base de données."""
    for _ in range(DB_POOL_SIZE):
        conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        db_pool.put(conn)
    logger.info(f"Pool BD initialisé ({DB_POOL_SIZE} connexions)")

def get_db_conn():
    """Emprunte une connexion du pool (bloquant si toutes utilisées)."""
    return db_pool.get()

def release_db_conn(conn):
    """Rend la connexion au pool."""
    db_pool.put(conn)

def create_tables():
    """Crée les tables si elles n'existent pas."""
    conn = get_db_conn()
    try:
        cursor = conn.cursor()
        cursor.executescript("""
            CREATE TABLE IF NOT EXISTS nodes (
                node_id     TEXT PRIMARY KEY,
                os          TEXT,
                cpu_type    TEXT,
                first_seen  TEXT,
                last_seen   TEXT,
                status      TEXT DEFAULT 'ACTIVE'
            );

            CREATE TABLE IF NOT EXISTS metrics (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                node_id     TEXT,
                timestamp   TEXT,
                cpu         REAL,
                memory      REAL,
                disk        REAL,
                uptime      INTEGER,
                FOREIGN KEY (node_id) REFERENCES nodes(node_id)
            );

            CREATE TABLE IF NOT EXISTS services (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                metric_id   INTEGER,
                node_id     TEXT,
                timestamp   TEXT,
                name        TEXT,
                status      TEXT,
                FOREIGN KEY (metric_id) REFERENCES metrics(id)
            );

            CREATE TABLE IF NOT EXISTS ports (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                metric_id   INTEGER,
                node_id     TEXT,
                timestamp   TEXT,
                port        TEXT,
                status      TEXT,
                FOREIGN KEY (metric_id) REFERENCES metrics(id)
            );

            CREATE TABLE IF NOT EXISTS alerts (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                node_id     TEXT,
                timestamp   TEXT,
                message     TEXT
            );
        """)
        conn.commit()
        logger.info("Tables BD créées/vérifiées.")
    finally:
        release_db_conn(conn)

def save_metrics(data: dict):
    """Insère ou met à jour les métriques reçues d'un agent."""
    conn = get_db_conn()
    try:
        cursor = conn.cursor()
        node_id   = data["node_id"]
        timestamp = data["timestamp"]
        now       = datetime.now().isoformat()

        # Upsert du nœud
        cursor.execute("""
            INSERT INTO nodes (node_id, os, cpu_type, first_seen, last_seen, status)
            VALUES (?, ?, ?, ?, ?, 'ACTIVE')
            ON CONFLICT(node_id) DO UPDATE SET
                last_seen = excluded.last_seen,
                status    = 'ACTIVE'
        """, (node_id, data.get("os"), data.get("cpu_type"), now, now))

        # Insertion des métriques
        cursor.execute("""
            INSERT INTO metrics (node_id, timestamp, cpu, memory, disk, uptime)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (node_id, timestamp, data["cpu"], data["memory"],
              data["disk"], data["uptime"]))
        metric_id = cursor.lastrowid

        # Services
        for svc_name, svc_status in data.get("services", {}).items():
            cursor.execute("""
                INSERT INTO services (metric_id, node_id, timestamp, name, status)
                VALUES (?, ?, ?, ?, ?)
            """, (metric_id, node_id, timestamp, svc_name, svc_status))

        # Ports
        for port_num, port_status in data.get("ports", {}).items():
            cursor.execute("""
                INSERT INTO ports (metric_id, node_id, timestamp, port, status)
                VALUES (?, ?, ?, ?, ?)
            """, (metric_id, node_id, timestamp, port_num, port_status))

        # Alertes
        for alert_msg in data.get("alerts", []):
            cursor.execute("""
                INSERT INTO alerts (node_id, timestamp, message)
                VALUES (?, ?, ?)
            """, (node_id, timestamp, alert_msg))
            logger.warning(f"[ALERTE] {node_id} - {alert_msg}")

        conn.commit()
    finally:
        release_db_conn(conn)


# ─────────────────────────────────────────────
# SUIVI DES NŒUDS ACTIFS (en mémoire)
# ─────────────────────────────────────────────
# node_id -> {"last_seen": timestamp, "socket": socket}
active_nodes: dict = {}
nodes_lock = threading.Lock()

def update_node_seen(node_id: str, client_socket=None):
    with nodes_lock:
        active_nodes[node_id] = {
            "last_seen": time.time(),
            "socket": client_socket
        }

def mark_node_down(node_id: str):
    """Marque un nœud comme en panne dans la BD et dans les logs."""
    conn = get_db_conn()
    try:
        conn.execute(
            "UPDATE nodes SET status='DOWN' WHERE node_id=?", (node_id,))
        conn.execute(
            "INSERT INTO alerts (node_id, timestamp, message) VALUES (?,?,?)",
            (node_id, datetime.now().isoformat(),
             f"Nœud {node_id} considéré en panne (timeout {TIMEOUT_NODE}s)"))
        conn.commit()
    finally:
        release_db_conn(conn)
    logger.error(f"[PANNE] Nœud {node_id} hors ligne (aucune donnée depuis {TIMEOUT_NODE}s)")


# ─────────────────────────────────────────────
# VALIDATION DES MESSAGES
# ─────────────────────────────────────────────
REQUIRED_FIELDS = {"node_id", "timestamp", "cpu", "memory", "disk", "uptime"}

def validate_message(data: dict) -> bool:
    """Vérifie que les champs obligatoires sont présents et cohérents."""
    if not REQUIRED_FIELDS.issubset(data.keys()):
        return False
    for field in ("cpu", "memory", "disk"):
        val = data.get(field)
        if not isinstance(val, (int, float)) or not (0 <= val <= 100):
            return False
    return True


# ─────────────────────────────────────────────
# GESTION D'UN CLIENT (exécutée dans un thread du pool)
# ─────────────────────────────────────────────

def handle_client(client_socket: socket.socket, addr):
    """Traite la connexion d'un agent client."""
    logger.info(f"Nouvelle connexion: {addr}")
    try:
        client_socket.settimeout(10)
        buffer = ""
        # Lecture des données (peut arriver en plusieurs fragments)
        while True:
            try:
                chunk = client_socket.recv(4096).decode("utf-8")
                if not chunk:
                    break
                buffer += chunk
                if "\n" in buffer:
                    break
            except socket.timeout:
                break

        raw = buffer.strip()
        if not raw:
            client_socket.sendall(b"ERROR: message vide\n")
            return

        # Parsing JSON
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            logger.warning(f"Message JSON invalide de {addr}: {e}")
            client_socket.sendall(b"ERROR: JSON invalide\n")
            return

        # Validation
        if not validate_message(data):
            logger.warning(f"Message incomplet/invalide de {addr}")
            client_socket.sendall(b"ERROR: champs manquants ou invalides\n")
            return

        node_id = data["node_id"]
        logger.info(f"Métriques reçues de {node_id} | CPU={data['cpu']}% "
                    f"MEM={data['memory']}% DISK={data['disk']}%")

        # Sauvegarde en BD
        save_metrics(data)

        # Mise à jour de la dernière activité
        update_node_seen(node_id, client_socket)

        # Réponse ACK
        client_socket.sendall(b"ACK: metriques recues\n")

    except Exception as e:
        logger.error(f"Erreur avec le client {addr}: {e}")
        try:
            client_socket.sendall(b"ERROR: erreur serveur\n")
        except:
            pass
    finally:
        client_socket.close()


# ─────────────────────────────────────────────
# WATCHDOG : détection des nœuds en panne
# ─────────────────────────────────────────────

def watchdog():
    """Thread de surveillance : vérifie les nœuds silencieux."""
    logger.info("Watchdog démarré.")
    while True:
        time.sleep(10)
        now = time.time()
        with nodes_lock:
            to_mark = [
                nid for nid, info in active_nodes.items()
                if now - info["last_seen"] > TIMEOUT_NODE
            ]
        for nid in to_mark:
            mark_node_down(nid)
            with nodes_lock:
                del active_nodes[nid]


# ─────────────────────────────────────────────
# INTERFACE CONSOLE ADMINISTRATEUR
# ─────────────────────────────────────────────

def send_command_to_node(node_id: str, command: str):
    """Envoie une commande à un nœud actif (si sa socket est encore ouverte)."""
    with nodes_lock:
        node = active_nodes.get(node_id)
    if not node or not node.get("socket"):
        print(f"[!] Nœud {node_id} non disponible ou déconnecté.")
        return
    try:
        node["socket"].sendall(f"{command}\n".encode("utf-8"))
        print(f"[✓] Commande envoyée à {node_id}: {command}")
    except Exception as e:
        print(f"[!] Erreur d'envoi à {node_id}: {e}")

def show_nodes():
    """Affiche tous les nœuds connus et leur statut."""
    conn = get_db_conn()
    try:
        rows = conn.execute(
            "SELECT node_id, os, last_seen, status FROM nodes ORDER BY last_seen DESC"
        ).fetchall()
        print("\n{'─'*60}")
        print(f"{'NODE ID':<12} {'OS':<25} {'LAST SEEN':<22} {'STATUS'}")
        print("─"*60)
        for r in rows:
            print(f"{r['node_id']:<12} {str(r['os']):<25} {str(r['last_seen']):<22} {r['status']}")
        print("─"*60)
    finally:
        release_db_conn(conn)

def show_last_metrics(node_id: str):
    """Affiche les dernières métriques d'un nœud."""
    conn = get_db_conn()
    try:
        row = conn.execute("""
            SELECT * FROM metrics WHERE node_id=?
            ORDER BY id DESC LIMIT 1
        """, (node_id,)).fetchone()
        if not row:
            print(f"Aucune métrique pour {node_id}")
            return
        print(f"\n--- Dernières métriques : {node_id} ---")
        print(f"  Timestamp : {row['timestamp']}")
        print(f"  CPU       : {row['cpu']}%")
        print(f"  Mémoire   : {row['memory']}%")
        print(f"  Disque    : {row['disk']}%")
        print(f"  Uptime    : {row['uptime']}s")

        # Services
        svcs = conn.execute("""
            SELECT name, status FROM services WHERE metric_id=?
        """, (row["id"],)).fetchall()
        print("  Services  :", {s["name"]: s["status"] for s in svcs})

        # Ports
        ports = conn.execute("""
            SELECT port, status FROM ports WHERE metric_id=?
        """, (row["id"],)).fetchall()
        print("  Ports     :", {p["port"]: p["status"] for p in ports})
    finally:
        release_db_conn(conn)

def show_alerts():
    """Affiche les 20 dernières alertes."""
    conn = get_db_conn()
    try:
        rows = conn.execute("""
            SELECT node_id, timestamp, message FROM alerts
            ORDER BY id DESC LIMIT 20
        """).fetchall()
        print("\n--- Alertes récentes ---")
        for r in rows:
            print(f"  [{r['timestamp']}] {r['node_id']}: {r['message']}")
    finally:
        release_db_conn(conn)

def console_interface():
    """Boucle de l'interface console administrateur."""
    print("\n=== Interface Admin - Serveur de Supervision ===")
    print("Tapez 'aide' pour la liste des commandes.\n")
    while True:
        try:
            cmd = input("admin> ").strip()
            if not cmd:
                continue
            parts = cmd.split()
            action = parts[0].lower()

            if action == "aide":
                print("""
  noeuds                     → liste tous les nœuds
  metriques <node_id>        → dernières métriques d'un nœud
  alertes                    → dernières alertes
  up <node_id> <service>     → envoyer CMD:UP:<service> à un nœud
  quitter                    → arrêter le serveur
                """)
            elif action == "noeuds":
                show_nodes()
            elif action == "metriques" and len(parts) == 2:
                show_last_metrics(parts[1])
            elif action == "alertes":
                show_alerts()
            elif action == "up" and len(parts) == 3:
                send_command_to_node(parts[1], f"CMD:UP:{parts[2]}")
            elif action == "quitter":
                print("Arrêt du serveur.")
                import os; os._exit(0)
            else:
                print("Commande inconnue. Tapez 'aide'.")
        except KeyboardInterrupt:
            print("\nArrêt.")
            import os; os._exit(0)
        except Exception as e:
            print(f"Erreur: {e}")


# ─────────────────────────────────────────────
# DÉMARRAGE DU SERVEUR
# ─────────────────────────────────────────────

def start_server():
    """Démarre le serveur TCP et le pool de threads."""
    init_db_pool()
    create_tables()

    # Thread watchdog
    t_watchdog = threading.Thread(target=watchdog, daemon=True)
    t_watchdog.start()

    # Thread interface console (dans un thread séparé)
    t_console = threading.Thread(target=console_interface, daemon=True)
    t_console.start()

    # Pool de threads pour les clients
    # Choix : ThreadPoolExecutor (pool fixe) — bon compromis entre
    # performance et contrôle des ressources. Plus adapté qu'un thread
    # par client (risque d'épuisement) ou CachedThreadPool (non borné).
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server_socket.bind((HOST, PORT))
        server_socket.listen(100)
        logger.info(f"Serveur démarré sur {HOST}:{PORT} | Pool: {MAX_WORKERS} threads")

        while True:
            try:
                client_sock, addr = server_socket.accept()
                # Soumettre la gestion du client au pool
                executor.submit(handle_client, client_sock, addr)
            except KeyboardInterrupt:
                logger.info("Serveur arrêté.")
                break
            except Exception as e:
                logger.error(f"Erreur accept: {e}")


if __name__ == "__main__":
    start_server()