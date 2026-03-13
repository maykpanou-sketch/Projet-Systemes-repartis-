"""
Agent de supervision - Client TCP
Collecte les métriques système et les envoie au serveur central.
"""

import socket
import json
import time
import platform
import uuid
import logging
import psutil
from datetime import datetime

# ─────────────────────────────────────────────
# CONFIGURATION
# ─────────────────────────────────────────────
SERVER_HOST = "127.0.0.1"   # Adresse du serveur
SERVER_PORT = 9999           # Port du serveur
SEND_INTERVAL = 30           # Intervalle d'envoi en secondes
ALERT_THRESHOLD = 90         # Seuil d'alerte en %

# 3 services réseau + 3 applications grand public
SERVICES_TO_CHECK = {
    "ssh":      22,
    "http":     80,
    "dns":      53,
    "firefox":  None,   # application (process)
    "chrome":   None,
    "vlc":      None,
}

# 4 ports à surveiller
PORTS_TO_CHECK = [22, 80, 443, 3306]

# ID unique du nœud (généré une fois)
NODE_ID = str(uuid.uuid4())[:8]

# ─────────────────────────────────────────────
# LOGGING
# ─────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [AGENT] %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler("agent.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────
# COLLECTE DES MÉTRIQUES
# ─────────────────────────────────────────────

def get_cpu():
    """Retourne le pourcentage d'utilisation CPU."""
    return psutil.cpu_percent(interval=1)

def get_memory():
    """Retourne le pourcentage d'utilisation mémoire."""
    return psutil.virtual_memory().percent

def get_disk():
    """Retourne le pourcentage d'utilisation du disque (partition racine)."""
    return psutil.disk_usage("/").percent

def get_uptime():
    """Retourne l'uptime du système en secondes."""
    return int(time.time() - psutil.boot_time())

def get_os_info():
    """Retourne les infos sur l'OS et le processeur."""
    return {
        "os": platform.system() + " " + platform.release(),
        "cpu_type": platform.processor() or platform.machine()
    }

def check_port(port: int) -> bool:
    """Vérifie si un port local est ouvert."""
    connections = psutil.net_connections(kind="inet")
    for conn in connections:
        if conn.laddr.port == port and conn.status == "LISTEN":
            return True
    return False

def check_service(name: str, port) -> str:
    """
    Vérifie le statut d'un service :
    - Pour les services réseau : vérifie si le port est en écoute
    - Pour les applications : vérifie si un processus du même nom tourne
    """
    if port is not None:
        return "OK" if check_port(port) else "DOWN"
    else:
        # Cherche un processus dont le nom contient celui du service
        for proc in psutil.process_iter(["name"]):
            try:
                if name.lower() in proc.info["name"].lower():
                    return "OK"
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        return "DOWN"

def check_alerts(cpu: float, mem: float, disk: float) -> list:
    """Génère une liste d'alertes si un seuil est dépassé."""
    alerts = []
    if cpu > ALERT_THRESHOLD:
        alerts.append(f"ALERTE CPU: {cpu}% > {ALERT_THRESHOLD}%")
    if mem > ALERT_THRESHOLD:
        alerts.append(f"ALERTE MEM: {mem}% > {ALERT_THRESHOLD}%")
    if disk > ALERT_THRESHOLD:
        alerts.append(f"ALERTE DISK: {disk}% > {ALERT_THRESHOLD}%")
    return alerts

def collect_metrics() -> dict:
    """Collecte toutes les métriques et retourne un dictionnaire."""
    cpu  = get_cpu()
    mem  = get_memory()
    disk = get_disk()
    os_info = get_os_info()
    alerts = check_alerts(cpu, mem, disk)

    # Statut des services
    services = {name: check_service(name, port)
                for name, port in SERVICES_TO_CHECK.items()}

    # Statut des ports
    ports = {str(p): ("OPEN" if check_port(p) else "CLOSED")
             for p in PORTS_TO_CHECK}

    metrics = {
        "node_id":    NODE_ID,
        "timestamp":  datetime.now().isoformat(),
        "os":         os_info["os"],
        "cpu_type":   os_info["cpu_type"],
        "cpu":        cpu,
        "memory":     mem,
        "disk":       disk,
        "uptime":     get_uptime(),
        "services":   services,
        "ports":      ports,
        "alerts":     alerts
    }

    if alerts:
        for alert in alerts:
            logger.warning(alert)

    return metrics


# ─────────────────────────────────────────────
# ENVOI AU SERVEUR
# ─────────────────────────────────────────────

def send_metrics(metrics: dict) -> bool:
    """
    Envoie les métriques au serveur via une connexion TCP.
    Retourne True si l'envoi a réussi, False sinon.
    """
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(10)
            s.connect((SERVER_HOST, SERVER_PORT))

            # Sérialisation JSON + encodage UTF-8
            payload = json.dumps(metrics) + "\n"
            s.sendall(payload.encode("utf-8"))

            # Attente de la réponse du serveur
            response = s.recv(1024).decode("utf-8").strip()
            logger.info(f"Serveur: {response}")

            # Si le serveur envoie une commande UP
            if response.startswith("CMD:UP:"):
                service = response.split(":")[-1]
                logger.info(f"Commande reçue: activer le service '{service}'")
                # Ici on pourrait lancer le service (ex: os.system(f"systemctl start {service}"))

            return True

    except ConnectionRefusedError:
        logger.error(f"Connexion refusée - serveur {SERVER_HOST}:{SERVER_PORT} inaccessible")
    except socket.timeout:
        logger.error("Timeout - le serveur ne répond pas")
    except Exception as e:
        logger.error(f"Erreur d'envoi: {e}")

    return False


# ─────────────────────────────────────────────
# BOUCLE PRINCIPALE
# ─────────────────────────────────────────────

def main():
    logger.info(f"Agent démarré - Node ID: {NODE_ID}")
    logger.info(f"Serveur cible: {SERVER_HOST}:{SERVER_PORT}")
    logger.info(f"Intervalle d'envoi: {SEND_INTERVAL}s")

    while True:
        try:
            logger.info("Collecte des métriques...")
            metrics = collect_metrics()

            logger.info(
                f"CPU={metrics['cpu']}% | MEM={metrics['memory']}% | "
                f"DISK={metrics['disk']}% | UPTIME={metrics['uptime']}s"
            )

            success = send_metrics(metrics)
            if not success:
                logger.warning("Envoi échoué, nouvelle tentative au prochain cycle.")

        except KeyboardInterrupt:
            logger.info("Agent arrêté par l'utilisateur.")
            break
        except Exception as e:
            logger.error(f"Erreur inattendue: {e}")

        time.sleep(SEND_INTERVAL)


if __name__ == "__main__":
    main()
