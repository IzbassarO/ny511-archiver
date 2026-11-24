# ny511-archiver

Code that archives public **511NY** (NYSDOT CARS) traffic events for **Onondaga County, NY** every 15 minutes.
This repository holds **code only — no data and no credentials**. The archive itself is written to a separate
private repository by the scheduled workflow.

- `archive.py` — one run: fetch the statewide 511NY `/events` feed, keep Onondaga County, update the tables.
  Standard library only. `active_at(timestamp)` reconstructs which events were present at any time.
- `.github/workflows/archive.yml` — main trigger: an external cron (cron-job.org) calls the `workflow_dispatch` API at
  minutes 7, 22, 37, 52 UTC. GitHub's own cron at the same minutes is a backup (GitHub often delays or skips scheduled
  runs). Runs never overlap (concurrency group), and a trigger less than 10 min after the last successful run is
  skipped, so duplicates are harmless. Manual button option `flush` commits waiting run rows. Triggers are
  `schedule` and `workflow_dispatch` only (never `pull_request`), so forks cannot run it or read its secrets.
- `.github/workflows/keepalive.yml` — monthly; re-enables the scheduled workflows so GitHub does not disable them
  after 60 days without activity in this repo.

## Tables written (CSV per month, UTC)
| table | columns |
|---|---|
| `data/runs/` | run_id, scheduled_at, started_at, status (ok/failed), n_events, error |
| `data/events/` | event_id, category, type, road, direction, description, lanes, start, end, geometry, updated, first_seen, last_seen |
| `data/changes/` | event_id, seen_at, field, old, new (`_present`: appear ""→1, gone 1→0, back 0→1) |
| `data/gaps.csv` | no_data_from, no_data_to, minutes (successful runs more than 25 min apart) |
| `data/raw/` | gzipped daily snapshot + raw records of each run's new/changed events |

## Secrets (repository secrets only)
`NY511_USERNAME`, `NY511_PASSWORD` (511NY developer account) and `ARCHIVE_TOKEN` (fine-grained token limited to the
data repository, Contents: read and write). Credentials go only into the HTTP Authorization header and are never
printed; the workflow log prints counts and timings only.
If `ARCHIVE_TOKEN` is missing, expired or invalid, the run fails with the error **"ARCHIVE_TOKEN expired/invalid"**;
the failed run is kept in the Actions cache and written to `runs` (plus a `gaps` row) by the next good run. A warning
appears 7 days before the token expires.

The external cron uses its own fine-grained token (only this repository, Actions: read and write), stored only in the
cron service. If it expires, dispatches fail (the cron service emails the owner) and the GitHub backup schedule keeps
running.

## Run locally
`NY511_USERNAME=… NY511_PASSWORD=… ARCHIVE_ROOT=/path/to/data-repo python3 archive.py`
Tests: `python3 -m unittest discover -s tests`

Data: 511NY / NYSDOT, provided "as is" (511ny.org/help/24); credit "powered by 511NY".
