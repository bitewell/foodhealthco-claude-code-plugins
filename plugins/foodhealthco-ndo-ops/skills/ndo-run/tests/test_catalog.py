"""Schema-level checks on catalog.yaml.

Every entry exposed to the runner must declare a known `input` mode, a list
of CLI `args`, and a `sync_flag` that's either null or one of `--sync`/`-sy`.
File-input entries (anything that ships a CSV) must declare a `csv_schema`
whose `id_columns` are drawn from the canonical list documented at the top of
catalog.yaml — typo'd `gtin_` would otherwise sail past review and only surface
in production when ndo_run.py silently fails to find an id column.
"""
from pathlib import Path

import pytest
import yaml


CATALOG_PATH = Path(__file__).resolve().parent.parent / "catalog.yaml"

VALID_INPUT_MODES = {"file", "file_or_source", "source", "vendor", "none"}
VALID_SYNC_FLAGS = {None, "--sync", "-sy"}
# Canonical id-column allowlist (catalog.yaml lines 11-13). Extending this set
# requires also teaching extract_ids_for_preflight about the new column.
CANONICAL_ID_COLUMNS = {
    "product_id", "id", "product_match_id",
    "gtin", "upc", "int_upc", "int_gtin",
    "sku",  # added for bulk_create_products (CREATE flow, no IPM id yet)
}


@pytest.fixture(scope="module")
def catalog() -> dict:
    with open(CATALOG_PATH) as f:
        raw = yaml.safe_load(f)
    flat: dict[str, dict] = {}
    for _section, commands in raw.items():
        for name, spec in commands.items():
            flat[name] = spec
    return flat


def test_every_entry_has_required_keys(catalog):
    for name, spec in catalog.items():
        assert "help" in spec, f"{name}: missing `help`"
        assert "input" in spec, f"{name}: missing `input`"
        assert "args" in spec, f"{name}: missing `args`"
        assert isinstance(spec["args"], list), f"{name}: `args` must be a list"
        assert "sync_flag" in spec, f"{name}: missing `sync_flag` (use null if N/A)"


def test_input_modes_are_known(catalog):
    for name, spec in catalog.items():
        assert spec["input"] in VALID_INPUT_MODES, (
            f"{name}: input={spec['input']!r} not in {VALID_INPUT_MODES}"
        )


def test_sync_flags_are_known(catalog):
    for name, spec in catalog.items():
        assert spec["sync_flag"] in VALID_SYNC_FLAGS, (
            f"{name}: sync_flag={spec['sync_flag']!r} not in {VALID_SYNC_FLAGS}"
        )


def test_file_inputs_declare_csv_schema(catalog):
    """Anything that takes a CSV must say which columns it expects.

    Without csv_schema, ndo_run.validate_csv_schema is a no-op and the operator
    gets a confusing manage.py error mid-run instead of a clear preflight failure.
    """
    for name, spec in catalog.items():
        if spec["input"] not in ("file", "file_or_source"):
            continue
        schema = spec.get("csv_schema")
        assert schema is not None, f"{name}: input=file but no csv_schema declared"
        assert (
            "id_columns" in schema or "required_columns" in schema
        ), f"{name}: csv_schema needs at least one of id_columns / required_columns"


def test_id_columns_are_canonical(catalog):
    for name, spec in catalog.items():
        schema = spec.get("csv_schema") or {}
        for col in schema.get("id_columns") or []:
            assert col in CANONICAL_ID_COLUMNS, (
                f"{name}: id_columns has unrecognized `{col}`. "
                f"Extend CANONICAL_ID_COLUMNS in tests AND teach the runner "
                f"about the new column type before adding it here."
            )


def test_bulk_create_products_entry(catalog):
    """Specific guard: the new bulk_create_products entry stays well-formed."""
    assert "bulk_create_products" in catalog, "bulk_create_products entry is missing"
    spec = catalog["bulk_create_products"]
    assert spec["input"] == "file"
    assert spec["sync_flag"] is None
    # -s/--source must appear in args so ndo_run.py forwards --source for an
    # input=file command (see build_manage_cmd source_in_spec_args logic).
    assert "-s" in spec["args"] or "--source" in spec["args"], (
        "bulk_create_products needs -s/--source in args; the runner relies on "
        "this to forward --source for input=file commands"
    )
    schema = spec.get("csv_schema") or {}
    assert "product_name" in (schema.get("required_columns") or [])
    assert "brand_name" in (schema.get("required_columns") or [])
    assert "gtin" in (schema.get("required_columns") or [])
    assert "gtin" in (schema.get("id_columns") or [])
