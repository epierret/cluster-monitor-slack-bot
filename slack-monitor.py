#!/usr/bin/env python3
"""
Bot Slack de supervision : commandes Kubernetes, Docker et hôte Linux,
plus une boucle de surveillance qui envoie des alertes dans un canal Slack.

Variables d'environnement :
  SLACK_BOT_TOKEN       token du bot, xoxb-...               (obligatoire)
  SLACK_APP_TOKEN       token Socket Mode, xapp-...          (obligatoire)
  ALLOWED_SLACK_USERS   IDs Slack autorisés, séparés par des virgules
  ALERT_CHANNEL         ID du canal des alertes (ex. C0123456789)
  DEFAULT_NAMESPACE     namespace par défaut (défaut : default)
  KUBECONFIG            kubeconfig ; sinon configuration in-cluster
  MONITOR_INTERVAL      secondes entre deux contrôles (défaut : 60)
  ALERT_COOLDOWN        secondes avant de renvoyer une même alerte (défaut : 900)
  CPU_THRESHOLD         seuil CPU en % (défaut : 90)
  RAM_THRESHOLD         seuil RAM en % (défaut : 90)
  DISK_THRESHOLD        seuil disque / en % (défaut : 85)

Les contrôles de l'hôte ne reflètent la machine réelle que si le bot tourne
directement sur l'hôte (service systemd), pas dans un conteneur sans montages.
"""

import logging
import os
import platform
import re
import socket
import subprocess
import threading
import time
from datetime import datetime, timedelta, timezone

import docker as docker_sdk
import psutil
from kubernetes import client, config
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("slack-monitor")

# ─────────────────────────────────────────────
# CONFIGURATION
# ─────────────────────────────────────────────
DEFAULT_NS = os.environ.get("DEFAULT_NAMESPACE", "default")
ALLOWED_USERS = {u.strip() for u in os.environ.get("ALLOWED_SLACK_USERS", "").split(",") if u.strip()}
ALERT_CHANNEL = os.environ.get("ALERT_CHANNEL", "")
MONITOR_INTERVAL = int(os.environ.get("MONITOR_INTERVAL", "60"))
ALERT_COOLDOWN = int(os.environ.get("ALERT_COOLDOWN", "900"))
CPU_THRESHOLD = float(os.environ.get("CPU_THRESHOLD", "90"))
RAM_THRESHOLD = float(os.environ.get("RAM_THRESHOLD", "90"))
DISK_THRESHOLD = float(os.environ.get("DISK_THRESHOLD", "85"))

# Validation des entrées utilisateur
K8S_NAME = re.compile(r"^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$")
HOSTNAME = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9.-]{0,252}[A-Za-z0-9])?$")
IMAGE = re.compile(r"^[A-Za-z0-9._/:@-]{1,255}$")

for _var in ("SLACK_BOT_TOKEN", "SLACK_APP_TOKEN"):
    if not os.environ.get(_var):
        raise SystemExit(f"Variable d'environnement manquante : {_var}")

app = App(token=os.environ["SLACK_BOT_TOKEN"])

# ─────────────────────────────────────────────
# CLIENTS (initialisés une seule fois, avec verrou)
# ─────────────────────────────────────────────
_lock = threading.Lock()
_core = None
_apps = None
_docker = None


def _init_kube():
    global _core, _apps
    with _lock:
        if _core is None:
            try:
                config.load_kube_config()       # ~/.kube/config ou $KUBECONFIG
            except config.ConfigException:
                config.load_incluster_config()  # dans un pod
            _core = client.CoreV1Api()
            _apps = client.AppsV1Api()
    return _core, _apps


def core_api():
    return _init_kube()[0]


def apps_api():
    return _init_kube()[1]


def docker_client():
    global _docker
    with _lock:
        if _docker is None:
            _docker = docker_sdk.from_env()
        return _docker


# ─────────────────────────────────────────────
# AUTORISATION ET ERREURS
# ─────────────────────────────────────────────
def is_allowed(message, say):
    user = message.get("user")
    if user in ALLOWED_USERS:
        return True
    log.warning("Commande refusée pour l'utilisateur %s", user)
    say("⛔ Non autorisé")
    return False


