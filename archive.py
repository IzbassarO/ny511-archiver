#!/usr/bin/env python3
"""Archive 511NY (NYSDOT CARS) traffic events for Onondaga County. Standard library only.

One run = one fetch of the statewide /events feed (scheduled every 15 min, at :07 :22 :37 :52, by GitHub Actions).

Tables (CSV, one file per month, under data/):
  runs/YYYY-MM.csv     run_id, scheduled_at, started_at, status (ok | failed), n_events, error
  events/YYYY-MM.csv   one row per event_id (month of first_seen): the latest 511NY fields + first_seen, last_seen
  changes/YYYY-MM.csv  event_id, seen_at, field, old, new — every change of a tracked field, plus the pseudo-field
                       `_present` ("" -> "1" first appearance, "1" -> "0" gone from the feed, "0" -> "1" back again)
  gaps.csv             no_data_from, no_data_to, minutes — successful runs more than GAP_SECONDS (25 min) apart
  raw/YYYY-MM/YYYY-MM-DD.json.gz        the day's first full Onondaga snapshot (written once)
  raw/YYYY-MM/YYYY-MM-DD/HHMMSS.json.gz the raw record of every event that appeared or changed in that run
Winter road-condition segments with "no active reporting" are not events and are skipped.

Times are UTC ("...Z"). Resolution is the run spacing: an event first seen at seen_at appeared between the previous
successful run and seen_at. Commits happen only when data changed (new/changed/gone events, a failed run, a gap,
or a new day); run rows and last_seen wait in state (Actions cache) until then.

Credentials: NY511_USERNAME / NY511_PASSWORD from the environment (GitHub secrets), used only in the Basic
Authorization header; never printed, logged or written.
"""
from __future__ import annotations

import base64
import csv
import datetime as dt
import gzip
import json
import os
import sys
import time
import urllib.request
from pathlib import Path

csv.field_size_limit(sys.maxsize)  # some 511NY geometries exceed the 128 KB default

FEED = os.environ.get("NY511_EVENTS_URL", "https://nysdot.carsprogram.org/carsapi_v1/api/events")
COUNTY = "Onondaga"
ROOT = Path(os.environ.get("ARCHIVE_ROOT") or Path(__file__).resolve().parent)
GAP_S = int(os.environ.get("GAP_SECONDS", str(25 * 60)))
CRON_MINUTES = (7, 22, 37, 52)
# A trigger within this long after the last successful run is a duplicate (cron-job.org + GitHub schedule) and is skipped.
MIN_INTERVAL_S = int(os.environ.get("MIN_INTERVAL_SECONDS", str(10 * 60)))
TIMEOUT_S = 40
TRACKED = ["category", "type", "road", "direction", "description", "lanes", "start", "end", "geometry"]
EVENT_FIELDS = ["event_id", *TRACKED, "updated", "first_seen", "last_seen"]
RUN_FIELDS = ["run_id", "scheduled_at", "started_at", "status", "n_events", "error"]
CHANGE_FIELDS = ["event_id", "seen_at", "field", "old", "new"]
GAP_FIELDS = ["no_data_from", "no_data_to", "minutes"]


def iso(t: float) -> str:
    return dt.datetime.fromtimestamp(t, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(s: str) -> float:
    return dt.datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc).timestamp()


def data() -> Path:
    return ROOT / "data"


def scheduled_slot(t: float) -> str:
    """Latest cron slot (minute 7, 22, 37 or 52) at or before t."""
    d = dt.datetime.fromtimestamp(t, dt.timezone.utc).replace(second=0, microsecond=0)
    for back in range(0, 61):
        c = d - dt.timedelta(minutes=back)
        if c.minute in CRON_MINUTES:
            return c.strftime("%Y-%m-%dT%H:%M:%SZ")
    return ""


# ------------------------------------------------------------------ transform (pure)
def _time(o) -> str:
    if not isinstance(o, dict) or o.get("time") in (None, ""):
        return ""
    off = dt.timezone(dt.timedelta(milliseconds=int(o.get("utcOffset") or 0)))
    return dt.datetime.fromtimestamp(int(o["time"]) / 1000, off).isoformat(timespec="minutes")


