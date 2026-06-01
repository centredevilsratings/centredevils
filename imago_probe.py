#!/usr/bin/env python3
"""
Standalone IMAGO API schema probe.

Run this from anywhere the network can reach api.imago-images.com (e.g. a
Render shell, or locally) to confirm the live request/response schema so the
imago.py client can be calibrated.

Usage:
    IMAGO_API_USER=imagoapi IMAGO_API_KEY=... python imago_probe.py "Lionel Messi"

It tries a few plausible search payloads against {base}{path} and dumps the
HTTP status + a slice of each response body. Send me the output and I'll lock
imago.py's parser/URL template to the real schema.
"""

import json
import os
import sys

import httpx

API_USER = os.environ.get("IMAGO_API_USER", "")
API_KEY = os.environ.get("IMAGO_API_KEY", "")
BASE = os.environ.get("IMAGO_API_BASE", "https://api.imago-images.com/api").rstrip("/")

QUERY = sys.argv[1] if len(sys.argv) > 1 else "soccer"

HEADERS = {
    "api-user": API_USER,
    "api-key": API_KEY,
    "Content-Type": "application/json",
    "Accept": "application/json",
}

# A spread of candidate payloads + endpoints — whichever returns 200 with
# real hits reveals the true schema.
ATTEMPTS = [
    ("POST", "/search", {"searchquery": QUERY, "querystring": QUERY, "size": 3, "from": 0}),
    ("POST", "/search", {"searchterm": QUERY, "limit": 3}),
    ("POST", "/search", {"query": QUERY, "size": 3}),
    ("GET",  "/search", {"q": QUERY, "size": 3}),
    ("GET",  "/images", {"q": QUERY, "limit": 3}),
]


def main() -> None:
    if not (API_USER and API_KEY):
        print("ERROR: set IMAGO_API_USER and IMAGO_API_KEY env vars")
        sys.exit(1)

    print(f"Base: {BASE}\nQuery: {QUERY!r}\n" + "=" * 60)
    with httpx.Client(timeout=20) as client:
        for method, path, params in ATTEMPTS:
            url = f"{BASE}{path}"
            print(f"\n>>> {method} {url}\n    payload={params}")
            try:
                if method == "POST":
                    r = client.post(url, json=params, headers=HEADERS)
                else:
                    r = client.get(url, params=params, headers=HEADERS)
            except Exception as e:
                print(f"    EXCEPTION: {e}")
                continue
            print(f"    HTTP {r.status_code}  content-type={r.headers.get('content-type')}")
            body = r.text
            try:
                parsed = r.json()
                body = json.dumps(parsed, indent=2, ensure_ascii=False)
                if isinstance(parsed, dict):
                    print(f"    top-level keys: {list(parsed.keys())}")
            except Exception:
                pass
            print("    body (first 1500 chars):")
            print("    " + body[:1500].replace("\n", "\n    "))


if __name__ == "__main__":
    main()
