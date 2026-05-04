"""Extract Tesseract OTS.xlsx sheet 1 into data/instances.json.

Each instance spans two rows in the xlsx: the first row has all metadata,
the second row has only the secondary model's evaluation result. We flatten
to one row per (instance, model) pair by carrying forward shared fields.
"""
from __future__ import annotations

import json
import re
import sys
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

NS = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
NS_T = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"

ROOT = Path(__file__).resolve().parent.parent
XLSX = ROOT / "Tesseract OTS.xlsx"
OUT = ROOT / "data" / "instances.json"


def col_to_idx(letters: str) -> int:
    idx = 0
    for c in letters:
        idx = idx * 26 + (ord(c) - ord("A") + 1)
    return idx - 1


def read_shared_strings(z: zipfile.ZipFile) -> list[str]:
    root = ET.fromstring(z.read("xl/sharedStrings.xml"))
    out = []
    for si in root.findall("s:si", NS):
        parts = [t.text or "" for t in si.iter(f"{NS_T}t")]
        out.append("".join(parts))
    return out


def read_rows(z: zipfile.ZipFile, strings: list[str]) -> list[dict[int, str]]:
    sheet = ET.fromstring(z.read("xl/worksheets/sheet1.xml"))
    rows = []
    for row in sheet.findall(".//s:row", NS):
        cells: dict[int, str] = {}
        for c in row.findall("s:c", NS):
            ref = c.get("r", "")
            col_letters = re.match(r"[A-Z]+", ref).group()
            idx = col_to_idx(col_letters)
            v = c.find("s:v", NS)
            if v is None:
                continue
            val = v.text or ""
            if c.get("t") == "s":
                val = strings[int(val)]
            cells[idx] = val
        rows.append(cells)
    return rows


def norm_num(s: str) -> float | int | str:
    if not s:
        return ""
    try:
        f = float(s)
        return int(f) if f.is_integer() else f
    except ValueError:
        return s


def repo_slug(url: str) -> str:
    m = re.search(r"github\.com/([^/]+/[^/]+)", url or "")
    return m.group(1) if m else ""


def main() -> int:
    with zipfile.ZipFile(XLSX) as z:
        strings = read_shared_strings(z)
        rows = read_rows(z, strings)

    header = rows[0]
    cols = {header[i].strip(): i for i in sorted(header)}

    def field(r: dict[int, str], key: str) -> str:
        i = cols.get(key)
        return r.get(i, "") if i is not None else ""

    current: dict = {}
    out = []
    for r in rows[1:]:
        instance_id = field(r, "Instance Id").strip()
        if instance_id:
            current = {
                "sr_no": norm_num(field(r, "Sr. No.")),
                "instance_id": instance_id,
                "repo_url": field(r, "Repo_URL"),
                "repo": repo_slug(field(r, "Repo_URL")),
                "pr_url": field(r, "PR URL"),
                "issue_url": field(r, "issue_url"),
                "f2p_count": norm_num(field(r, "f2p count")),
                "p2p_count": norm_num(field(r, "p2p count")),
                "language": field(r, "Language"),
                "difficulty": field(r, "difficulty"),
                "docker_uri": field(r, "Docker URI"),
                "trajectory_url": field(r, "Trajectory"),
            }
        model = field(r, "Model").strip()
        if not model:
            continue
        rec = dict(current)
        rec.update({
            "model": model,
            "files_modified": norm_num(field(r, "Total No of Files Modified")),
            "tool_calls": norm_num(field(r, "Total No of Tool Calls")),
            "time_secs": norm_num(field(r, "Time of Completion (secs)")),
            "pass_at_1": field(r, "Pass@1"),
        })
        if not rec["trajectory_url"]:
            rec["trajectory_url"] = f"https://github.com/Ethara-Ai/tesseract/tree/main/trajectories/{rec['instance_id']}"
        out.append(rec)

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(out, indent=2))
    print(f"Wrote {len(out)} rows to {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