def _lanes(lanes) -> str:
    out = []
    for ln in lanes or []:
        a, t = ln.get("total-affected"), ln.get("total-original")
        n = f"{a} of {t}" if a is not None and t is not None else (str(a) if a is not None else "")
        bits = [n, (ln.get("lanes-type") or "").replace("_", " ").lower(),
                (ln.get("lanes-impact-type") or "").replace("_", " ").lower()]
        out.append(" ".join(b for b in bits if b))
    return "; ".join(x for x in out if x)


def _round(c):
    return round(c, 5) if isinstance(c, (int, float)) else [_round(x) for x in c]


def record(e: dict) -> dict | None:
    """Normalized 511NY fields of one raw CARS event, or None when it is not in Onondaga County."""
    if not isinstance(e, dict) or COUNTY not in (e.get("counties") or []) or e.get("event-id") in (None, ""):
        return None
    head = e.get("headline") or {}
    if head.get("category") == "nysdot-road-conditions" and head.get("code") == "no active reporting":
        return None  # winter road segment with nothing to report (not an event); large geometry
    d = (e.get("details") or [{}])[0] or {}
    loc = (d.get("locations") or [{}])[0] or {}
    geom = e.get("geometry")
    if not geom:
        p = (loc.get("primary-location") or {}).get("geo-location") or loc.get("geo-location") or {}
        geom = {"type": "Point", "coordinates": [float(p["longitude"]), float(p["latitude"])]} if p else None
    if geom and "coordinates" in geom:
        geom = {"type": geom.get("type"), "coordinates": _round(geom["coordinates"])}
    return {
        "event_id": str(e["event-id"]),
        "category": head.get("category") or "",
        "type": head.get("code") or "",
        "road": loc.get("route-designator") or loc.get("landmark-name") or "",
        "direction": (loc.get("link-direction") or "").replace("_", " ").lower(),
        "description": e.get("description") or "",
        "lanes": _lanes(d.get("lanes")),
        "start": _time(d.get("start-time")),
        "end": _time(d.get("end-time")),
        "geometry": json.dumps(geom, separators=(",", ":")) if geom else "",
        "updated": _time(e.get("update-time")),
    }


# ------------------------------------------------------------------ io helpers
def fetch() -> list:
    user = os.environ.get("NY511_USERNAME", "").strip()
    pw = os.environ.get("NY511_PASSWORD", "").strip()
    if not (user and pw):
        raise RuntimeError("credentials not configured")
    tok = base64.b64encode(f"{user}:{pw}".encode()).decode()
    req = urllib.request.Request(FEED, headers={"Authorization": f"Basic {tok}", "Accept": "application/json",
                                                "User-Agent": "ny511-archive"})
    with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
        events = json.load(r)
    if not isinstance(events, list):
        raise ValueError("unexpected response shape")
    return events


def read_csv(p: Path) -> list[dict]:
    if not p.exists():
        return []
    with open(p, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def read_all(table: str) -> list[dict]:
    out = []
    for p in sorted((data() / table).glob("*.csv")):
        out += read_csv(p)
    return out


def write_csv(p: Path, rows: list[dict], fields: list[str]) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, lineterminator="\n", extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    os.replace(tmp, p)


def append_rows(table: str, rows: list[dict], fields: list[str], month_key: str) -> None:
    by_month: dict[str, list[dict]] = {}
    for r in rows:
        by_month.setdefault(r[month_key][:7], []).append(r)
    for m, rs in by_month.items():
        p = data() / table / f"{m}.csv"
        write_csv(p, read_csv(p) + rs, fields)


def write_gz_json(p: Path, obj) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    with open(tmp, "wb") as f, gzip.GzipFile(fileobj=f, mode="wb", mtime=0) as g:
        g.write(json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode())
    os.replace(tmp, p)


# ------------------------------------------------------------------ state
# committed copy: state/state.json (written with every commit); cached copy: .state-cache/state.json (every run).
STATE_FILES = ("state/state.json", ".state-cache/state.json")
FAILED_RUNS = ".state-cache/failed_runs.json"   # runs that could not reach the data repo (e.g. token expired)


def record_failure(info: dict, error: str) -> None:
    """The data repo is unreachable: keep the failed run in the cache only; the next good run writes it to the runs
    table, and the gap check then logs the missing period."""
    p = ROOT / FAILED_RUNS
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        rows = json.loads(p.read_text())
    except (OSError, ValueError):
        rows = []
    rows.append({**info, "status": "failed", "n_events": "", "error": error})
    p.write_text(json.dumps(rows))


def load_state() -> dict:
    best = None
    for rel in STATE_FILES:
        try:
            s = json.loads((ROOT / rel).read_text())
        except (OSError, ValueError):
            continue
        if best is None or (s.get("saved") or "") > (best.get("saved") or ""):
            best = s
    return best or {"saved": "", "last_ok": None, "events": {}, "pending_runs": []}


def save_state(st: dict, rel: str) -> None:
    p = ROOT / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(st, separators=(",", ":"), sort_keys=True))
    os.replace(tmp, p)


