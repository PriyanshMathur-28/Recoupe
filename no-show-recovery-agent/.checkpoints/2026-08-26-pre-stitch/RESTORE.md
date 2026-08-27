# Checkpoint: pre-Stitch frontend (2026-08-26)

Snapshot of `frontend/` and the compiled `static/clients/` bundle taken
immediately before the Google Stitch design was converted to React.

## What is in here

| Path | Restores to |
| --- | --- |
| `frontend/` | `<repo>/frontend/` (source; `node_modules` excluded) |
| `static-clients/` | `<repo>/static/clients/` (the compiled bundle Flask serves) |

## Restore

From the repository root, in PowerShell:

    .\.checkpoints\2026-08-26-pre-stitch\restore.ps1

The script keeps `frontend/node_modules` in place so no reinstall is needed,
then rebuilds is *not* required — the previous compiled bundle is restored too,
so `python dashboard.py` serves the old dashboard immediately.

If you would rather rebuild from the restored source:

    cd frontend
    npm install
    npm run build

## Undo a restore

`restore.ps1` moves whatever is currently in place into
`.checkpoints/replaced-<timestamp>/` before overwriting, so a restore is itself
reversible.