def fail(say, action):
    """Journalise l'exception en cours et renvoie un message générique à Slack."""
    log.exception("Échec de la commande : %s", action)
    say("❌ Erreur pendant la commande. Détails dans les logs du bot.")


def os_pretty_name():
    try:
        with open("/etc/os-release") as f:
            for line in f:
                if line.startswith("PRETTY_NAME="):
                    return line.split("=", 1)[1].strip().strip('"')
    except OSError:
        pass
    return "inconnu"


# ─────────────────────────────────────────────
# AIDE
# ─────────────────────────────────────────────
@app.message(re.compile(r"^help$", re.I))
def handle_help(message, say):
    say("""*🤖 DevOps Bot Commands*
_🔒 = réservé aux utilisateurs autorisés_

*☸️ Kubernetes*
• `pods [namespace|all]` — liste des pods
• `nodes` — liste des nœuds
• `deployments [namespace|all]` — liste des deployments
• `services` — services du namespace par défaut
• `namespaces` — liste des namespaces
• `logs <pod> [namespace]` — 50 dernières lignes 🔒
• `describe <pod> [namespace]` — détail d'un pod 🔒
• `deploy <name> <image>` — créer un deployment 🔒
• `scale <deployment> <replicas>` — mettre à l'échelle 🔒
• `restart <deployment>` — rollout restart 🔒
• `delete pod|deployment <name>` — supprimer 🔒

*🐳 Docker*
• `containers [all]` — conteneurs (tous avec `all`)
• `images` — images locales
• `dlogs <container>` — logs d'un conteneur 🔒
• `dstats <container>` — CPU et mémoire
• `docker info` — informations Docker

*🖥️ VM / Système*
• `cpu` — CPU, RAM, disque, uptime
• `disks` — disques montés
• `top` — 10 processus les plus gourmands
• `vminfo` — hostname, OS, noyau
• `svcstatus` — services systemd actifs

*🌐 Réseau*
• `ports` — ports en écoute
• `interfaces` — interfaces réseau
• `ping <host>` — test de connectivité
• `portscan <host> <port>` — port ouvert ? 🔒
""")


# ─────────────────────────────────────────────
# KUBERNETES : LECTURE
# ─────────────────────────────────────────────
@app.message(re.compile(r"^namespaces$", re.I))
def handle_namespaces(message, say):
    try:
        names = [ns.metadata.name for ns in core_api().list_namespace().items]
        say("*☸️ Namespaces:*\n" + "\n".join(f"• `{n}`" for n in names))
    except Exception:
        fail(say, "namespaces")


@app.message(re.compile(r"^pods(?: +(\S+))?$", re.I))
def handle_pods(message, say, context):
    ns = (context["matches"][0] or DEFAULT_NS).lower()
    if ns != "all" and not K8S_NAME.match(ns):
        say("❌ Nom de namespace invalide")
        return
    try:
        if ns == "all":
            pods = core_api().list_pod_for_all_namespaces()
        else:
            pods = core_api().list_namespaced_pod(namespace=ns)
        if not pods.items:
            say(f"Aucun pod dans `{ns}`")
            return
        lines = []
        for p in pods.items:
            statuses = p.status.container_statuses or []
            ready = sum(1 for cs in statuses if cs.ready)
            restarts = sum(cs.restart_count for cs in statuses)
            phase = p.status.phase
            icon = "🟢" if phase == "Running" else "🔴" if phase == "Failed" else "🟡"
            lines.append(
                f"{icon} `{p.metadata.name}` | {phase} | {ready}/{len(p.spec.containers)} ready "
                f"| restarts: {restarts} | ns: `{p.metadata.namespace}`"
            )
        say(f"*☸️ Pods ({ns}):*\n" + "\n".join(lines))
    except Exception:
        fail(say, "pods")


