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

# Auth-scheme candidates — first probe revealed api-user/api-key headers
# return 401 "Invalid Username or Password" (classic Basic Auth phrasing),
# so Basic is the prime suspect.
AUTH_SCHEMES = [
    ("basic", {}),                                                # httpx.BasicAuth
    ("bearer", {"Authorization": f"Bearer {API_KEY}"}),
    ("api-key-header", {"X-API-Key": API_KEY, "X-API-User": API_USER}),
    ("legacy-lowercase", {"api-user": API_USER, "api-key": API_KEY}),
]

PAYLOADS = [
    ("POST", "/search", {"searchquery": QUERY, "querystring": QUERY, "size": 3, "from": 0}),
    ("POST", "/search", {"query": QUERY, "size": 3}),
    ("GET",  "/search", {"q": QUERY, "size": 3}),
]


def main() -> None:
    if not (API_USER and API_KEY):
        print("ERROR: set IMAGO_API_USER and IMAGO_API_KEY env vars")
        sys.exit(1)

    print(f"Base: {BASE}\nUser: {API_USER}\nQuery: {QUERY!r}\n" + "=" * 60)

    with httpx.Client(timeout=20) as client:
        for scheme_name, extra_headers in AUTH_SCHEMES:
            print(f"\n########## AUTH SCHEME: {scheme_name} ##########")
            auth = httpx.BasicAuth(API_USER, API_KEY) if scheme_name == "basic" else None
            headers = {"Content-Type": "application/json",
                       "Accept": "application/json", **extra_headers}

            for method, path, params in PAYLOADS:
                url = f"{BASE}{path}"
                print(f"\n>>> {method} {url}\n    payload={params}")
                try:
                    if method == "POST":
                        r = client.post(url, json=params, headers=headers, auth=auth)
                    else:
                        r = client.get(url, params=params, headers=headers, auth=auth)
                except Exception as e:
                    print(f"    EXCEPTION: {e}")
                    continue
                print(f"    HTTP {r.status_code}  "
                      f"content-type={r.headers.get('content-type')}")
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
                # Quick success short-circuit so output stays readable.
                if r.status_code == 200:
                    print(f"\n*** SUCCESS with scheme={scheme_name}, "
                          f"{method} {path}, payload-keys={list(params.keys())} ***")
                    return


if __name__ == "__main__":
    main()
