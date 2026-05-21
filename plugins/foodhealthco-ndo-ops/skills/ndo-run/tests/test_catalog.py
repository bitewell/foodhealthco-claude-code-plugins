"""Schema check on catalog.yaml — guards against malformed catalog entries
when adding new commands."""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

CATALOG_PATH = Path(__file__).resolve().parent.parent / "catalog.yaml"
VALID_INPUT_MODES = {"file", "source", "vendor", "file_or_source", "none"}
VALID_KEYS = {
    "help", "input", "args", "sync_flag", "csv_schema", "notes", "file_flag",
}


@pytest.fixture(scope="module")
def catalog():
    with open(CATALOG_PATH) as f:
        return yaml.safe_load(f)


def _flat(catalog):
    """Flatten the group-structured catalog into a {command_name: spec} dict."""
    out = {}
    for group, commands in catalog.items():
        for name, spec in commands.items():
            out[name] = (group, spec)
    return out


def test_catalog_parses(catalog):
    assert isinstance(catalog, dict)
    assert len(catalog) > 0  # at least one group


def test_no_duplicate_command_names(catalog):
    seen = set()
    for _group, commands in catalog.items():
        for name in commands:
            assert name not in seen, f"duplicate command name: {name}"
            seen.add(name)


@pytest.mark.parametrize("required", ["help", "input", "args"])
def test_every_entry_has_required_fields(catalog, required):
    for name, (group, spec) in _flat(catalog).items():
        assert required in spec, f"`{name}` (in {group}) missing required field `{required}`"


def test_every_input_mode_is_valid(catalog):
    for name, (_group, spec) in _flat(catalog).items():
        assert spec["input"] in VALID_INPUT_MODES, (
            f"`{name}` has invalid input mode `{spec['input']}` "
            f"(valid: {sorted(VALID_INPUT_MODES)})"
        )


def test_no_typo_keys(catalog):
    """Catch typos like `csv_schemma` or `sync-flag`."""
    for name, (_group, spec) in _flat(catalog).items():
        extra = set(spec.keys()) - VALID_KEYS
        assert not extra, f"`{name}` has unknown keys: {extra}"


def test_file_mode_or_file_or_source_has_csv_schema(catalog):
    """Any command that takes a file MUST declare a csv_schema so the runner
    can validate uploads."""
    for name, (_group, spec) in _flat(catalog).items():
        if spec["input"] in ("file", "file_or_source"):
            assert "csv_schema" in spec, f"`{name}` (input={spec['input']}) needs a csv_schema"
            schema = spec["csv_schema"]
            assert "id_columns" in schema or "required_columns" in schema, (
                f"`{name}` csv_schema needs at least id_columns or required_columns"
            )


def test_sync_flag_values_are_known(catalog):
    """sync_flag must be `--sync`, `-sy`, or None."""
    for name, (_group, spec) in _flat(catalog).items():
        sf = spec.get("sync_flag")
        assert sf in ("--sync", "-sy", None), (
            f"`{name}` has unexpected sync_flag={sf!r}"
        )


def test_args_includes_relevant_flag_pairs(catalog):
    """If sync_flag is set, the corresponding flag must appear in args."""
    for name, (_group, spec) in _flat(catalog).items():
        sf = spec.get("sync_flag")
        if sf:
            args = spec["args"]
            assert sf in args or sf.lstrip("-") in [a.lstrip("-") for a in args], (
                f"`{name}` declares sync_flag={sf!r} but it's not in args={args}"
            )