@app.message(re.compile(r"^nodes$", re.I))
def handle_nodes(message, say):
    try:
        lines = []
        for n in core_api().list_node().items:
            ready = next((c.status for c in (n.status.conditions or []) if c.type == "Ready"), "Unknown")
            icon = "🟢" if ready == "True" else "🔴"
            labels = n.metadata.labels or {}
            roles = [k.split("/", 1)[1] for k in labels if k.startswith("node-role.kubernetes.io/")] or ["worker"]
            cap = n.status.capacity or {}
            lines.append(
                f"{icon} `{n.metadata.name}` | Ready: {ready} | Roles: {', '.join(roles)} "
                f"| CPU: {cap.get('cpu')} | RAM: {cap.get('memory')}"
            )
        say("*☸️ Nodes:*\n" + "\n".join(lines))
    except Exception:
        fail(say, "nodes")


@app.message(re.compile(r"^deployments(?: +(\S+))?$", re.I))
def handle_deployments(message, say, context):
    ns = (context["matches"][0] or DEFAULT_NS).lower()
    if ns != "all" and not K8S_NAME.match(ns):
        say("❌ Nom de namespace invalide")
        return
    try:
        if ns == "all":
            deps = apps_api().list_deployment_for_all_namespaces()
        else:
            deps = apps_api().list_namespaced_deployment(namespace=ns)
        if not deps.items:
            say(f"Aucun deployment dans `{ns}`")
            return
        lines = []
        for d in deps.items:
            ready = d.status.ready_replicas or 0
            desired = d.spec.replicas or 0
            icon = "🟢" if ready == desired else "🔴"
            containers = d.spec.template.spec.containers
            image = containers[0].image if containers else "?"
            lines.append(
                f"{icon} `{d.metadata.name}` | {ready}/{desired} replicas "
                f"| image: `{image}` | ns: `{d.metadata.namespace}`"
            )
        say(f"*☸️ Deployments ({ns}):*\n" + "\n".join(lines))
    except Exception:
        fail(say, "deployments")


@app.message(re.compile(r"^services$", re.I))
def handle_services(message, say):
    try:
        lines = []
        for s in core_api().list_namespaced_service(namespace=DEFAULT_NS).items:
            ports = ", ".join(f"{p.port}/{p.protocol}" for p in (s.spec.ports or []))
            lines.append(f"• `{s.metadata.name}` | {s.spec.type} | {s.spec.cluster_ip} | ports: {ports}")
        say(f"*☸️ Services ({DEFAULT_NS}):*\n" + "\n".join(lines))
    except Exception:
        fail(say, "services")


@app.message(re.compile(r"^logs +(\S+)(?: +(\S+))?$", re.I))
def handle_logs(message, say, context):
    if not is_allowed(message, say):
        return
    pod = context["matches"][0].lower()
    ns = (context["matches"][1] or DEFAULT_NS).lower()
    if not (K8S_NAME.match(pod) and K8S_NAME.match(ns)):
        say("❌ Nom de pod ou de namespace invalide")
        return
    try:
        logs = core_api().read_namespaced_pod_log(name=pod, namespace=ns, tail_lines=50)
        safe = logs[-3000:].replace("```", "'''")
        say(f"*📋 Logs `{pod}`:*\n```{safe}```")
    except Exception:
        fail(say, "logs")


@app.message(re.compile(r"^describe +(\S+)(?: +(\S+))?$", re.I))
def handle_describe(message, say, context):
    if not is_allowed(message, say):
        return
    pod = context["matches"][0].lower()
    ns = (context["matches"][1] or DEFAULT_NS).lower()
    if not (K8S_NAME.match(pod) and K8S_NAME.match(ns)):
        say("❌ Nom de pod ou de namespace invalide")
        return
    try:
        p = core_api().read_namespaced_pod(name=pod, namespace=ns)
        statuses = {s.name: s for s in (p.status.container_statuses or [])}
        lines = []
        for c in p.spec.containers:
            cs = statuses.get(c.name)
            lines.append(
                f"  - `{c.name}` | {c.image} | ready: {cs.ready if cs else False} "
                f"| restarts: {cs.restart_count if cs else 0}"
            )
        say(
            f"*☸️ Pod `{pod}`:*\n"
            f"• Namespace: `{ns}`\n"
            f"• Node: `{p.spec.node_name}`\n"
            f"• Status: `{p.status.phase}`\n"
            f"• IP: `{p.status.pod_ip}`\n"
            f"• Démarré: `{p.status.start_time}`\n"
            "• Conteneurs:\n" + "\n".join(lines)
        )
    except Exception:
        fail(say, "describe")


