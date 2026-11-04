"""Refresh or reconnect The Farm 3's YouTube upload authorization.

Run from The Farm root:
    .venv/bin/python "theFarm 3/reauth_youtube.py"

This script never uploads videos and never changes the production checkpoint.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys
import tempfile

import config as settings

SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]


def oauth_client_file() -> Path:
    token_dir = Path(settings.TOKEN_RESOURCE_DIR)
    selected = str(getattr(settings, "YOUTUBE_CLIENT_FILE", "") or "").strip()
    if selected:
        path = Path(selected)
        if not path.is_absolute():
            path = token_dir / path
        if not path.is_file():
            raise FileNotFoundError(f"YouTube OAuth client file not found: {path}")
        return path
    candidates = sorted(token_dir.glob("client_secret*.json"))
    if len(candidates) != 1:
        raise FileNotFoundError(
            f"Expected exactly one client_secret*.json in {token_dir}; "
            "set YOUTUBE_CLIENT_FILE in config.py if there are multiple."
        )
    return candidates[0]


def save_token(token_path: Path, content: str) -> None:
    token_path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=".youtube_token_", suffix=".tmp", dir=token_path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            os.fchmod(f.fileno(), 0o600)
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_name, token_path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def main() -> int:
    parser = argparse.ArgumentParser(description="Renew The Farm's YouTube OAuth login (no upload).")
    parser.add_argument("--login", action="store_true", help="Force browser login even if existing token works")
    args = parser.parse_args()

    try:
        from google.auth.exceptions import RefreshError
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError as exc:
        print(f"Missing Google OAuth dependencies in this Python environment: {exc}")
        return 1

    token_path = Path(settings.YOUTUBE_TOKEN_FILE)
    creds = None
    if token_path.is_file() and not args.login:
        try:
            creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
            if not creds.valid and creds.refresh_token:
                print("Refreshing saved YouTube authorization...")
                creds.refresh(Request())
        except (RefreshError, ValueError, OSError) as exc:
            print(f"Saved login couldn't be refreshed ({type(exc).__name__}).")
            creds = None

    if creds is None or not creds.valid:
        print("Opening Google login in your browser...")
        print("Choose the Google account that owns your YouTube channel.")
        print("Approve the requested YouTube upload permission. Nothing will be uploaded now.")
        flow = InstalledAppFlow.from_client_secrets_file(str(oauth_client_file()), SCOPES)
        creds = flow.run_local_server(host="localhost", port=0, open_browser=True,
                                      prompt="consent", access_type="offline", timeout_seconds=300)

    if not creds.valid:
        print("Login did not produce valid credentials. Token unchanged.")
        return 1
    save_token(token_path, creds.to_json())
    print(f"YouTube authorization saved successfully to: {token_path}")
    print('Resume The Farm from its root folder with: .venv/bin/python "theFarm 3/run.py" --resume')
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (Exception, KeyboardInterrupt) as exc:
        print(f"Authorization was not completed: {type(exc).__name__}: {exc}", file=sys.stderr)
        sys.exit(1)
