#!/usr/bin/env python3
"""Upload a local CSV to the btw-nutrition DigitalOcean Spaces bucket.

Reads credentials from meltano-elt-pipelines/.env:
  - DO_SPACES_ACCESS_KEY
  - DO_SPACES_SECRET_KEY
  - DO_SPACES_REGION (default: nyc3)
  - DO_SPACES_ENDPOINT (optional; derived from region if not set)

Bucket is hard-coded to `btw-nutrition` because that is what the NDO management
commands expect (see nutrition-data-ops/bitewell/processors/downloader.py).
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import boto3
from botocore.exceptions import BotoCoreError, ClientError
from dotenv import load_dotenv

BUCKET = "btw-nutrition"
DEFAULT_REGION = "nyc3"
DEFAULT_PREFIX = "ops-skill"


def _load_env() -> None:
    """Load the discovered .env. Delegates to ndo_run's discovery chain so
    upload.py works whether the skill lives in meltano-elt-pipelines (legacy)
    or in foodhealthco-claude-code-plugins (new home)."""
    # Import lazily to avoid circular import surprises if upload.py is invoked
    # standalone before ndo_run.py runs.
    try:
        from ndo_run import discover_env_file  # type: ignore
    except ImportError:
        # Fallback: import via path manipulation if upload.py is called from
        # a different cwd
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "ndo_run", Path(__file__).resolve().parent / "ndo_run.py"
        )
        module = importlib.util.module_from_spec(spec)  # type: ignore
        spec.loader.exec_module(module)  # type: ignore
        discover_env_file = module.discover_env_file  # type: ignore

    env_path = discover_env_file()
    if env_path:
        load_dotenv(env_path)


def _timestamped_key(command: str, prefix: str = DEFAULT_PREFIX) -> str:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%S")
    safe_cmd = command.replace("/", "_").replace(" ", "_")
    return f"{prefix}/{ts}-{safe_cmd}.csv"


def _client():
    access_key = os.environ.get("DO_SPACES_ACCESS_KEY")
    secret_key = os.environ.get("DO_SPACES_SECRET_KEY")
    if not access_key or not secret_key:
        sys.exit(
            "error: DO_SPACES_ACCESS_KEY / DO_SPACES_SECRET_KEY not set. "
            "Check meltano-elt-pipelines/.env."
        )
    region = os.environ.get("DO_SPACES_REGION", DEFAULT_REGION)
    # Always use the generic regional endpoint, NOT a bucket-specific one.
    # If DO_SPACES_ENDPOINT is set in .env it's often `https://<bucket>.<region>.digitaloceanspaces.com`,
    # which routes PutObject for *other* buckets to the wrong subdomain → AccessDenied.
    endpoint = f"https://{region}.digitaloceanspaces.com"
    session = boto3.session.Session()
    return session.client(
        "s3",
        region_name=region,
        endpoint_url=endpoint,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
    )


def upload(local_path: Path, command: str, key: str | None = None) -> str:
    if not local_path.exists():
        sys.exit(f"error: {local_path} does not exist")
    _load_env()
    spaces_key = key or _timestamped_key(command)
    client = _client()
    try:
        client.upload_file(str(local_path), BUCKET, spaces_key)
    except (BotoCoreError, ClientError) as exc:
        sys.exit(f"error: upload failed: {exc}")
    return spaces_key


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("local_path", type=Path, help="Path to local CSV")
    parser.add_argument(
        "--command",
        required=True,
        help="Command name used in the generated Spaces key",
    )
    parser.add_argument(
        "--key",
        default=None,
        help="Override the generated key (otherwise: ops-skill/<timestamp>-<command>.csv)",
    )
    args = parser.parse_args(argv)
    key = upload(args.local_path, args.command, args.key)
    print(key)


if __name__ == "__main__":
    main()
