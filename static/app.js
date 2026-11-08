"use strict";

const $ = id => document.getElementById(id);
const finite = value => typeof value === "number" && Number.isFinite(value);
const pct = value => finite(value) ? `${value.toFixed(1)}%` : "—";
const text = (id, value) => { $(id).textContent = value; };
const svgNS = "http://www.w3.org/2000/svg";
let paused = false, lastData = null, fetching = false, lastGoodAt = 0;
let filesystemSignature = "";
const gpuCards = new Map();

function bytes(value) {
  if (!finite(value)) return "—";
  const units = ["B", "KiB", "MiB", "GiB", "TiB"];
  let index = 0;
  while (Math.abs(value) >= 1024 && index < units.length - 1) { value /= 1024; index++; }
  return `${value.toFixed(index === 0 ? 0 : value >= 100 ? 0 : 1)} ${units[index]}`;
}
const speed = value => finite(value) ? `${bytes(value)}/s` : "—";
function uptime(seconds) {
  const days = Math.floor(seconds / 86400), hours = Math.floor(seconds % 86400 / 3600), minutes = Math.floor(seconds % 3600 / 60);
  return [days ? `${days}d` : "", hours ? `${hours}h` : "", `${minutes}m`].filter(Boolean).join(" ");
}
function element(tag, className, value) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (value !== undefined) node.textContent = value;
  return node;
}
function svgElement(tag, attrs) {
  const node = document.createElementNS(svgNS, tag);
  for (const [name, value] of Object.entries(attrs)) node.setAttribute(name, value);
  return node;
}
function chart(svg, history, selectors, maximum = 100) {
  const nodes = [16, 32, 48, 63].map(y => svgElement("line", {x1: 0, x2: 500, y1: y, y2: y, class: "gridline"}));
  if (history.length) {
    const end = history[history.length - 1].time, start = end - 300;
    selectors.forEach((select, series) => {
      let points = [], previousTime = null;
      function flush() {
        if (points.length > 1) nodes.push(svgElement("polyline", {points: points.join(" "), class: `series ${series ? "send-series" : ""}`}));
        points = [];
      }
      for (const sample of history) {
        const value = select(sample);
        if (!finite(value) || sample.time < start) { flush(); previousTime = null; continue; }
        if (previousTime !== null && sample.time - previousTime > 6) flush();
        points.push(`${((sample.time - start) / 300 * 500).toFixed(1)},${(62 - Math.min(1, Math.max(0, value / maximum)) * 60).toFixed(1)}`);
        previousTime = sample.time;
      }
      flush();
    });
  }
  svg.replaceChildren(...nodes);
}
function makeBar(value) {
  const bar = element("div", "bar"), fill = element("span");
  fill.style.width = `${finite(value) ? Math.max(0, Math.min(100, value)) : 0}%`;
  bar.append(fill);
  return bar;
}
function renderGpu(gpu, history) {
  let card = gpuCards.get(gpu.pci);
  if (!card) {
    card = $("gpu-template").content.firstElementChild.cloneNode(true);
    gpuCards.set(gpu.pci, card);
    $("gpus").append(card);
  }
  card.dataset.kind = gpu.kind;
  card.setAttribute("aria-label", `${gpu.kind === "integrated" ? "Integrated" : "Discrete"} GPU: ${gpu.name}`);
  const set = (name, value) => { card.querySelector(`.${name}`).textContent = value; };
  set("gpu-kind", gpu.kind === "integrated" ? "Integrated GPU" : "Discrete GPU");
  set("gpu-name", gpu.name);
  set("gpu-value", pct(gpu.percent));
  set("gpu-sensors", [finite(gpu.temperature_c) ? `${gpu.temperature_c.toFixed(0)} °C` : "", finite(gpu.power_w) ? `${gpu.power_w.toFixed(1)} W` : ""].filter(Boolean).join(" · "));
  const plot = card.querySelector(".gpu-chart");
  plot.setAttribute("aria-label", `${gpu.name} usage over the last five minutes`);
  chart(plot, history, [s => s.gpus[gpu.pci]]);
  const rows = gpu.engines.map(engine => {
    const row = element("div", "engine");
    const label = element("span", "engine-label", engine.label);
    if (engine.capacity > 1) label.append(element("small", "", ` ×${engine.capacity}`));
    row.append(label, makeBar(engine.percent), element("span", "engine-value", pct(engine.percent)));
    row.title = `${engine.label}: ${engine.capacity} ${engine.capacity === 1 ? "engine" : "engines"}, ${pct(engine.percent)} average utilization`;
    return row;
  });
  card.querySelector(".engines").replaceChildren(...rows);
  set("gpu-memory-label", gpu.kind === "integrated" ? "Resident shared RAM" : "Resident client VRAM");
  set("gpu-memory", bytes(gpu.kind === "integrated" ? gpu.system_bytes : gpu.vram_bytes));
  set("gpu-frequency", finite(gpu.frequency_mhz) ? `${gpu.frequency_mhz.toFixed(0)} MHz` : "Unavailable");
  set("gpu-clients", String(gpu.client_count));
  set("gpu-status", gpu.status === "Live" ? (gpu.kind === "integrated" ? "Uses system memory" : `System RAM used by GPU clients: ${bytes(gpu.system_bytes)}`) : gpu.status);
  const processes = gpu.processes.map(process => {
    const li = element("li");
    li.append(element("strong", "", process.name), element("span", "", `PID ${process.pid}`));
    return li;
  });
  if (!processes.length) processes.push(element("li", "", "No open GPU clients"));
  card.querySelector(".gpu-processes").replaceChildren(...processes);
  set("gpu-pci", `${gpu.pci} · ${gpu.driver}`);
}
function filesystemRow(fs) {
  const item = element("div", "filesystem"), row = element("div", "fs-row");
  item.dataset.full = String(fs.percent >= 90);
  row.append(element("strong", "", fs.path), element("span", "", fs.error || `${pct(fs.percent)} · ${bytes(fs.available_bytes)} free`));
  item.append(row);
  if (!fs.error) item.append(makeBar(fs.percent));
  item.title = fs.error || `${fs.path} (${fs.type}): ${bytes(fs.used_bytes)} used, ${bytes(fs.total_bytes)} total`;
  return item;
}
function render(data) {
  const history = data.history || [];
  text("hostname", data.hostname);
  text("host-meta", `Up ${uptime(data.uptime_seconds)} · ${data.cpu.cores.length} CPU cores · Linux ${data.kernel}`);
  text("cpu-value", pct(data.cpu.percent));
  text("cpu-model", data.cpu.model.replaceAll("(R)", "").replaceAll("(TM)", ""));
  text("cpu-temp", finite(data.cpu.temperature_c) ? `${data.cpu.temperature_c.toFixed(0)} °C` : "");
  chart($("cpu-chart"), history, [s => s.cpu]);
  $("cores").replaceChildren(...data.cpu.cores.map((value, i) => {
    const core = element("div", "core"), fill = element("span");
    fill.style.height = `${finite(value) ? value : 0}%`;
    core.title = `Core ${i}: ${pct(value)}`;
    core.setAttribute("aria-label", core.title);
    core.append(fill);
    return core;
  }));
  text("cpu-load", `Per-core usage · Load ${data.cpu.load.map(v => v.toFixed(2)).join(" / ")}`);
  text("memory-value", pct(data.memory.percent));
  text("memory-used", `${bytes(data.memory.used_bytes)} used`);
  text("memory-total", `${bytes(data.memory.total_bytes)} total`);
  text("memory-available", bytes(data.memory.available_bytes));
  text("memory-cache", bytes(data.memory.cache_bytes));
  text("memory-swap", `${bytes(data.memory.swap_used_bytes)} / ${bytes(data.memory.swap_total_bytes)}`);
  chart($("memory-chart"), history, [s => s.memory]);
  for (const gpu of data.gpus) renderGpu(gpu, history);
  for (const [pci, card] of gpuCards) {
    if (!data.gpus.some(g => g.pci === pci)) { card.remove(); gpuCards.delete(pci); }
  }
  if (!data.gpus.length && !$("no-gpus")) { const p = element("p", "card", "No GPU devices detected."); p.id = "no-gpus"; $("gpus").append(p); }
  if (data.gpus.length) $("no-gpus")?.remove();
  text("network-name", data.network.interfaces.filter(i => i.state === "up").map(i => i.name).join(", "));
  text("network-rx", speed(data.network.rx_bytes_s));
  text("network-tx", speed(data.network.tx_bytes_s));
  const maximum = Math.max(1024, ...history.flatMap(s => [s.rx || 0, s.tx || 0]));
  chart($("network-chart"), history, [s => s.rx, s => s.tx], maximum);
  text("network-scale", `Scale ${speed(maximum)}`);
  $("interfaces").replaceChildren(...data.network.interfaces.map(item => {
    const row = element("div", "interface");
    row.append(element("strong", "", item.name), element("span", "", item.state === "up" ? `↓ ${speed(item.rx_bytes_s)} · ↑ ${speed(item.tx_bytes_s)}` : "Down"));
    return row;
  }));
  text("disk-read", speed(data.disk.read_bytes_s));
  text("disk-write", speed(data.disk.write_bytes_s));
  const filesystems = data.disk.filesystems;
  const signature = JSON.stringify(filesystems);
  if (signature !== filesystemSignature) {
  const wasOpen = $("filesystems").querySelector("details")?.open || false;
  const primary = filesystems.filter(f => f.path === "/" || f.type.startsWith("fuse."));
  const other = filesystems.filter(f => !primary.includes(f));
  const rows = primary.map(filesystemRow);
  if (other.length) {
    const details = element("details", "extra-disks");
    details.open = wasOpen;
    details.append(element("summary", "", `${other.length} other filesystems`), ...other.map(filesystemRow));
    rows.push(details);
  }
  if (!filesystems.length) rows.push(element("p", "subtitle", "No filesystems configured"));
  $("filesystems").replaceChildren(...rows);
  filesystemSignature = signature;
  }
  text("updated", `Updated ${new Date(data.timestamp * 1000).toLocaleTimeString()}`);
  $("dashboard").setAttribute("aria-busy", "false");
}
function connection(state, message = "") {
  $("status").className = state;
  text("status", state === "live" ? "Live" : state === "paused" ? "Paused" : "Stale");
  $("error").hidden = !message;
  text("error", message);
  document.body.classList.toggle("stale", state === "stale");
}
async function refresh() {
  if (paused || fetching) return;
  fetching = true;
  try {
    const response = await fetch("/api/metrics", {cache: "no-store", signal: AbortSignal.timeout(5000)});
    const data = await response.json();
    if (paused) return;
    if (!response.ok) throw new Error("Metrics are unavailable or stale");
    // Freshness is checked server-side against the collector's clock.
    lastData = data; lastGoodAt = performance.now();
    render(data);
    connection("live");
  } catch (error) {
    if (!paused) connection("stale", lastData ? "Live updates are unavailable. Showing the last received sample; retrying automatically." : "Waiting for host metrics. Retrying automatically.");
  } finally { fetching = false; }
}
$("pause").addEventListener("click", () => {
  paused = !paused;
  text("pause", paused ? "Resume" : "Pause");
  $("pause").setAttribute("aria-pressed", String(paused));
  if (paused) connection("paused");
  else { connection("stale"); refresh(); }
});
document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh(); });
setInterval(() => {
  if (!paused && lastGoodAt && performance.now() - lastGoodAt > 10000) connection("stale", "Live updates are delayed. Showing the last received sample.");
  if (!document.hidden) refresh();
}, 2000);
refresh();
