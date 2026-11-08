"""Read Linux host counters and publish a bounded, atomic JSON snapshot."""

import argparse
from collections import deque
import json
import logging
import os
from pathlib import Path
import re
import signal
import threading
import time


ENGINE_NAMES = {"rcs": "render", "ccs": "compute", "bcs": "copy",
                "vcs": "video", "vecs": "video-enhance"}
ENGINE_LABELS = {"render": "Render", "compute": "Compute", "copy": "Copy",
                 "video": "Video", "video-enhance": "Video enhance"}
ENGINE_ORDER = ("render", "compute", "video", "video-enhance", "copy")


def read(path, default=""):
    try:
        return Path(path).read_text().strip()
    except OSError:
        return default


def number(path):
    try:
        return float(Path(path).read_text().strip())
    except (OSError, ValueError):
        return None


def fields(text):
    return {k.strip(): v.strip() for line in text.splitlines()
            if ":" in line for k, v in [line.split(":", 1)]}


def byte_value(value):
    parts = value.split()
    return int(parts[0]) * {"KiB": 1024, "kB": 1024, "MiB": 1024**2,
                           "GiB": 1024**3}.get(parts[1] if len(parts) > 1 else "", 1)


def percent(value):
    return round(max(0, min(100, value)), 2)


def cpu_usage(current, previous):
    if previous is None:
        return None
    # Guest times are already included in user and nice.
    total = sum(current[:8]) - sum(previous[:8])
    idle = sum(current[3:5]) - sum(previous[3:5])
    return percent(100 * (total - idle) / total) if total > 0 and idle >= 0 else None


def rate(current, previous, seconds):
    if previous is None or seconds <= 0 or current < previous:
        return None
    return round((current - previous) / seconds, 2)


def parse_client(text):
    data = fields(text)
    if not data.get("drm-client-id") or not data.get("drm-pdev"):
        return None
    engines = {}
    for key, value in data.items():
        if key.startswith("drm-engine-") and not key.startswith("drm-engine-capacity-"):
            raw_name = key.removeprefix("drm-engine-")
            engines[ENGINE_NAMES.get(raw_name, raw_name)] = {
                "busy": int(value.split()[0]), "total": None,
                "capacity": max(1, int(data.get("drm-engine-capacity-" + raw_name, "1"))),
            }
        elif key.startswith("drm-cycles-"):
            raw_name = key.removeprefix("drm-cycles-")
            total = data.get("drm-total-cycles-" + raw_name)
            if total is not None:
                engines[ENGINE_NAMES.get(raw_name, raw_name)] = {
                    "busy": int(value), "total": int(total),
                    "capacity": max(1, int(data.get("drm-engine-capacity-" + raw_name, "1"))),
                }
    resident = {k.removeprefix("drm-resident-"): byte_value(v)
                for k, v in data.items() if k.startswith("drm-resident-")}
    return {
        "pci": data["drm-pdev"], "id": data["drm-client-id"],
        "driver": data.get("drm-driver"), "engines": engines,
        "resident_bytes": sum(resident.values()) if resident else None,
        "vram_bytes": sum(v for k, v in resident.items() if k.startswith("vram")) if resident else None,
        "system_bytes": sum(v for k, v in resident.items() if not k.startswith("vram")) if resident else None,
    }


def engine_usage(current, previous, seconds):
    if previous is None or seconds <= 0:
        return None
    # DRM counters may briefly regress. Retain their high-water mark, per ABI.
    current["busy"] = max(current["busy"], previous["busy"])
    busy = current["busy"] - previous["busy"]
    if current["total"] is None:
        elapsed = seconds * 1e9
    elif previous["total"] is not None:
        elapsed = current["total"] - previous["total"]
    else:
        return None
    return 100 * busy / elapsed / current["capacity"] if elapsed > 0 else None


