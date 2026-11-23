"""Offline tests: python3 -m unittest discover -s tests"""
import gzip
import json
import os
import sys
import shutil
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import archive  # noqa: E402

T0 = 1_795_000_020.0  # a fixed UTC time


def ev(eid, desc="Lane closed", county="Onondaga"):
    return {"event-id": eid, "counties": [county], "headline": {"category": "roadwork", "code": "lane closure"},
            "description": desc, "geometry": {"type": "Point", "coordinates": [-76.1412345, 43.02]},
            "details": [{"locations": [{"route-designator": "I-81", "link-direction": "NORTHBOUND"}],
                         "start-time": {"time": 1_794_990_000_000, "utcOffset": -18_000_000}}]}


def go(t, events, err=""):
    return archive.run({"run_id": str(int(t)), "scheduled_at": "", "started_at": archive.iso(t)}, events, err)


class ArchiveTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        archive.ROOT = Path(self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_tables(self):
        s = go(T0, [ev("A"), ev("B", county="Erie")])
        self.assertEqual((s["onondaga"], s["new"], s["data_changed"]), (1, 1, True))
        self.assertFalse(go(T0 + 1800, [ev("A")])["data_changed"])          # nothing changed: no write
        s = go(T0 + 3600, [ev("A", desc="Two lanes closed")])
        self.assertEqual(s["changed_fields"], 1)
        runs = archive.read_all("runs")
        self.assertEqual(len(runs), 3)                                          # pending run row flushed
        ch = [(c["field"], c["old"], c["new"]) for c in archive.read_all("changes")]
        self.assertEqual(ch, [("_present", "", "1"), ("description", "Lane closed", "Two lanes closed")])
        e = archive.read_all("events")
        self.assertEqual((e[0]["description"], e[0]["first_seen"], e[0]["last_seen"]),
                         ("Two lanes closed", archive.iso(T0), archive.iso(T0 + 3600)))
        self.assertEqual(e[0]["geometry"], '{"type":"Point","coordinates":[-76.14123,43.02]}')
        go(T0 + 5400, [])
        go(T0 + 7200, [ev("A", desc="Two lanes closed")])
        self.assertEqual([c["new"] for c in archive.read_all("changes") if c["field"] == "_present"], ["1", "0", "1"])

    def test_failed_run_and_gap(self):
        go(T0, [ev("A")])
        s = go(T0 + 1800, None, "URLError")
        self.assertTrue(s["data_changed"])
        self.assertEqual(archive.read_all("runs")[-1]["status"], "failed")
        s = go(T0 + 3 * 3600, [ev("A")])
        self.assertTrue(s["gap"])
        g = archive.read_csv(archive.ROOT / "data" / "gaps.csv")
        self.assertEqual((g[0]["no_data_from"], g[0]["minutes"]), (archive.iso(T0), "180"))

    def test_active_at(self):
        go(T0, [ev("A")])
        go(T0 + 1800, [ev("A", desc="changed"), ev("B")])
        go(T0 + 3600, [ev("B")])
        a = archive.active_at(T0 + 60)
        self.assertEqual([(x["event_id"], x["description"]) for x in a["events"]], [("A", "Lane closed")])
        self.assertEqual(a["coverage"], "ok")
        a = archive.active_at(archive.iso(T0 + 1900))
        self.assertEqual(sorted((x["event_id"], x["description"]) for x in a["events"]),
                         [("A", "changed"), ("B", "Lane closed")])
        self.assertEqual([x["event_id"] for x in archive.active_at(T0 + 3700)["events"]], ["B"])
        self.assertEqual(archive.active_at(T0 - 10)["coverage"], "before_archive")
        self.assertEqual(archive.active_at(T0 + 5 * 3600)["coverage"], "gap")

    def test_raw_daily(self):
        go(T0, [ev("A")])
        go(T0 + 1800, [ev("A", desc="changed")])
        day = archive.iso(T0)[:10]
        base = archive.ROOT / "data" / "raw" / day[:7]
        with gzip.open(base / f"{day}.json.gz", "rt") as f:
            self.assertEqual(len(json.load(f)["events"]), 1)
        per_run = sorted((base / day).glob("*.json.gz"))
        descs = []
        for p in per_run:
            with gzip.open(p, "rt") as f:
                descs += [e["description"] for e in json.load(f)["events"]]
        self.assertEqual(descs, ["Lane closed", "changed"])

    def test_skips_inactive_road_conditions(self):
        e = ev("W"); e["headline"] = {"category": "nysdot-road-conditions", "code": "no active reporting"}
        self.assertIsNone(archive.record(e))

    def test_cache_lost_falls_back_to_committed_state(self):
        go(T0, [ev("A")])
        shutil.rmtree(archive.ROOT / ".state-cache")
        s = go(T0 + 1800, [ev("A")])
        self.assertEqual((s["new"], s["data_changed"]), (0, False))

    def test_scheduled_slot(self):
        self.assertEqual(archive.scheduled_slot(archive.parse_iso("2026-11-23T10:15:30Z")), "2026-11-23T10:07:00Z")
        self.assertEqual(archive.scheduled_slot(archive.parse_iso("2026-11-23T10:05:00Z")), "2026-11-23T09:37:00Z")

    def test_no_credentials_never_fetches(self):
        env = {k: os.environ.pop(k, None) for k in ("NY511_USERNAME", "NY511_PASSWORD")}
        try:
            with self.assertRaises(RuntimeError):
                archive.fetch()
        finally:
            for k, v in env.items():
                if v is not None:
                    os.environ[k] = v


if __name__ == "__main__":
    unittest.main()
