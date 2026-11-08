"""Build and run the dashboard on a Linux Docker host over SSH."""

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shlex
import subprocess
import time
from urllib.request import urlopen


ROOT = Path(__file__).resolve().parent
INVENTORY = r'''
import json, pathlib, re, socket, subprocess
filesystems = []
seen = set()
for line in pathlib.Path('/proc/self/mountinfo').read_text().splitlines():
    left, right = line.split(' - ', 1)
    values, details = left.split(), right.split()
    kind = details[0]
    if kind not in ('ext4', 'xfs', 'btrfs', 'vfat', 'exfat', 'zfs', 'fuse.mergerfs'):
        continue
    path = re.sub(r'\\([0-7]{3})', lambda m: chr(int(m[1], 8)), values[4])
    key = (values[2], values[3])
    if key in seen or not pathlib.Path(path).is_dir():
        continue
    seen.add(key)
    filesystems.append({'path': path, 'type': kind})
gpus = {}
for card in pathlib.Path('/sys/class/drm').glob('card*'):
    if not re.fullmatch(r'card\d+', card.name): continue
    device = card / 'device'
    fields = dict(line.split('=', 1) for line in (device/'uevent').read_text().splitlines() if '=' in line)
    pci = fields.get('PCI_SLOT_NAME')
    if not pci: continue
    driver = (device/'driver').resolve().name
    ident = (device/'device').read_text().strip()
    vendor = (device/'vendor').read_text().strip()
    description = subprocess.check_output(['lspci', '-s', pci], text=True).strip().split(': ', 1)[-1]
    name = {'0x7d67': 'Intel Graphics', '0xe20b': 'Intel Arc B580'}.get(ident, description) if vendor == '0x8086' else description
    gpus[pci] = {'name': name, 'kind': 'integrated' if driver == 'i915' or pci == '0000:00:02.0' else 'discrete'}
print(json.dumps({'hostname': socket.gethostname(), 'gpus': gpus, 'filesystems': sorted(filesystems, key=lambda f: f['path'])}))
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ssh", required=True, help="SSH user@host")
    parser.add_argument("--identity", required=True, help="Existing SSH key path")
    parser.add_argument("--host-path", required=True, help="This project directory on the physical host")
    parser.add_argument("--bind", required=True, help="Private host address")
    parser.add_argument("--port", type=int, default=18017)
    args = parser.parse_args()
    ssh = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "-o", "IdentitiesOnly=yes", "-i", args.identity, args.ssh]

    def remote(command, input=None, check=True):
        result = subprocess.run(ssh + [shlex.join(command)], input=input, text=True, capture_output=True)
        if check and result.returncode:
            raise RuntimeError(result.stderr or result.stdout)
        return result

    inventory = json.loads(remote(["python3", "-"], input=INVENTORY).stdout)
    for i, fs in enumerate(inventory["filesystems"]):
        fs["mount"] = f"/filesystems/{i}"
    runtime = ROOT / "runtime"
    runtime.mkdir(exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    config = runtime / ("config-" + stamp + ".json")
    config.write_text(json.dumps(inventory, indent=2) + "\n")
    # The workspace is shared with the host; verify the path before starting.
    digest = hashlib.sha256(config.read_bytes()).hexdigest()
    host_config = args.host_path + "/runtime/" + config.name
    actual = remote(["sha256sum", host_config]).stdout.split()[0]
    if actual != digest:
        raise RuntimeError("The host project directory does not match the local workspace")
    image = "local/host-dashboard:" + stamp
    print("Building", image, flush=True)
    build = remote(["docker", "build", "-t", image, args.host_path])
    print(build.stderr[-2500:], flush=True)
    remote(["docker", "volume", "create", "host-dashboard-metrics"])
    common = ["--restart", "unless-stopped", "--read-only", "--cap-drop", "ALL",
              "--security-opt", "no-new-privileges:true", "--pids-limit", "64",
              "--cpus", "0.5", "--label", "app=host-dashboard",
              "--log-opt", "max-size=5m", "--log-opt", "max-file=2"]
    health = "python -c 'import json,time; d=json.load(open(\"/data/metrics.json\")); assert -5 <= time.time()-d[\"timestamp\"] < 10'"
    collector = ["docker", "run", "-d", "--name", "host-dashboard-collector", *common,
                 "--memory", "192m", "--memory-swap", "192m", "--user", "0:0", "--network", "none",
                 "--cap-add", "SYS_PTRACE", "--cap-add", "DAC_READ_SEARCH",
                 "--security-opt", "apparmor=unconfined",
                 "--mount", "type=bind,src=/proc,dst=/host/proc,readonly,bind-recursive=disabled",
                 "--mount", "type=bind,src=/sys,dst=/host/sys,readonly,bind-recursive=disabled",
                 "--mount", f"type=bind,src={host_config},dst=/config/config.json,readonly",
                 "--mount", "type=volume,src=host-dashboard-metrics,dst=/data",
                 "--health-cmd", health, "--health-interval", "10s", "--health-timeout", "3s",
                 "--health-start-period", "10s", "--health-retries", "3"]
    for fs in inventory["filesystems"]:
        collector += ["--mount", f"type=bind,src={fs['path']},dst={fs['mount']},readonly,bind-recursive=disabled"]
    collector += [image, "python", "collector.py"]
    web = ["docker", "run", "-d", "--name", "host-dashboard", *common,
           "--memory", "64m", "--memory-swap", "64m", "--user", "65534:65534",
           "--publish", f"{args.bind}:{args.port}:8080",
           "--mount", "type=volume,src=host-dashboard-metrics,dst=/data,readonly",
           "--health-cmd", "python -c 'from urllib.request import urlopen; urlopen(\"http://127.0.0.1:8080/health\",timeout=2)'",
           "--health-interval", "10s", "--health-timeout", "3s", "--health-start-period", "10s",
           "--health-retries", "3", image]
    old, started = {}, []
    names = ["host-dashboard-collector", "host-dashboard"]
    # Confirm ownership before replacing only this application's containers.
    for name in names:
        result = remote(["docker", "inspect", "--format", '{{index .Config.Labels "app"}}', name], check=False)
        if not result.returncode:
            if result.stdout.strip() != "host-dashboard":
                raise RuntimeError(f"Container name {name} is already owned by another application")
            old[name] = name + "-before-" + stamp
    try:
        for name, backup in old.items():
            remote(["docker", "stop", name])
            remote(["docker", "rename", name, backup])
        for command, name in zip([collector, web], names):
            remote(command)
            started.append(name)
        url = f"http://{args.bind}:{args.port}"
        deadline = time.monotonic() + 45
        while True:
            try:
                with urlopen(url + "/api/metrics", timeout=3) as response:
                    snapshot = json.load(response)
                assert snapshot["collector"]["denied_process_reads"] == 0
                assert len(snapshot["gpus"]) == len(inventory["gpus"])
                assert all(g["percent"] is not None for g in snapshot["gpus"])
                break
            except Exception:
                if time.monotonic() > deadline:
                    raise RuntimeError("The new deployment did not produce healthy host and GPU metrics")
                time.sleep(2)
    except Exception:
        logs = {name: remote(["docker", "logs", "--tail", "40", name], check=False).stderr for name in started}
        (runtime / ("failure-" + stamp + ".json")).write_text(json.dumps(logs, indent=2) + "\n")
        for name in started:
            remote(["docker", "rm", "-f", name], check=False)
        for name, backup in old.items():
            remote(["docker", "rename", backup, name], check=False)
            remote(["docker", "start", name], check=False)
        raise
    record = {"url": url + "/", "image": image, "previous_containers": old, "timestamp": stamp, "config_file": host_config,
              "source_hashes": {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
                                for p in [ROOT / "collector.py", ROOT / "server.py", ROOT / "Dockerfile", *sorted((ROOT / "static").glob("*"))]}}
    (runtime / "deployment.json").write_text(json.dumps(record, indent=2) + "\n")
    print(json.dumps(record, indent=2))


if __name__ == "__main__":
    main()