# ─────────────────────────────────────────────
# KUBERNETES : ACTIONS (réservées)
# ─────────────────────────────────────────────
@app.message(re.compile(r"^deploy +(\S+) +(\S+)$", re.I))
def handle_deploy(message, say, context):
    if not is_allowed(message, say):
        return
    name = context["matches"][0].lower()
    image = context["matches"][1]
    if not K8S_NAME.match(name) or not IMAGE.match(image):
        say("❌ Nom de deployment ou image invalide")
        return
    try:
        body = client.V1Deployment(
            metadata=client.V1ObjectMeta(name=name),
            spec=client.V1DeploymentSpec(
                replicas=1,
                selector=client.V1LabelSelector(match_labels={"app": name}),
                template=client.V1PodTemplateSpec(
                    metadata=client.V1ObjectMeta(labels={"app": name}),
                    spec=client.V1PodSpec(containers=[client.V1Container(name=name, image=image)]),
                ),
            ),
        )
        apps_api().create_namespaced_deployment(namespace=DEFAULT_NS, body=body)
        say(f"✅ Deployment `{name}` créé avec l'image `{image}`")
    except Exception:
        fail(say, "deploy")


@app.message(re.compile(r"^scale +(\S+) +(\d{1,3})$", re.I))
def handle_scale(message, say, context):
    if not is_allowed(message, say):
        return
    name = context["matches"][0].lower()
    replicas = int(context["matches"][1])
    if not K8S_NAME.match(name) or replicas > 50:
        say("❌ Nom invalide, ou plus de 50 réplicas demandés")
        return
    try:
        apps_api().patch_namespaced_deployment_scale(
            name=name, namespace=DEFAULT_NS, body={"spec": {"replicas": replicas}}
        )
        say(f"✅ Deployment `{name}` mis à l'échelle : {replicas} réplica(s)")
    except Exception:
        fail(say, "scale")


@app.message(re.compile(r"^restart +(\S+)$", re.I))
def handle_restart(message, say, context):
    if not is_allowed(message, say):
        return
    name = context["matches"][0].lower()
    if not K8S_NAME.match(name):
        say("❌ Nom de deployment invalide")
        return
    try:
        now = datetime.now(timezone.utc).isoformat()
        body = {"spec": {"template": {"metadata": {"annotations": {"kubectl.kubernetes.io/restartedAt": now}}}}}
        apps_api().patch_namespaced_deployment(name=name, namespace=DEFAULT_NS, body=body)
        say(f"✅ Deployment `{name}` redémarré")
    except Exception:
        fail(say, "restart")


@app.message(re.compile(r"^delete +(pod|deployment) +(\S+)$", re.I))
def handle_delete(message, say, context):
    if not is_allowed(message, say):
        return
    kind = context["matches"][0].lower()
    name = context["matches"][1].lower()
    if not K8S_NAME.match(name):
        say("❌ Nom invalide")
        return
    try:
        if kind == "pod":
            core_api().delete_namespaced_pod(name=name, namespace=DEFAULT_NS)
        else:
            apps_api().delete_namespaced_deployment(name=name, namespace=DEFAULT_NS)
        say(f"🗑️ {kind.capitalize()} `{name}` supprimé")
    except Exception:
        fail(say, f"delete {kind}")


# ─────────────────────────────────────────────
# DOCKER
# ─────────────────────────────────────────────
@app.message(re.compile(r"^containers(?: +(all))?$", re.I))
def handle_containers(message, say, context):
    show_all = bool(context["matches"][0])
    try:
        containers = docker_client().containers.list(all=show_all)
        if not containers:
            say("Aucun conteneur")
            return
        lines = []
        for c in containers:
            icon = "🟢" if c.status == "running" else "🔴"
            image = c.image.tags[0] if c.image.tags else c.image.short_id
            lines.append(f"{icon} `{c.name}` | {c.status} | {image}")
        say("*🐳 Conteneurs:*\n" + "\n".join(lines))
    except Exception:
        fail(say, "containers")


