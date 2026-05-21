#!/usr/bin/env python3
"""Live pipeline monitor — polls task states every 5s."""
import sys
import time
import xmlrpc.client

URL = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8069"
DB = sys.argv[2] if len(sys.argv) > 2 else "leviathan_dev"
USER, PASS = "admin", "admin"

common = xmlrpc.client.ServerProxy(f"{URL}/xmlrpc/2/common")
uid = common.authenticate(DB, USER, PASS, {})
models = xmlrpc.client.ServerProxy(f"{URL}/xmlrpc/2/object")

FIELDS = ["name", "state", "url", "via_batch", "user_id",
          "started_at", "last_heartbeat", "error_message",
          "prd_text", "score", "qc_verdict"]

COLORS = {
    "not_assigned": "\033[90m",   # gray
    "draft": "\033[37m",          # white
    "extracting": "\033[33m",     # yellow
    "generating": "\033[34m",     # blue
    "scoring": "\033[35m",        # magenta
    "done": "\033[32m",           # green
    "submitted": "\033[36m",      # cyan
    "failed": "\033[31m",         # red
    "cancelled": "\033[90m",      # gray
}
RESET = "\033[0m"

print(f"\033[1mLeviathan Pipeline Monitor\033[0m — {URL} / {DB}")
print("Ctrl+C to stop\n")

try:
    while True:
        tasks = models.execute_kw(DB, uid, PASS, "leviathan.job", "search_read",
            [[("state", "not in", ["submitted", "cancelled"])]],
            {"fields": FIELDS, "order": "id desc", "limit": 20})

        print(f"\033[2J\033[H\033[1mLeviathan Pipeline Monitor\033[0m — {time.strftime('%H:%M:%S')}")
        print(f"{'ID':<12} {'STATE':<14} {'URL':<30} {'BAT':>3} {'USER':>5} {'SCORE':>6} {'QC':<15} {'ERROR'}")
        print("-" * 110)

        for t in tasks:
            c = COLORS.get(t["state"], "")
            url = (t["url"] or "")[:28]
            bat = "Y" if t["via_batch"] else ""
            usr = str(t["user_id"][0]) if t["user_id"] else ""
            score = f"{t['score']:.0f}" if t["score"] else ""
            qc = t["qc_verdict"] or ""
            err = (t["error_message"] or "")[:40]
            prd = "PRD" if t["prd_text"] else ""
            print(f"{c}{t['name']:<12} {t['state']:<14} {url:<30} {bat:>3} {usr:>5} {score:>6} {qc:<15} {prd} {err}{RESET}")

        # Summary
        states = {}
        for t in tasks:
            states[t["state"]] = states.get(t["state"], 0) + 1
        summary = " | ".join(f"{COLORS.get(s,'')}{s}: {c}{RESET}" for s, c in sorted(states.items()))
        print(f"\n{summary}")

        time.sleep(5)
except KeyboardInterrupt:
    print("\nStopped.")
