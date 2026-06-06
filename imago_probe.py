#!/usr/bin/env python3
"""
IMAGO API probe — confirm the documented preview-URL and /download paths
work against our customer base, before wiring them into imago.py.

Per the official spec (PDF v1, 12.06.25):
  - Preview URL format: {API-URL}/{db-2-letter}/{pictureid}/{resolution-name}
    db: "st" | "sp"
    resolution: "thumbs" (192px) | "smalls" (420px) | "mediums" (1000px)
    e.g. https://API-URL.imago-images.de/st/95739952/smalls
  - Download endpoint: POST {API-URL}/download with JSON body
    {"pictureid": "...", "db": "stock"|"sport", "res": 1|2|4|8|9}
    Returns raw image/jpeg bytes on success.

Our /search works at https://api.imago-images.com/api/search, so we treat
https://api.imago-images.com/api as the customer-base. We test the preview
URL with and without auth headers (Discord's image proxy won't send auth —
the preview URL has to work anonymously OR we have to use /download +
re-upload as a Discord attachment).
"""

import json
import os
import sys

import httpx

API_USER = os.environ.get("IMAGO_API_USER", "")
API_KEY = os.environ.get("IMAGO_API_KEY", "")
BASE = os.environ.get("IMAGO_API_BASE", "https://api.imago-images.com/api").rstrip("/")

# Known-good picture from earlier probes (Messi statue, db=stock).
PID = os.environ.get("IMAGO_PROBE_PID", "858068900")
DB_FULL = "stock"
DB_SHORT = "st"

AUTH_HEADERS = {"X-API-User": API_USER, "X-API-Key": API_KEY}


def _dump(label: str, r: httpx.Response) -> None:
    ct = r.headers.get("content-type", "")
    print(f"  [{label}] HTTP {r.status_code}  content-type={ct}  len={len(r.content)}")
    if ct.startswith("image/"):
        sig = r.content[:8].hex()
        print(f"    -> {sig}  (FFD8FF... = real JPEG)")
    else:
        print(f"    body: {r.text[:300]}")


def main() -> None:
    if not (API_USER and API_KEY):
        print("ERROR: set IMAGO_API_USER and IMAGO_API_KEY")
        sys.exit(1)

    print(f"Base: {BASE}\nPID:  {PID}  db={DB_FULL}/{DB_SHORT}\n" + "=" * 60)

    with httpx.Client(timeout=20, follow_redirects=False) as c:
        # === A. Preview URL — the simple path if it works anonymously ===
        for res_name in ("smalls", "mediums", "thumbs"):
            url = f"{BASE}/{DB_SHORT}/{PID}/{res_name}"
            print(f"\n>>> preview URL  {url}")
            try:
                r = c.get(url)  # no auth headers — Discord can't send any
                _dump("anon", r)
            except Exception as e:
                print(f"  [anon] ERR {e}")
            try:
                r = c.get(url, headers=AUTH_HEADERS)
                _dump("auth", r)
            except Exception as e:
                print(f"  [auth] ERR {e}")

        # === B. /download — POST with body, returns bytes ===
        print(f"\n>>> POST {BASE}/download   (db={DB_FULL}, res=2)")
        try:
            r = c.post(
                f"{BASE}/download",
                headers={**AUTH_HEADERS, "Content-Type": "application/json"},
                json={"pictureid": PID, "db": DB_FULL, "res": 2},
            )
            _dump("download/2", r)
        except Exception as e:
            print(f"  ERR {e}")

        # Some APIs want res as a string per the docs ("res": "1") — try that too.
        print(f"\n>>> POST {BASE}/download   (db={DB_FULL}, res='2' as string)")
        try:
            r = c.post(
                f"{BASE}/download",
                headers={**AUTH_HEADERS, "Content-Type": "application/json"},
                json={"pictureid": str(PID), "db": DB_FULL, "res": "2"},
            )
            _dump("download/2-str", r)
        except Exception as e:
            print(f"  ERR {e}")


if __name__ == "__main__":
    main()
