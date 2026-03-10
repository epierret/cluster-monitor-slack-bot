#!/home/admin-nrik/mcp-venv/bin/python3

import os
import re
import socket
import subprocess
import datetime

import psutil
import docker as docker_sdk
from kubernetes import client, config

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

# ─────────────────────────────────────────────
# SLACK
# ─────────────────────────────────────────────
app = App(token=os.environ["SLACK_BOT_TOKEN"])

# ─────────────────────────────────────────────
# CLIENTS
# ─────────────────────────────────────────────
_v1 = None
_apps_v1 = None
_docker = None

def get_v1():
    global _v1
    if _v1 is None:
        config.load_kube_config(config_file="/home/admin-nrik/.kube/config")
        _v1 = client.CoreV1Api()
    return _v1

def get_apps_v1():
    global _apps_v1
    if _apps_v1 is None:
        config.load_kube_config(config_file="/home/admin-nrik/.kube/config")
        _apps_v1 = client.AppsV1Api()
    return _apps_v1

def get_docker():
    global _docker
    if _docker is None:
        _docker = docker_sdk.from_env()
    return _docker


# ══════════════════════════════════════════════
# HELP
# ══════════════════════════════════════════════

@app.message(re.compile(r"^help$", re.I))
def handle_help(message, say):
    say("""*🤖 DevOps Bot Commands*

*☸️ Kubernetes*
• `pods` — list pods (default namespace)
• `pods <namespace>` — list pods in namespace
• `pods all` — list all pods
• `nodes` — list nodes
• `deployments` — list deployments
• `services` — list services
• `namespaces` — list namespaces
• `logs <pod>` — last 50 lines of pod logs
• `logs <pod> <namespace>` — logs from specific namespace
• `describe <pod>` — pod details
• `deploy <name> <image>` — create a deployment
• `scale <deployment> <replicas>` — scale a deployment
• `delete pod <name>` — delete a pod
• `delete deployment <name>` — delete a deployment
• `restart <deployment>` — rollout restart

*🐳 Docker*
• `containers` — running containers
• `containers all` — all containers including stopped
• `images` — list images
• `dlogs <container>` — container logs
• `dstats <container>` — container CPU/mem stats
• `docker info` — docker system info

*🖥️ VM / System*
• `cpu` — CPU, RAM, disk, uptime
• `disks` — all mounted disks
• `top` — top 10 processes by CPU
• `vminfo` — hostname, OS, kernel
• `svcstatus` — running systemd services

*🌐 Network*
• `ports` — open listening ports
• `interfaces` — network interfaces
• `ping <host>` — check connectivity
• `portscan <host> <port>` — check if port is open
""")


# ══════════════════════════════════════════════
# KUBERNETES
# ══════════════════════════════════════════════

@app.message(re.compile(r"^namespaces$", re.I))
def handle_namespaces(message, say):
    try:
        ns_list = get_v1().list_namespace()
        names = [ns.metadata.name for ns in ns_list.items]
        say("*☸️ Namespaces:*\n" + "\n".join(f"• `{n}`" for n in names))
    except Exception as e:
        say(f"❌ Error: {e}")


@app.message(re.compile(r"^pods ?(.*)$", re.I))
def handle_pods(message, say, context):
    ns = context["matches"][0].strip() or "default"
    try:
        v1 = get_v1()
        pods = v1.list_pod_for_all_namespaces() if ns == "all" else v1.list_namespaced_pod(namespace=ns)
        if not pods.items:
            say(f"No pods found in `{ns}`")
            return
        lines = []
        for p in pods.items:
            ready = sum(1 for cs in (p.status.container_statuses or []) if cs.ready)
            total = len(p.spec.containers)
            restarts = sum(cs.restart_count for cs in (p.status.container_statuses or []))
            status = p.status.phase
            icon = "🟢" if status == "Running" else "🔴" if status == "Failed" else "🟡"
            lines.append(f"{icon} `{p.metadata.name}` | {status} | {ready}/{total} ready | restarts: {restarts} | ns: `{p.metadata.namespace}`")
        say(f"*☸️ Pods ({ns}):*\n" + "\n".join(lines))
    except Exception as e:
        say(f"❌ Error: {e}")


