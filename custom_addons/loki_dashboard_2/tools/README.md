# Loki Dashboard 2 — Offline Tools

Two standalone CLIs convert raw clinical data into the assets the portal serves.
Neither depends on Odoo runtime.

## 1. `ingest_excel.py` — Excel → `patients.json`

```bash
python tools/ingest_excel.py [--data-dir PATH] [--output PATH]
```

**Defaults**

- `--data-dir`: `../data/Clinical_Data` relative to this script
- `--output`:   `<data-dir>/patients.json`

**Expects**

```
Clinical_Data/
    Patient_3/
        structured/*.xlsx       # 17 sheet names per the dashboard plan
        records/<category>/*.pdf
        wsi/*.svs
    Patient_4/...
    Patient_7_GB/...
```

**Prerequisites**

```bash
pip install openpyxl
```

The script reads workbooks in `read_only` / `data_only` mode (no formula
evaluation) and skips sheet names it doesn't recognise.

## 2. `generate_dzi.py` — `.svs` → Deep Zoom tiles

```bash
python tools/generate_dzi.py [--data-dir PATH] [--output-dir PATH]
```

**Defaults**

- `--data-dir`:  `../data/Clinical_Data`
- `--output-dir`: `../static/src/wsi/dzi`

**Prerequisites**

- macOS: `brew install vips`
- Debian/Ubuntu: `apt-get install libvips-tools`

The script invokes `vips dzsave` once per `.svs`. Existing `<basename>.dzi`
files are skipped, so the run is idempotent.

## After running both

Restart the Odoo worker (no schema changes). The `/loki2` page picks the new
data up on next request.