# ------------------------------------------------------------------ one run
def run(run_info: dict, events: list | None, error: str = "") -> dict:
    """Record one run (events=None means the fetch failed). Writes data/ only when something changed."""
    st = load_state()
    fp = ROOT / FAILED_RUNS
    try:
        earlier_failures = json.loads(fp.read_text())
    except (OSError, ValueError):
        earlier_failures = []
    st["pending_runs"] = earlier_failures + st["pending_runs"]
    now_s = run_info["started_at"]
    now = parse_iso(now_s)
    ok = events is not None
    recs = {r["event_id"]: (r, e) for e in (events or []) for r in [record(e)] if r}
    st["pending_runs"].append({**run_info, "status": "ok" if ok else "failed",
                               "n_events": len(recs) if ok else "", "error": error})
    summary = {"ok": ok, "onondaga": len(recs), "new": 0, "changed_fields": 0, "gone": 0, "back": 0, "gap": False}
    changes: list[dict] = []
    gaps: list[dict] = []
    raw_changes: list[dict] = []

    if ok:
        if st["last_ok"] and now - parse_iso(st["last_ok"]) > GAP_S:
            gaps.append({"no_data_from": st["last_ok"], "no_data_to": now_s,
                         "minutes": round((now - parse_iso(st["last_ok"])) / 60)})
            summary["gap"] = True
        evs = st["events"]
        for eid, (r, raw) in recs.items():
            cur = evs.get(eid)
            if cur is None:
                evs[eid] = {**r, "first_seen": now_s, "last_seen": now_s, "present": True}
                changes.append({"event_id": eid, "seen_at": now_s, "field": "_present", "old": "", "new": "1"})
                summary["new"] += 1
                raw_changes.append({"seen": now_s, "event": raw})
                continue
            touched = False
            if not cur["present"]:
                changes.append({"event_id": eid, "seen_at": now_s, "field": "_present", "old": "0", "new": "1"})
                cur["present"] = True
                summary["back"] += 1
                touched = True
            for f in TRACKED:
                if cur[f] != r[f]:
                    changes.append({"event_id": eid, "seen_at": now_s, "field": f, "old": cur[f], "new": r[f]})
                    cur[f] = r[f]
                    summary["changed_fields"] += 1
                    touched = True
            cur["updated"] = r["updated"]
            cur["last_seen"] = now_s
            if touched:
                raw_changes.append({"seen": now_s, "event": raw})
        for eid, cur in evs.items():
            if cur["present"] and eid not in recs:
                changes.append({"event_id": eid, "seen_at": now_s, "field": "_present", "old": "1", "new": "0"})
                cur["present"] = False
                summary["gone"] += 1
        st["last_ok"] = now_s

    raw_dir = data() / "raw" / now_s[:7]
    raw_path = raw_dir / f"{now_s[:10]}.json.gz"
    first_of_day = ok and not raw_path.exists()
    changed = bool(changes or gaps or not ok or first_of_day or earlier_failures or os.environ.get("FLUSH") == "1")
    if changed:
        append_rows("runs", st["pending_runs"], RUN_FIELDS, "started_at")
        st["pending_runs"] = []
        append_rows("changes", changes, CHANGE_FIELDS, "seen_at")
        if gaps:
            p = data() / "gaps.csv"
            write_csv(p, read_csv(p) + gaps, GAP_FIELDS)
        # events table: rewrite the months that hold events (small; latest fields + first/last seen)
        by_month: dict[str, list[dict]] = {}
        for eid, cur in st["events"].items():
            by_month.setdefault(cur["first_seen"][:7], []).append({"event_id": eid, **cur})
        for m, rows in by_month.items():
            rows.sort(key=lambda x: (x["first_seen"], x["event_id"]))
            write_csv(data() / "events" / f"{m}.csv", rows, EVENT_FIELDS)
        if first_of_day:
            write_gz_json(raw_path, {"snapshot_at": now_s, "county": COUNTY,
                                     "events": [raw for _, raw in recs.values()]})
        if raw_changes:
            write_gz_json(raw_dir / now_s[:10] / f"{now_s[11:19].replace(':', '')}.json.gz",
                          {"seen": now_s, "events": [c["event"] for c in raw_changes]})
    st["saved"] = now_s
    save_state(st, STATE_FILES[1])
    fp.unlink(missing_ok=True)
    if changed:
        save_state(st, STATE_FILES[0])
    summary["data_changed"] = changed
    return summary