@app.message(re.compile(r"^nodes$", re.I))
def handle_nodes(message, say):
    try:
        nodes = get_v1().list_node()
        lines = []
        for n in nodes.items:
            conditions = {c.type: c.status for c in n.status.conditions}
            ready = conditions.get("Ready", "Unknown")
            icon = "🟢" if ready == "True" else "🔴"
            roles = [k.replace("node-role.kubernetes.io/", "") for k in n.metadata.labels if "node-role" in k] or ["worker"]
            lines.append(f"{icon} `{n.metadata.name}` | Ready: {ready} | Roles: {', '.join(roles)} | CPU: {n.status.capacity.get('cpu')} | RAM: {n.status.capacity.get('memory')}")
        say("*☸️ Nodes:*\n" + "\n".join(lines))
    except Exception as e:
        say(f"❌ Error: {e}")


@app.message(re.compile(r"^deployments ?(.*)$", re.I))
def handle_deployments(message, say, context):
    ns = context["matches"][0].strip() or "default"
    try:
        apps = get_apps_v1()
        deps = apps.list_deployment_for_all_namespaces() if ns == "all" else apps.list_namespaced_deployment(namespace=ns)
        if not deps.items:
            say(f"No deployments in `{ns}`")
            return
        lines = []
        for d in deps.items:
            ready = d.status.ready_replicas or 0
            desired = d.spec.replicas or 0
            icon = "🟢" if ready == desired else "🔴"
            image = d.spec.template.spec.containers[0].image if d.spec.template.spec.containers else "?"
            lines.append(f"{icon} `{d.metadata.name}` | {ready}/{desired} replicas | image: `{image}` | ns: `{d.metadata.namespace}`")
        say(f"*☸️ Deployments ({ns}):*\n" + "\n".join(lines))
    except Exception as e:
        say(f"❌ Error: {e}")


@app.message(re.compile(r"^services$", re.I))
def handle_k8s_services(message, say):
    try:
        svcs = get_v1().list_namespaced_service(namespace="default")
        lines = []
        for s in svcs.items:
            ports = ", ".join(f"{p.port}/{p.protocol}" for p in (s.spec.ports or []))
            lines.append(f"• `{s.metadata.name}` | {s.spec.type} | {s.spec.cluster_ip} | ports: {ports}")
        say("*☸️ Services (default):*\n" + "\n".join(lines))
    except Exception as e:
        say(f"❌ Error: {e}")


@app.message(re.compile(r"^logs (\S+) ?(\S*)$", re.I))
def handle_logs(message, say, context):
    pod_name = context["matches"][0]
    ns = context["matches"][1] or "default"
    try:
        logs = get_v1().read_namespaced_pod_log(name=pod_name, namespace=ns, tail_lines=50)
        say(f"*📋 Logs `{pod_name}`:*\n```{logs[-3000:]}```")
    except Exception as e:
        say(f"❌ Error: {e}")


@app.message(re.compile(r"^describe (\S+) ?(\S*)$", re.I))
def handle_describe(message, say, context):
    pod_name = context["matches"][0]
    ns = context["matches"][1] or "default"
    try:
        pod = get_v1().read_namespaced_pod(name=pod_name, namespace=ns)
        containers = []
        for c in pod.spec.containers:
            cs = next((s for s in (pod.status.container_statuses or []) if s.name == c.name), None)
            containers.append(f"  - `{c.name}` | {c.image} | ready: {cs.ready if cs else False} | restarts: {cs.restart_count if cs else 0}")
        say(
            f"*☸️ Pod `{pod_name}`:*\n"
            f"• Namespace: `{ns}`\n"
            f"• Node: `{pod.spec.node_name}`\n"
            f"• Status: `{pod.status.phase}`\n"
            f"• IP: `{pod.status.pod_ip}`\n"
            f"• Started: `{pod.status.start_time}`\n"
            f"• Containers:\n" + "\n".join(containers)
        )
    except Exception as e:
        say(f"❌ Error: {e}")