@app.message(re.compile(r"^images$", re.I))
def handle_images(message, say):
    try:
        lines = []
        for img in docker_client().images.list():
            tags = ", ".join(img.tags) if img.tags else img.short_id
            size = img.attrs.get("Size", 0) / 1024**2
            lines.append(f"• `{tags}` | {size:.1f} Mo")
        say("*🐳 Images:*\n" + "\n".join(lines))
    except Exception:
        fail(say, "images")


@app.message(re.compile(r"^dlogs +(\S+)$", re.I))
def handle_dlogs(message, say, context):
    if not is_allowed(message, say):
        return
    name = context["matches"][0]
    try:
        raw = docker_client().containers.get(name).logs(tail=50, timestamps=True)
        safe = raw.decode("utf-8", "replace")[-3000:].replace("```", "'''")
        say(f"*📋 Logs Docker `{name}`:*\n```{safe}```")
    except Exception:
        fail(say, "dlogs")


@app.message(re.compile(r"^dstats +(\S+)$", re.I))
def handle_dstats(message, say, context):
    name = context["matches"][0]
    try:
        s = docker_client().containers.get(name).stats(stream=False)
        cpu = s["cpu_stats"]
        pre = s["precpu_stats"]
        cpu_delta = cpu["cpu_usage"]["total_usage"] - pre["cpu_usage"]["total_usage"]
        sys_delta = cpu["system_cpu_usage"] - pre["system_cpu_usage"]
        ncpu = cpu.get("online_cpus") or 1
        cpu_pct = round(cpu_delta / sys_delta * ncpu * 100, 2) if sys_delta > 0 else 0.0
        mem = s["memory_stats"]
        usage = mem.get("usage", 0)
        limit = mem.get("limit") or 1
        say(
            f"*🐳 Stats `{name}`:*\n"
            f"• CPU: `{cpu_pct}%`\n"
            f"• Mémoire: `{usage / 1024**2:.1f} Mo / {limit / 1024**2:.1f} Mo ({usage / limit * 100:.1f}%)`"
        )
    except Exception:
        fail(say, "dstats")


@app.message(re.compile(r"^docker info$", re.I))
def handle_docker_info(message, say):
    try:
        dc = docker_client()
        info = dc.info()
        say(
            "*🐳 Docker:*\n"
            f"• Version: `{dc.version()['Version']}`\n"
            f"• En cours: `{info['ContainersRunning']}` | Arrêtés: `{info['ContainersStopped']}`\n"
            f"• Images: `{info['Images']}`\n"
            f"• CPU: `{info['NCPU']}` | RAM: `{info['MemTotal'] / 1024**3:.1f} Go`\n"
            f"• OS: `{info['OperatingSystem']}`"
        )
    except Exception:
        fail(say, "docker info")


# ─────────────────────────────────────────────
# VM / SYSTÈME
# ─────────────────────────────────────────────
@app.message(re.compile(r"^cpu$", re.I))
def handle_cpu(message, say):
    try:
        cpu = psutil.cpu_percent(interval=1)
        ram = psutil.virtual_memory()
        disk = psutil.disk_usage("/")
        l1, l5, l15 = psutil.getloadavg()
        uptime = timedelta(seconds=int(time.time() - psutil.boot_time()))
        say(
            "*🖥️ Stats système:*\n"
            f"• CPU: `{cpu}%` | Load: `{l1:.2f} / {l5:.2f} / {l15:.2f}`\n"
            f"• RAM: `{ram.percent}%` ({ram.used / 1024**3:.1f} Go / {ram.total / 1024**3:.1f} Go)\n"
            f"• Disque /: `{disk.percent}%` ({disk.used / 1024**3:.1f} Go / {disk.total / 1024**3:.1f} Go)\n"
            f"• Uptime: `{uptime}`"
        )
    except Exception:
        fail(say, "cpu")


@app.message(re.compile(r"^disks$", re.I))
def handle_disks(message, say):
    try:
        lines = []
        for part in psutil.disk_partitions():
            try:
                u = psutil.disk_usage(part.mountpoint)
            except OSError:
                continue
            lines.append(
                f"• `{part.device}` → `{part.mountpoint}` | {u.percent}% | libre : {u.free / 1024**3:.1f} Go"
            )
        say("*🖥️ Disques:*\n" + "\n".join(lines))
    except Exception:
        fail(say, "disks")


