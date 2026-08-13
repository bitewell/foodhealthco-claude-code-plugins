#!/usr/bin/env python3
"""Upload a local CSV to the NDO object store (GCS) for the management commands.

NDO migrated object storage from DigitalOcean Spaces to Google Cloud Storage
(see nutrition-data-ops/bitewell/helpers/cloud_storage.py), so the management
commands now read their input CSV from `gs://$NDO_GCS_BUCKET/<key>`. This helper
uploads there so the runner's hand-off matches where NDO actually reads. (The old
DO Spaces path left the CSV in `btw-nutrition` while NDO looked in GCS -> the
send died with "Cannot determine path without bucket name.")

Auth is Application Default Credentials (google.cloud.storage picks up the
ambient gcloud ADC / Workload Identity). The bucket comes from NDO_GCS_BUCKET
(e.g. ndo-files-prod); GCP_PROJECT_ID is optional (ADC default project is fine
for cross-project bucket access).
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from google.cloud import storage

DEFAULT_PREFIX = "ops-skill"


def _load_env() -> None:
    """Load the discovered .env. Delegates to ndo_run's discovery chain."""
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


def upload(local_path: Path, command: str, key: str | None = None) -> str:
    if not local_path.exists():
        sys.exit(f"error: {local_path} does not exist")
    _load_env()
    gcs_key = key or _timestamped_key(command)
    bucket = os.environ.get("NDO_GCS_BUCKET")
    if not bucket:
        sys.exit(
            "error: NDO_GCS_BUCKET not set. NDO's management commands read the "
            "input CSV from GCS (post DO->GCP migration), so the runner must "
            "upload there. Set NDO_GCS_BUCKET (e.g. ndo-files-prod) in your .env."
        )
    project = os.environ.get("GCP_PROJECT_ID") or None
    try:
        client = storage.Client(project=project) if project else storage.Client()
        client.bucket(bucket).blob(gcs_key).upload_from_filename(str(local_path))
    except Exception as exc:
        sys.exit(f"error: GCS upload to gs://{bucket}/{gcs_key} failed: {exc}")
    return gcs_key


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