@app.message(re.compile(r"^deploy (\S+) (\S+)$", re.I))
def handle_deploy(message, say, context):
    name = context["matches"][0]
    image = context["matches"][1]
    try:
        body = client.V1Deployment(
            metadata=client.V1ObjectMeta(name=name),
            spec=client.V1DeploymentSpec(
                replicas=1,
                selector=client.V1LabelSelector(match_labels={"app": name}),
                template=client.V1PodTemplateSpec(
                    metadata=client.V1ObjectMeta(labels={"app": name}),
                    spec=client.V1PodSpec(containers=[
                        client.V1Container(name=name, image=image)
                    ])
                )
            )
        )
        get_apps_v1().create_namespaced_deployment(namespace="default", body=body)
        say(f"✅ Deployment `{name}` created with image `{image}`")
    except Exception as e:
        say(f"❌ Error: {e}")


@app.message(re.compile(r"^scale (\S+) (\d+)$", re.I))
def handle_scale(message, say, context):
    name = context["matches"][0]
    replicas = int(context["matches"][1])
    try:
        get_apps_v1().patch_namespaced_deployment_scale(
            name=name, namespace="default",
            body={"spec": {"replicas": replicas}}
        )
        say(f"✅ Deployment `{name}` scaled to `{replicas}` replicas")
    except Exception as e:
        say(f"❌ Error: {e}")


@app.message(re.compile(r"^restart (\S+)$", re.I))
def handle_restart(message, say, context):
    name = context["matches"][0]
    try:
        now = datetime.datetime.utcnow().isoformat()
        get_apps_v1().patch_namespaced_deployment(
            name=name, namespace="default",
            body={"spec": {"template": {"metadata": {"annotations": {"kubectl.kubernetes.io/restartedAt": now}}}}}
        )
        say(f"✅ Deployment `{name}` restarted")
    except Exception as e:
        say(f"❌ Error: {e}")


@app.message(re.compile(r"^delete (pod|deployment) (\S+)$", re.I))
def handle_delete(message, say, context):
    kind = context["matches"][0].lower()
    name = context["matches"][1]
    try:
        if kind == "pod":
            get_v1().delete_namespaced_pod(name=name, namespace="default")
        else:
            get_apps_v1().delete_namespaced_deployment(name=name, namespace="default")
        say(f"🗑️ {kind.capitalize()} `{name}` deleted")
    except Exception as e:
        say(f"❌ Error: {e}")


# ══════════════════════════════════════════════
# DOCKER
# ══════════════════════════════════════════════

@app.message(re.compile(r"^containers ?(.*)$", re.I))
def handle_containers(message, say, context):
    show_all = "all" in context["matches"][0].lower()
    try:
        containers = get_docker().containers.list(all=show_all)
        if not containers:
            say("No containers found")
            return
        lines = []
        for c in containers:
            icon = "🟢" if c.status == "running" else "🔴"
            image = c.image.tags[0] if c.image.tags else c.image.short_id
            lines.append(f"{icon} `{c.name}` | {c.status} | {image}")
        say("*🐳 Containers:*\n" + "\n".join(lines))
    except Exception as e:
        say(f"❌ Error: {e}")


@app.message(re.compile(r"^images$", re.I))
def handle_images(message, say):
    try:
        images = get_docker().images.list()
        lines = []
        for img in images:
            tags = ", ".join(img.tags) if img.tags else img.short_id
            size = round(img.attrs["Size"] / 1024 / 1024, 1)
            lines.append(f"• `{tags}` | {size} MB")
        say("*🐳 Docker Images:*\n" + "\n".join(lines))
    except Exception as e:
        say(f"❌ Error: {e}")


@app.message(re.compile(r"^dlogs (\S+)$", re.I))
def handle_docker_logs(message, say, context):
    name = context["matches"][0]
    try:
        c = get_docker().containers.get(name)
        logs = c.logs(tail=50, timestamps=True).decode("utf-8")
        say(f"*📋 Docker logs `{name}`:*\n```{logs[-3000:]}```")
    except Exception as e:
        say(f"❌ Error: {e}")