@app.message(re.compile(r"^top$", re.I))
def handle_top(message, say):
    try:
        procs = list(psutil.process_iter(["pid", "name", "memory_percent"]))
        for p in procs:
            try:
                p.cpu_percent(None)  # premier appel d'amorçage : renvoie 0
            except psutil.Error:
                pass
        time.sleep(1)
        rows = []
        for p in procs:
            try:
                rows.append((p.cpu_percent(None), p.info["name"], p.info["pid"], p.info["memory_percent"] or 0.0))
            except psutil.Error:
                continue
        rows.sort(reverse=True)
        lines = [
            f"• `{name}` (pid {pid}) | CPU: {cpu:.1f}% | MEM: {mem:.1f}%"
            for cpu, name, pid, mem in rows[:10]
        ]
        say("*🖥️ Top 10 processus:*\n" + "\n".join(lines))
    except Exception:
        fail(say, "top")


@app.message(re.compile(r"^vminfo$", re.I))
def handle_vminfo(message, say):
    say(
        "*🖥️ Infos VM:*\n"
        f"• Hostname: `{socket.gethostname()}`\n"
        f"• OS: `{os_pretty_name()}`\n"
        f"• Noyau: `{platform.release()}`\n"
        f"• Architecture: `{platform.machine()}`"
    )


@app.message(re.compile(r"^svcstatus$", re.I))
def handle_svcstatus(message, say):
    try:
        out = subprocess.run(
            ["systemctl", "list-units", "--type=service", "--state=running", "--no-pager", "--plain"],
            capture_output=True, text=True, timeout=10,
        )
        lines = [line for line in out.stdout.splitlines() if ".service" in line][:20]
        say("*🖥️ Services actifs:*\n```" + "\n".join(lines) + "```")
    except Exception:
        fail(say, "svcstatus")


# ─────────────────────────────────────────────
# RÉSEAU
# ─────────────────────────────────────────────
@app.message(re.compile(r"^ports$", re.I))
def handle_ports(message, say):
    try:
        entries = set()
        for c in psutil.net_connections(kind="inet"):
            if c.status == "LISTEN" and c.laddr:
                try:
                    proc = psutil.Process(c.pid).name() if c.pid else "inconnu"
                except psutil.Error:
                    proc = "inconnu"
                entries.add(f"• `{c.laddr.ip}:{c.laddr.port}` | {proc}")
        say("*🌐 Ports en écoute:*\n" + "\n".join(sorted(entries)[:30]))
    except Exception:
        fail(say, "ports")


@app.message(re.compile(r"^interfaces$", re.I))
def handle_interfaces(message, say):
    try:
        addrs = psutil.net_if_addrs()
        stats = psutil.net_if_stats()
        counters = psutil.net_io_counters(pernic=True)
        lines = []
        for iface, addr_list in addrs.items():
            ips = [a.address for a in addr_list if a.family == socket.AF_INET]
            up = stats[iface].isup if iface in stats else False
            io = counters.get(iface)
            traffic = (
                f" | ↑{io.bytes_sent / 1024**2:.1f} Mo ↓{io.bytes_recv / 1024**2:.1f} Mo" if io else ""
            )
            icon = "🟢" if up else "🔴"
            lines.append(f"{icon} `{iface}` | {', '.join(ips) or 'sans IP'}{traffic}")
        say("*🌐 Interfaces:*\n" + "\n".join(lines))
    except Exception:
        fail(say, "interfaces")


@app.message(re.compile(r"^ping +(\S+)$", re.I))
def handle_ping(message, say, context):
    host = context["matches"][0]
    if not HOSTNAME.match(host):
        say("❌ Hôte invalide")
        return
    try:
        out = subprocess.run(
            ["ping", "-c", "3", "-W", "2", "--", host],
            capture_output=True, text=True, timeout=15,
        )
        if out.returncode != 0:
            say(f"❌ `{host}` injoignable")
            return
        match = re.search(r"= ([\d.]+)/([\d.]+)/", out.stdout)
        latency = f" | moyenne : `{match.group(2)} ms`" if match else ""
        say(f"✅ `{host}` joignable{latency}")
    except subprocess.TimeoutExpired:
        say(f"❌ `{host}` : délai dépassé")
    except Exception:
        fail(say, "ping")