# ------------------------------------------------------------------ reconstruction
def active_at(ts: str | float) -> dict:
    """Events present in the 511NY feed at time ts (ISO 'YYYY-MM-DDTHH:MM:SSZ' or epoch seconds), with their field
    values as of ts. Resolution = run spacing (the state at ts is the state of the last successful run <= ts).
    coverage = 'ok' | 'gap' (no successful run within GAP_SECONDS before ts) | 'before_archive'."""
    t = ts if isinstance(ts, (int, float)) else parse_iso(ts)
    runs = sorted(parse_iso(r["started_at"]) for r in read_all("runs") if r["status"] == "ok")
    prev = [r for r in runs if r <= t]
    if not prev:
        return {"at": iso(t), "coverage": "before_archive", "as_of_run": None, "events": []}
    as_of = prev[-1]
    coverage = "ok" if t - as_of <= GAP_S else "gap"
    later = sorted((c for c in read_all("changes") if parse_iso(c["seen_at"]) > as_of),
                   key=lambda c: c["seen_at"], reverse=True)
    present_changes: dict[str, list[dict]] = {}
    for c in read_all("changes"):
        if c["field"] == "_present":
            present_changes.setdefault(c["event_id"], []).append(c)
    out = []
    for e in read_all("events"):
        if parse_iso(e["first_seen"]) > as_of:
            continue
        pres = [c for c in present_changes.get(e["event_id"], []) if parse_iso(c["seen_at"]) <= as_of]
        if not pres or max(pres, key=lambda c: c["seen_at"])["new"] != "1":
            continue
        v = dict(e)
        for c in later:                               # undo changes made after as_of, newest first
            if c["event_id"] == e["event_id"] and c["field"] in TRACKED:
                v[c["field"]] = c["old"]
        v.pop("last_seen", None)
        v.pop("updated", None)
        out.append(v)
    return {"at": iso(t), "coverage": coverage, "as_of_run": iso(as_of), "events": out}


# ------------------------------------------------------------------ main
def duplicate_trigger(t0: float) -> bool:
    """True (and nothing is written) if the last successful run started less than MIN_INTERVAL_S ago, unless FLUSH=1."""
    last = load_state().get("last_ok")
    if os.environ.get("FLUSH") == "1" or not last or t0 - parse_iso(last) >= MIN_INTERVAL_S:
        return False
    print(f"skipped: duplicate trigger, last successful run {round((t0 - parse_iso(last)) / 60)} min ago")
    return True


def main() -> int:
    t0 = time.time()
    started = iso(t0)
    # schedule = GitHub's backup cron; workflow_dispatch = cron-job.org (main trigger) or the manual button.
    sched = scheduled_slot(t0) if os.environ.get("GITHUB_EVENT_NAME", "") in ("schedule", "workflow_dispatch") else ""
    info = {"run_id": os.environ.get("GITHUB_RUN_ID") or f"local-{int(t0)}", "scheduled_at": sched,
            "started_at": started}
    if len(sys.argv) == 3 and sys.argv[1] == "--record-failure":
        record_failure(info, sys.argv[2])
        print(f"recorded failed run: {sys.argv[2]}")
        return 1
    if duplicate_trigger(t0):
        return 0
    try:
        events, err = fetch(), ""
    except Exception as ex:  # record the class only: messages can echo URLs or headers
        code = getattr(ex, "code", None)
        events, err = None, f"{type(ex).__name__}{f' {code}' if code else ''}"
    s = run(info, events, err)
    late = f", late {round((t0 - parse_iso(sched)) / 60)} min" if sched else ""
    print(f"{'ok' if s['ok'] else 'FAILED ' + err}: {time.time() - t0:.1f}s{late}; onondaga {s['onondaga']}, "
          f"new {s['new']}, changed fields {s['changed_fields']}, gone {s['gone']}, back {s['back']}, "
          f"gap {s['gap']}, data_changed {s['data_changed']}")
    return 0 if s["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