@app.message(re.compile(r"^dstats (\S+)$", re.I))
def handle_docker_stats(message, say, context):
    name = context["matches"][0]
    try:
        c = get_docker().containers.get(name)
        s = c.stats(stream=False)
        cpu_delta = s["cpu_stats"]["cpu_usage"]["total_usage"] - s["precpu_stats"]["cpu_usage"]["total_usage"]
        sys_delta = s["cpu_stats"]["system_cpu_usage"] - s["precpu_stats"]["system_cpu_usage"]
        cpus = s["cpu_stats"].get("online_cpus", 1)
        cpu_pct = round((cpu_delta / sys_delta) * cpus * 100, 2) if sys_delta > 0 else 0
        mem = s["memory_stats"]
        mem_used = round(mem["usage"] / 1024**2, 1)
        mem_limit = round(mem["limit"] / 1024**2, 1)
        mem_pct = round((mem["usage"] / mem["limit"]) * 100, 1)
        say(
            f"*🐳 Stats `{name}`:*\n"
            f"• CPU: `{cpu_pct}%`\n"
            f"• Memory: `{mem_used}MB / {mem_limit}MB ({mem_pct}%)`"
        )
    except Exception as e:
        say(f"❌ Error: {e}")


@app.message(re.compile(r"^docker info$", re.I))
def handle_docker_info(message, say):
    try:
        dc = get_docker()
        info = dc.info()
        say(
            f"*🐳 Docker Info:*\n"
            f"• Version: `{dc.version()['Version']}`\n"
            f"• Running: `{info['ContainersRunning']}`\n"
            f"• Stopped: `{info['ContainersStopped']}`\n"
            f"• Images: `{info['Images']}`\n"
            f"• CPUs: `{info['NCPU']}`\n"
            f"• Memory: `{round(info['MemTotal']/1024**3, 1)} GB`\n"
            f"• OS: `{info['OperatingSystem']}`"
        )
    except Exception as e:
        say(f"❌ Error: {e}")


# ══════════════════════════════════════════════
# VM / SYSTEM
# ══════════════════════════════════════════════

@app.message(re.compile(r"^cpu$", re.I))
def handle_cpu(message, say):
    try:
        cpu = psutil.cpu_percent(interval=1)
        ram = psutil.virtual_memory()
        disk = psutil.disk_usage("/")
        load = psutil.getloadavg()
        uptime = str(datetime.timedelta(seconds=int(datetime.datetime.now().timestamp() - psutil.boot_time())))
        say(
            f"*🖥️ System Stats:*\n"
            f"• CPU: `{cpu}%` | Load: `{round(load[0],2)} / {round(load[1],2)} / {round(load[2],2)}`\n"
            f"• RAM: `{ram.percent}%` ({round(ram.used/1024**3,1)}GB / {round(ram.total/1024**3,1)}GB)\n"
            f"• Disk /: `{disk.percent}%` ({round(disk.used/1024**3,1)}GB / {round(disk.total/1024**3,1)}GB)\n"
            f"• Uptime: `{uptime}`"
        )
    except Exception as e:
        say(f"❌ Error: {e}")


@app.message(re.compile(r"^disks$", re.I))
def handle_disks(message, say):
    try:
        lines = []
        for p in psutil.disk_partitions():
            try:
                u = psutil.disk_usage(p.mountpoint)
                lines.append(f"• `{p.device}` → `{p.mountpoint}` | {u.percent}% | free: {round(u.free/1024**3,1)}GB")
            except PermissionError:
                pass
        say("*🖥️ Disks:*\n" + "\n".join(lines))
    except Exception as e:
        say(f"❌ Error: {e}")


@app.message(re.compile(r"^top$", re.I))
def handle_top(message, say):
    try:
        procs = []
        for p in psutil.process_iter(["pid", "name", "cpu_percent", "memory_percent"]):
            try:
                procs.append(p.info)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        procs.sort(key=lambda x: x.get("cpu_percent") or 0, reverse=True)
        lines = [
            f"• `{p['name']}` (pid {p['pid']}) | CPU: {round(p['cpu_percent'],1)}% | MEM: {round(p['memory_percent'],1)}%"
            for p in procs[:10]
        ]
        say("*🖥️ Top 10 processes:*\n" + "\n".join(lines))
    except Exception as e:
        say(f"❌ Error: {e}")


