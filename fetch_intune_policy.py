#!/usr/bin/env python3
"""Fetch an Intune Settings Catalog policy from the sandbox tenant and save
it as a Graph-creation-ready JSON.

Strips the response of read-only/system fields (id, createdDateTime, etc.)
and `settings[].id` so the output can be POSTed back to
``/deviceManagement/configurationPolicies`` without modification.

Auth: client-credentials, sandbox tenant only. Required env vars
(loaded from ~/.zshenv or the calling shell):
    INTUNE_SANDBOX_TENANT_ID
    INTUNE_SANDBOX_CLIENT_ID
    INTUNE_SANDBOX_CLIENT_SECRET

Usage:
    python3 fetch_intune_policy.py POLICY_ID OUTPUT_PATH
"""

import argparse
import json
import os
import sys
import urllib.parse
import urllib.request


GRAPH_BASE = "https://graph.microsoft.com/beta"
SCOPE = "https://graph.microsoft.com/.default"
SYSTEM_FIELDS = {
    "@odata.context",
    "id",
    "createdDateTime",
    "lastModifiedDateTime",
    "settingCount",
    "creationSource",
    "isAssigned",
    "priorityMetaData",
    "settings@odata.context",
}


def get_token(tenant_id: str, client_id: str, client_secret: str) -> str:
    url = f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"
    body = urllib.parse.urlencode({
        "grant_type": "client_credentials",
        "client_id": client_id,
        "client_secret": client_secret,
        "scope": SCOPE,
    }).encode()
    req = urllib.request.Request(
        url, data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())["access_token"]


def graph_get(token: str, path: str) -> dict:
    req = urllib.request.Request(
        f"{GRAPH_BASE}{path}",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
    )
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())


def clean_policy(raw: dict) -> dict:
    """Drop system fields and per-setting ids so the result is POSTable."""
    out = {k: v for k, v in raw.items() if k not in SYSTEM_FIELDS}

    # templateReference: drop null display fields
    if "templateReference" in out and isinstance(out["templateReference"], dict):
        out["templateReference"] = {
            k: v for k, v in out["templateReference"].items() if v is not None
        }

    # settings: drop the `id` field — Graph assigns one on create
    if "settings" in out and isinstance(out["settings"], list):
        out["settings"] = [
            {k: v for k, v in s.items() if k != "id"} for s in out["settings"]
        ]

    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("policy_id", help="Configuration policy id (GUID)")
    parser.add_argument("output_path", help="Where to write the cleaned JSON")
    args = parser.parse_args()

    tenant_id = os.environ.get("INTUNE_SANDBOX_TENANT_ID")
    client_id = os.environ.get("INTUNE_SANDBOX_CLIENT_ID")
    client_secret = os.environ.get("INTUNE_SANDBOX_CLIENT_SECRET")
    missing = [n for n, v in [
        ("INTUNE_SANDBOX_TENANT_ID", tenant_id),
        ("INTUNE_SANDBOX_CLIENT_ID", client_id),
        ("INTUNE_SANDBOX_CLIENT_SECRET", client_secret),
    ] if not v]
    if missing:
        print(f"ERROR: missing env vars: {', '.join(missing)}", file=sys.stderr)
        return 2

    print(f"Authenticating to sandbox tenant {tenant_id[:8]}...", file=sys.stderr)
    token = get_token(tenant_id, client_id, client_secret)

    print(f"GET /deviceManagement/configurationPolicies/{args.policy_id}", file=sys.stderr)
    raw = graph_get(token, f"/deviceManagement/configurationPolicies/{args.policy_id}?$expand=settings")

    cleaned = clean_policy(raw)
    print(f"Captured: {cleaned.get('name')!r} ({len(cleaned.get('settings', []))} settings)", file=sys.stderr)

    os.makedirs(os.path.dirname(os.path.abspath(args.output_path)) or ".", exist_ok=True)
    with open(args.output_path, "w", encoding="utf-8") as f:
        json.dump(cleaned, f, indent=2, ensure_ascii=False)
    print(f"Written: {args.output_path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