@app.message(re.compile(r"^portscan +(\S+) +(\d{1,5})$", re.I))
def handle_portscan(message, say, context):
    if not is_allowed(message, say):
        return
    host = context["matches"][0]
    port = int(context["matches"][1])
    if not HOSTNAME.match(host) or not 1 <= port <= 65535:
        say("❌ Hôte ou port invalide")
        return
    try:
        with socket.create_connection((host, port), timeout=2):
            say(f"✅ `{host}:{port}` est *ouvert*")
    except OSError:
        say(f"❌ `{host}:{port}` est *fermé* ou filtré")


# ─────────────────────────────────────────────
# SURVEILLANCE ET ALERTES
# ─────────────────────────────────────────────
_last_alert = {}  # clé -> horodatage du dernier envoi


def alert(key, text):
    """Envoie une alerte, sauf si la même clé a été envoyée pendant ALERT_COOLDOWN."""
    now = time.monotonic()
    last = _last_alert.get(key)
    if last is not None and now - last < ALERT_COOLDOWN:
        return
    _last_alert[key] = now
    log.warning("Alerte [%s] %s", key, text)
    if ALERT_CHANNEL:
        app.client.chat_postMessage(channel=ALERT_CHANNEL, text=text)


BAD_WAITING = {"CrashLoopBackOff", "ImagePullBackOff", "ErrImagePull", "CreateContainerConfigError"}


def check_nodes():
    for n in core_api().list_node().items:
        ready = next((c.status for c in (n.status.conditions or []) if c.type == "Ready"), "Unknown")
        if ready != "True":
            alert(f"node:{n.metadata.name}", f"🔴 Nœud `{n.metadata.name}` non prêt (Ready={ready})")


def check_pods():
    for p in core_api().list_pod_for_all_namespaces().items:
        ref = f"{p.metadata.namespace}/{p.metadata.name}"
        if p.status.phase == "Failed":
            alert(f"pod-failed:{ref}", f"🔴 Pod `{ref}` en échec")
        for cs in p.status.container_statuses or []:
            waiting = cs.state.waiting if cs.state else None
            if waiting and waiting.reason in BAD_WAITING:
                alert(
                    f"pod:{ref}:{cs.name}",
                    f"🔴 Pod `{ref}` (conteneur `{cs.name}`) : {waiting.reason}",
                )


def check_host():
    cpu = psutil.cpu_percent(interval=1)
    ram = psutil.virtual_memory().percent
    disk = psutil.disk_usage("/").percent
    if cpu >= CPU_THRESHOLD:
        alert("host:cpu", f"🟠 CPU à {cpu:.0f} % (seuil {CPU_THRESHOLD:.0f} %)")
    if ram >= RAM_THRESHOLD:
        alert("host:ram", f"🟠 RAM à {ram:.0f} % (seuil {RAM_THRESHOLD:.0f} %)")
    if disk >= DISK_THRESHOLD:
        alert("host:disk", f"🟠 Disque / à {disk:.0f} % (seuil {DISK_THRESHOLD:.0f} %)")


def monitor_loop():
    log.info("Surveillance active toutes les %s s", MONITOR_INTERVAL)
    checks = (check_nodes, check_pods, check_host)
    while True:
        for check in checks:
            try:
                check()
            except Exception:
                log.exception("Contrôle en échec : %s", check.__name__)
        time.sleep(MONITOR_INTERVAL)


# ─────────────────────────────────────────────
# DÉMARRAGE
# ─────────────────────────────────────────────
if __name__ == "__main__":
    if not ALLOWED_USERS:
        log.warning("ALLOWED_SLACK_USERS est vide : les commandes sensibles sont refusées.")
    if not ALERT_CHANNEL:
        log.warning("ALERT_CHANNEL est vide : les alertes sont seulement journalisées.")
    threading.Thread(target=monitor_loop, name="monitor", daemon=True).start()
    log.info("Bot démarré")
    SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"]).start()