@app.message(re.compile(r"^vminfo$", re.I))
def handle_vminfo(message, say):
    try:
        hostname = socket.gethostname()
        kernel = subprocess.run(["uname", "-r"], capture_output=True, text=True).stdout.strip()
        arch = subprocess.run(["uname", "-m"], capture_output=True, text=True).stdout.strip()
        with open("/etc/os-release") as f:
            os_info = dict(line.strip().split("=", 1) for line in f if "=" in line)
        say(
            f"*🖥️ VM Info:*\n"
            f"• Hostname: `{hostname}`\n"
            f"• OS: `{os_info.get('PRETTY_NAME','').strip(chr(34))}`\n"
            f"• Kernel: `{kernel}`\n"
            f"• Arch: `{arch}`"
        )
    except Exception as e:
        say(f"❌ Error: {e}")


@app.message(re.compile(r"^svcstatus$", re.I))
def handle_svcstatus(message, say):
    try:
        out = subprocess.run(
            ["systemctl", "list-units", "--type=service", "--state=running", "--no-pager", "--plain"],
            capture_output=True, text=True
        )
        lines = [l for l in out.stdout.splitlines() if ".service" in l][:20]
        say("*🖥️ Running Services:*\n```" + "\n".join(lines) + "```")
    except Exception as e:
        say(f"❌ Error: {e}")


# ══════════════════════════════════════════════
# NETWORK
# ══════════════════════════════════════════════

@app.message(re.compile(r"^ports$", re.I))
def handle_ports(message, say):
    try:
        conns = psutil.net_connections(kind="inet")
        seen = set()
        lines = []
        for c in conns:
            if c.status == "LISTEN" and c.laddr:
                try:
                    proc = psutil.Process(c.pid).name() if c.pid else "unknown"
                except Exception:
                    proc = "unknown"
                entry = f"• `{c.laddr.ip}:{c.laddr.port}` | {proc}"
                if entry not in seen:
                    seen.add(entry)
                    lines.append(entry)
        say("*🌐 Open Ports:*\n" + "\n".join(sorted(lines)[:30]))
    except Exception as e:
        say(f"❌ Error: {e}")


@app.message(re.compile(r"^interfaces$", re.I))
def handle_interfaces(message, say):
    try:
        addrs = psutil.net_if_addrs()
        stats = psutil.net_if_stats()
        io = psutil.net_io_counters(pernic=True)
        lines = []
        for iface, addr_list in addrs.items():
            ips = [a.address for a in addr_list if a.family == socket.AF_INET]
            up = stats[iface].isup if iface in stats else False
            icon = "🟢" if up else "🔴"
            iface_io = io.get(iface)
            traffic = f" | ↑{round(iface_io.bytes_sent/1024**2,1)}MB ↓{round(iface_io.bytes_recv/1024**2,1)}MB" if iface_io else ""
            lines.append(f"{icon} `{iface}` | {', '.join(ips) or 'no IP'}{traffic}")
        say("*🌐 Interfaces:*\n" + "\n".join(lines))
    except Exception as e:
        say(f"❌ Error: {e}")


@app.message(re.compile(r"^ping (\S+)$", re.I))
def handle_ping(message, say, context):
    host = context["matches"][0]
    try:
        out = subprocess.run(["ping", "-c", "3", "-W", "2", host], capture_output=True, text=True)
        reachable = out.returncode == 0
        icon = "✅" if reachable else "❌"
        latency = ""
        for line in out.stdout.splitlines():
            if "avg" in line or "rtt" in line:
                parts = line.split("/")
                if len(parts) >= 5:
                    latency = f" | avg: `{parts[4]}ms`"
        say(f"{icon} `{host}` {'reachable' if reachable else 'unreachable'}{latency}")
    except Exception as e:
        say(f"❌ Error: {e}")


@app.message(re.compile(r"^portscan (\S+) (\d+)$", re.I))
def handle_portscan(message, say, context):
    host = context["matches"][0]
    port = int(context["matches"][1])
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(2)
        result = sock.connect_ex((host, port))
        sock.close()
        if result == 0:
            say(f"✅ `{host}:{port}` is *open*")
        else:
            say(f"❌ `{host}:{port}` is *closed*")
    except Exception as e:
        say(f"❌ Error: {e}")


# ─────────────────────────────────────────────
# START
# ─────────────────────────────────────────────
if __name__ == "__main__":
    print("DevOps Slack bot starting...", flush=True)
    handler = SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"])
    handler.start()