def scan_clients(proc):
    clients, denied = {}, 0
    for process in proc.iterdir():
        if not process.name.isdigit():
            continue
        try:
            for fd in (process / "fd").iterdir():
                try:
                    # Only read fdinfo for DRM handles, not every file on the host.
                    if not os.readlink(fd).startswith("/dev/dri/"):
                        continue
                    client = parse_client((process / "fdinfo" / fd.name).read_text())
                    if client is None:
                        continue
                    key = (client["pci"], client["id"])
                    owner = {"pid": int(process.name), "name": read(process / "comm", "Unknown")}
                    if key in clients:
                        if owner not in clients[key]["owners"]:
                            clients[key]["owners"].append(owner)
                    else:
                        client.update(owners=[owner], sampled=time.monotonic())
                        clients[key] = client
                except (FileNotFoundError, ProcessLookupError):
                    pass  # Processes and file descriptors can disappear during a read.
                except PermissionError:
                    denied += 1
        except (FileNotFoundError, ProcessLookupError):
            pass
        except PermissionError:
            denied += 1
    return clients, denied


class Collector:
    def __init__(self, config, proc="/host/proc", sys="/host/sys"):
        self.config, self.proc, self.sys = config, Path(proc), Path(sys)
        self.previous_cpu, self.previous_net, self.previous_disk = {}, {}, {}
        self.previous_clients, self.previous_energy = {}, {}
        self.previous_time = None
        self.history = deque(maxlen=150)  # Five minutes at the default two seconds.
        self.storage, self.storage_time = [], 0
        self.gpu_devices = self.discover_gpus()
        missing = set(config.get("gpus", {})) - {gpu["pci"] for gpu in self.gpu_devices}
        if missing:
            raise RuntimeError("Configured GPUs were not found: " + ", ".join(sorted(missing)))
        info = fields(read(self.proc / "cpuinfo"))
        self.cpu_model = info.get("model name", "CPU")
        self.hostname = config.get("hostname", "Host")
        self.kernel = read(self.proc / "sys/kernel/osrelease")
        self.cpu_hwmon = next((p for p in (self.sys / "class/hwmon").glob("hwmon*")
                               if read(p / "name") in ("coretemp", "k10temp")), None)

    def discover_gpus(self):
        devices = []
        for card in sorted((self.sys / "class/drm").glob("card[0-9]*")):
            if not re.fullmatch(r"card\d+", card.name):
                continue
            device = card / "device"
            identity = dict(line.split("=", 1) for line in read(device / "uevent").splitlines() if "=" in line)
            pci = identity.get("PCI_SLOT_NAME")
            if not pci:
                continue
            driver = (device / "driver").resolve().name
            configured = self.config.get("gpus", {}).get(pci, {})
            kind = configured.get("kind", "integrated" if driver == "i915" else "discrete")
            capacities = {}
            paths = list((card / "engine").glob("*"))
            paths += list(device.glob("tile*/gt*/engines/*"))
            for path in paths:
                raw_name = re.sub(r"\d+$", "", path.name)
                name = ENGINE_NAMES.get(raw_name, raw_name)
                if name in ENGINE_LABELS:
                    capacities[name] = capacities.get(name, 0) + 1
            devices.append({"pci": pci, "name": configured.get("name", f"{driver} GPU"),
                            "kind": kind, "driver": driver, "card": card,
                            "capacities": capacities, "device": device,
                            "hwmon": next(device.glob("hwmon/hwmon*"), None)})
        return sorted(devices, key=lambda d: (d["kind"] != "integrated", d["pci"]))

    def cpu(self):
        counters = {line.split()[0]: list(map(int, line.split()[1:]))
                    for line in read(self.proc / "stat").splitlines() if re.match(r"cpu(?:\d+)? ", line)}
        if "cpu" not in counters:
            raise RuntimeError("Host CPU counters unavailable")
        usage = {name: cpu_usage(values, self.previous_cpu.get(name)) for name, values in counters.items()}
        self.previous_cpu = counters
        temp = None
        if self.cpu_hwmon:
            temp = number(self.cpu_hwmon / "temp1_input")
        loads = read(self.proc / "loadavg").split()
        return {"model": self.cpu_model, "percent": usage["cpu"],
                "cores": [usage[k] for k in sorted(usage, key=lambda k: int(k[3:] or -1)) if k != "cpu"],
                "temperature_c": temp / 1000 if temp is not None else None,
                "load": [float(x) for x in loads[:3]]}

    def memory(self):
        mem = {k: byte_value(v) for k, v in fields(read(self.proc / "meminfo")).items()}
        total, available = mem["MemTotal"], mem["MemAvailable"]
        return {"total_bytes": total, "available_bytes": available, "used_bytes": total - available,
                "percent": percent(100 * (total - available) / total),
                "cache_bytes": mem.get("Cached", 0) + mem.get("SReclaimable", 0),
                "swap_total_bytes": mem["SwapTotal"], "swap_used_bytes": mem["SwapTotal"] - mem["SwapFree"]}

    def network(self, elapsed):
        interfaces, counters = [], {}
        for line in read(self.proc / "1/net/dev").splitlines():
            if ":" not in line:
                continue
            name, values = line.split(":", 1)
            name, values = name.strip(), values.split()
            device = self.sys / "class/net" / name
            if not (device / "device").exists():
                continue
            rx, tx = int(values[0]), int(values[8])
            before = self.previous_net.get(name, (None, None))
            counters[name] = rx, tx
            interfaces.append({"name": name, "state": read(device / "operstate"),
                               "rx_bytes_s": rate(rx, before[0], elapsed),
                               "tx_bytes_s": rate(tx, before[1], elapsed)})
        self.previous_net = counters
        active = [i for i in interfaces if i["state"] == "up"]
        def total(field):
            return sum(i[field] for i in active) if active and all(i[field] is not None for i in active) else None
        return {"interfaces": interfaces, "rx_bytes_s": total("rx_bytes_s"), "tx_bytes_s": total("tx_bytes_s")}

    def disks(self, elapsed, now):
        disks, counters = [], {}
        for line in read(self.proc / "diskstats").splitlines():
            values = line.split()
            if len(values) < 14:
                continue
            name = values[2]
            device = self.sys / "class/block" / name
            if (device / "partition").exists() or not (device / "device").exists():
                continue
            current = int(values[5]) * 512, int(values[9]) * 512
            previous = self.previous_disk.get(name, (None, None))
            counters[name] = current
            disks.append({"name": name, "read_bytes_s": rate(current[0], previous[0], elapsed),
                          "write_bytes_s": rate(current[1], previous[1], elapsed)})
        self.previous_disk = counters
        if now - self.storage_time >= 60:
            self.storage = []
            for target in self.config.get("filesystems", []):
                item = {k: target[k] for k in ("path", "type")}
                try:
                    stat = os.statvfs(target["mount"])
                    total, free, available = stat.f_blocks * stat.f_frsize, stat.f_bfree * stat.f_frsize, stat.f_bavail * stat.f_frsize
                    used = total - free
                    item.update(total_bytes=total, used_bytes=used, available_bytes=available,
                                percent=percent(100 * used / (used + available)) if used + available else 0)
                except OSError:
                    item["error"] = "Filesystem unavailable"
                self.storage.append(item)
            self.storage_time = now
        def total(field):
            return sum(d[field] for d in disks) if disks and all(d[field] is not None for d in disks) else None
        return {"read_bytes_s": total("read_bytes_s"), "write_bytes_s": total("write_bytes_s"),
                "devices": disks, "filesystems": self.storage}

    def gpu_sensors(self, device, elapsed):
        hwmon = device["hwmon"]
        temperature, watts = None, None
        if hwmon:
            temps = [(read(p.with_name(p.name.replace("_input", "_label"))), number(p))
                     for p in hwmon.glob("temp*_input")]
            selected = next((v for label, v in temps if label == "pkg"), None)
            if selected is not None:
                temperature = selected / 1000
            energies = [(read(p.with_name(p.name.replace("_input", "_label"))), p)
                        for p in hwmon.glob("energy*_input")]
            path = next((p for label, p in energies if label == "card"), None)
            if path:
                current = number(path)
                if current is not None:
                    raw_rate = rate(current, self.previous_energy.get(device["pci"]), elapsed)
                    watts = round(raw_rate / 1e6, 1) if raw_rate is not None else None
                    self.previous_energy[device["pci"]] = current
        if device["driver"] == "i915":
            paths = list(device["card"].glob("gt/gt*/rps_act_freq_mhz")) or [device["card"] / "gt_act_freq_mhz"]
        else:
            paths = list(device["device"].glob("tile*/gt*/freq*/act_freq"))
        frequencies = [v for p in paths if (v := number(p)) is not None]
        return {"temperature_c": temperature, "power_w": watts,
                "frequency_mhz": max(frequencies) if frequencies else None}

    def gpus(self, elapsed):
        clients, denied = scan_clients(self.proc)
        result = []
        for device in self.gpu_devices:
            relevant = {k: c for k, c in clients.items() if c["pci"] == device["pci"]}
            capacities = dict(device["capacities"])
            for client in relevant.values():
                capacities.update({k: e["capacity"] for k, e in client["engines"].items()})
            values = {name: 0.0 for name in capacities}
            incomplete = set()
            for key, client in relevant.items():
                before = self.previous_clients.get(key)
                for name, engine in client["engines"].items():
                    prior = before["engines"].get(name) if before else None
                    delta = engine_usage(engine, prior, client["sampled"] - before["sampled"] if before else 0)
                    if delta is None:
                        incomplete.add(name)
                    else:
                        values[name] += delta
                incomplete.update(set(capacities) - set(client["engines"]))
            supported = device["driver"] in ("i915", "xe")
            engines = [{"name": name, "label": ENGINE_LABELS.get(name, name), "capacity": capacities[name],
                        "percent": percent(value) if supported and not denied and name not in incomplete else None}
                       for name, value in sorted(values.items(), key=lambda item: (ENGINE_ORDER.index(item[0]) if item[0] in ENGINE_ORDER else 99, item[0]))]
            ready = bool(engines) and all(e["percent"] is not None for e in engines)
            summary = max(e["percent"] for e in engines) if ready else None
            if denied:
                status = "Some host processes could not be read"
            elif not supported:
                status = "Usage counters unavailable for this driver"
            elif not ready:
                status = "Sampling GPU counters"
            else:
                status = "Live"
            def memory_total(field):
                return sum(c[field] for c in relevant.values()) if not denied and all(c[field] is not None for c in relevant.values()) else None
            owners = {(o["pid"], o["name"]) for c in relevant.values() for o in c["owners"]}
            result.append({k: device[k] for k in ("pci", "name", "kind", "driver")} | {
                "percent": summary, "status": status, "engines": engines,
                "resident_bytes": memory_total("resident_bytes"), "vram_bytes": memory_total("vram_bytes"),
                "system_bytes": memory_total("system_bytes"), "client_count": len(relevant),
                "processes": [{"pid": pid, "name": name} for pid, name in sorted(owners)],
                **self.gpu_sensors(device, elapsed)})
        self.previous_clients = clients
        return result, denied

    def sample(self):
        started = time.monotonic()
        elapsed = started - self.previous_time if self.previous_time is not None else 0
        cpu, memory = self.cpu(), self.memory()
        network, disk = self.network(elapsed), self.disks(elapsed, started)
        gpus, denied = self.gpus(elapsed)
        timestamp = time.time()
        self.history.append({"time": timestamp, "cpu": cpu["percent"], "memory": memory["percent"],
                             "gpus": {g["pci"]: g["percent"] for g in gpus},
                             "rx": network["rx_bytes_s"], "tx": network["tx_bytes_s"]})
        self.previous_time = started
        return {"timestamp": timestamp, "hostname": self.hostname, "kernel": self.kernel,
                "uptime_seconds": float(read(self.proc / "uptime").split()[0]),
                "interval_seconds": 2, "cpu": cpu, "memory": memory, "gpus": gpus,
                "network": network, "disk": disk, "history": list(self.history),
                "collector": {"duration_ms": round((time.monotonic() - started) * 1000, 1),
                              "denied_process_reads": denied}}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="/config/config.json")
    parser.add_argument("--output", default="/data/metrics.json")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    collector = Collector(json.loads(Path(args.config).read_text()))
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    while not stop.is_set():
        start = time.monotonic()
        try:
            data = collector.sample()
            temporary = output.with_suffix(".tmp")
            temporary.write_text(json.dumps(data, separators=(",", ":"), allow_nan=False))
            temporary.chmod(0o644)
            temporary.replace(output)
        except Exception:
            # Keep the last complete sample; the web server marks it stale.
            logging.exception("Collection failed")
        stop.wait(max(0.1, 2 - (time.monotonic() - start)))


if __name__ == "__main__":
    main()
