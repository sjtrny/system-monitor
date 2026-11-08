import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import urlopen

import collector
import server


I915 = """drm-driver: i915
drm-client-id: 4
drm-pdev: 0000:00:02.0
drm-engine-video: 900000000 ns
drm-engine-capacity-video: 2
drm-resident-system0: 4096 KiB
"""
XE = """drm-driver: xe
drm-client-id: 4
drm-pdev: 0000:04:00.0
drm-cycles-vcs: 1000
drm-total-cycles-vcs: 10000
drm-engine-capacity-vcs: 2
drm-resident-vram0: 2 MiB
drm-resident-gtt: 16 KiB
drm-total-vram0: 100 MiB
"""


class CounterTests(unittest.TestCase):
    def test_gpu_discovery_uses_pci_identity_not_unstable_card_number(self):
        with tempfile.TemporaryDirectory() as directory:
            sys = Path(directory) / "sys"
            for card, pci, driver in [(2, "0000:00:02.0", "i915"), (0, "0000:04:00.0", "xe")]:
                device = sys / f"class/drm/card{card}/device"
                device.mkdir(parents=True)
                (device / "uevent").write_text(f"DRIVER={driver}\nPCI_SLOT_NAME={pci}\n")
                (device / "driver").symlink_to("/drivers/" + driver)
            (sys / "class/drm/card0-HDMI-1").mkdir()
            monitor = collector.Collector({"gpus": {"0000:00:02.0": {"name": "Integrated", "kind": "integrated"},
                                                   "0000:04:00.0": {"name": "Arc", "kind": "discrete"}}},
                                          proc=directory, sys=sys)
            self.assertEqual([(g["pci"], g["name"]) for g in monitor.gpu_devices],
                             [("0000:00:02.0", "Integrated"), ("0000:04:00.0", "Arc")])

    def test_cpu_excludes_guest_time_already_in_user_time(self):
        previous = [0] * 10
        current = [30, 0, 10, 60, 0, 0, 0, 0, 20, 0]
        self.assertEqual(collector.cpu_usage(current, previous), 40)

    def test_cpu_iowait_is_idle_and_first_sample_is_unknown(self):
        self.assertEqual(collector.cpu_usage([20, 0, 0, 60, 20, 0, 0, 0], [0] * 8), 20)
        self.assertIsNone(collector.cpu_usage([20] * 8, None))

    def test_i915_video_capacity_is_not_double_counted(self):
        before = {"busy": 0, "total": None, "capacity": 2}
        current = collector.parse_client(I915)["engines"]["video"]
        self.assertEqual(collector.engine_usage(current, before, 1), 45)

    def test_xe_uses_gpu_clock_and_engine_capacity(self):
        before = collector.parse_client(XE)["engines"]["video"]
        current = {"busy": 2000, "total": 12000, "capacity": 2}
        # CPU elapsed time must not change the Xe clock-domain calculation.
        self.assertEqual(collector.engine_usage(current, before, 99), 25)

    def test_counter_regression_preserves_high_water_mark(self):
        before = {"busy": 1000, "total": None, "capacity": 1}
        lower = {"busy": 500, "total": None, "capacity": 1}
        self.assertEqual(collector.engine_usage(lower, before, 1), 0)
        self.assertEqual(lower["busy"], 1000)
        recovered = {"busy": 1100, "total": None, "capacity": 1}
        self.assertAlmostEqual(collector.engine_usage(recovered, lower, 1), .00001)

    def test_missing_baseline_and_stopped_gpu_clock_are_unknown(self):
        value = {"busy": 3, "total": 100, "capacity": 1}
        self.assertIsNone(collector.engine_usage(value, None, 2))
        self.assertIsNone(collector.engine_usage(value.copy(), value, 2))

    def test_resident_memory_excludes_unbacked_allocations(self):
        client = collector.parse_client(XE)
        self.assertEqual(client["vram_bytes"], 2 * 1024**2)
        self.assertEqual(client["system_bytes"], 16 * 1024)
        self.assertEqual(client["resident_bytes"], 2 * 1024**2 + 16 * 1024)

    def test_absent_memory_counters_are_not_zero(self):
        client = collector.parse_client("drm-pdev: 0000:00:02.0\ndrm-client-id: 3\n")
        self.assertIsNone(client["resident_bytes"])

    def test_reset_network_counters_are_not_negative_traffic(self):
        self.assertIsNone(collector.rate(1, 2000, 2))
        self.assertIsNone(collector.rate(1, None, 2))
        self.assertEqual(collector.rate(2000, 1000, 2), 500)

    def test_gpu_clients_deduplicated_by_pci_and_client_id(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for pid, raw in [(123, I915), (124, I915), (125, XE)]:
                process = root / str(pid)
                (process / "fd").mkdir(parents=True)
                (process / "fdinfo").mkdir()
                (process / "comm").write_text("ffmpeg")
                for fd in (7, 8):
                    (process / "fd" / str(fd)).symlink_to("/dev/dri/renderD128")
                    (process / "fdinfo" / str(fd)).write_text(raw)
            clients, denied = collector.scan_clients(root)
            self.assertEqual(denied, 0)
            self.assertEqual(len(clients), 2)
            self.assertEqual(len(clients[("0000:00:02.0", "4")]["owners"]), 2)
            self.assertEqual(sum(c["resident_bytes"] for c in clients.values()), 6 * 1024**2 + 16 * 1024)

    def test_unreadable_processes_invalidate_gpu_usage(self):
        monitor = collector.Collector.__new__(collector.Collector)
        monitor.proc = Path("/unused")
        monitor.gpu_devices = [{"pci": "0000:00:02.0", "name": "Intel", "kind": "integrated",
                                "driver": "i915", "capacities": {"video": 2}}]
        monitor.previous_clients = {}
        with patch("collector.scan_clients", return_value=({}, 1)), patch.object(monitor, "gpu_sensors", return_value={}):
            gpus, denied = monitor.gpus(2)
        self.assertEqual(denied, 1)
        self.assertIsNone(gpus[0]["percent"])
        self.assertIsNone(gpus[0]["resident_bytes"])

    def test_idle_gpu_with_no_clients_reports_zero(self):
        monitor = collector.Collector.__new__(collector.Collector)
        monitor.proc = Path("/unused")
        monitor.gpu_devices = [{"pci": "0000:00:02.0", "name": "Intel", "kind": "integrated",
                                "driver": "i915", "capacities": {"video": 2}}]
        monitor.previous_clients = {}
        with patch("collector.scan_clients", return_value=({}, 0)), patch.object(monitor, "gpu_sensors", return_value={}):
            gpus, _ = monitor.gpus(2)
        self.assertEqual(gpus[0]["percent"], 0)


class HttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        cls.metrics = Path(cls.directory.name) / "metrics.json"
        cls.override = patch.object(server, "METRICS", cls.metrics)
        cls.override.start()
        cls.http = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.thread = threading.Thread(target=cls.http.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.http.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.http.shutdown()
        cls.http.server_close()
        cls.thread.join()
        cls.override.stop()
        cls.directory.cleanup()

    def test_live_snapshot_and_health(self):
        self.metrics.write_text(json.dumps({"timestamp": time.time(), "cpu": {"percent": 22}}))
        with urlopen(self.base + "/api/metrics") as response:
            self.assertEqual(json.load(response)["cpu"]["percent"], 22)
            self.assertEqual(response.headers["Cache-Control"], "no-store")
        with urlopen(self.base + "/health") as response:
            self.assertEqual(json.load(response)["status"], "ok")

    def test_stale_snapshot_returns_503_not_apparent_live_data(self):
        self.metrics.write_text(json.dumps({"timestamp": time.time() - 20}))
        for path in ("/api/metrics", "/health"):
            with self.assertRaises(HTTPError) as error:
                urlopen(self.base + path)
            self.assertEqual(error.exception.code, 503)

    def test_invalid_snapshot_is_unavailable(self):
        self.metrics.write_text("{")
        with self.assertRaises(HTTPError) as error:
            urlopen(self.base + "/api/metrics")
        self.assertEqual(error.exception.code, 503)

    def test_static_paths_cannot_read_collector_or_host_files(self):
        for path in ("/../collector.py", "/%2e%2e/etc/passwd", "/config/config.json"):
            with self.assertRaises(HTTPError) as error:
                urlopen(self.base + path)
            self.assertEqual(error.exception.code, 404)


if __name__ == "__main__":
    unittest.main()
