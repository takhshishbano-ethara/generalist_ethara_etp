#!/usr/bin/env python3
"""One-shot helper to obtain an OAuth refresh token for Google Drive.

Usage:
    python authorize_drive.py /path/to/client_secrets.json

Prerequisites:
    pip install google-auth-oauthlib google-api-python-client

What it does:
    1. Reads the OAuth Desktop-app client_secrets JSON downloaded from
       GCP Console → Credentials.
    2. Opens your default browser for the Google sign-in & consent flow.
    3. Spins up a tiny localhost server to receive the callback.
    4. Exchanges the auth code for a refresh token.
    5. Prints client_id, client_secret, refresh_token to stdout — paste
       these into Fenrir → Configuration → Google Drive.

You only need to run this ONCE per Google account whose Drive you want to
upload to. The refresh token doesn't expire (unless revoked).
"""

import json
import sys

try:
    from google_auth_oauthlib.flow import InstalledAppFlow
except ImportError:
    sys.stderr.write(
        "ERROR: google-auth-oauthlib is not installed.\n"
        "Run:  pip install google-auth-oauthlib google-api-python-client\n")
    sys.exit(1)


SCOPES = ["https://www.googleapis.com/auth/drive"]


def main():
    if len(sys.argv) != 2:
        sys.stderr.write(
            "Usage: python authorize_drive.py /path/to/client_secrets.json\n")
        sys.exit(1)

    secrets_path = sys.argv[1]

    flow = InstalledAppFlow.from_client_secrets_file(secrets_path, SCOPES)
    # Opens browser, runs a tiny localhost server, captures the code.
    creds = flow.run_local_server(
        port=0,
        prompt="consent",
        access_type="offline",
        open_browser=True,
    )

    if not creds.refresh_token:
        sys.stderr.write(
            "\nERROR: Google returned credentials without a refresh token.\n"
            "This usually means you've already authorized this client before.\n"
            "Revoke access at https://myaccount.google.com/permissions and "
            "re-run this script.\n")
        sys.exit(2)

    # Reload secrets to grab client_id + client_secret in the same output.
    with open(secrets_path, "r", encoding="utf-8") as fh:
        raw = json.load(fh)
    section = raw.get("installed") or raw.get("web") or {}

    print("\n" + "=" * 60)
    print("SUCCESS — paste these into Fenrir → Configuration → Google Drive:")
    print("=" * 60)
    print(f"OAuth Client ID:      {section.get('client_id', '')}")
    print(f"OAuth Client Secret:  {section.get('client_secret', '')}")
    print(f"OAuth Refresh Token:  {creds.refresh_token}")
    print("=" * 60)


if __name__ == "__main__":
    main()
