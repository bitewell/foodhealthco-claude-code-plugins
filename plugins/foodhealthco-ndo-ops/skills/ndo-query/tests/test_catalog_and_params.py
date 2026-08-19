"""Catalog integrity + param coercion tests for the ndo-query runner.

No DB, no proxy, no gcloud — everything here is pure. The catalog checks are
the load-bearing ones: they enforce the invariants that make the skill safe
(bound params only, dietitian-schema-only references, LIMITs present).
"""
import re
from pathlib import Path

import pytest
import yaml

import ndo_query

CATALOG = yaml.safe_load(ndo_query.CATALOG_PATH.read_text())["queries"]


# ---------------------------------------------------------------------------
# Catalog integrity
# ---------------------------------------------------------------------------

def test_catalog_loads_and_is_nonempty():
    assert CATALOG, "queries.yaml must define at least one query"


@pytest.mark.parametrize("name", list(CATALOG))
def test_every_query_has_description_and_sql(name):
    entry = CATALOG[name]
    assert entry.get("description", "").strip()
    assert entry.get("sql", "").strip()


@pytest.mark.parametrize("name", list(CATALOG))
def test_sql_references_only_dietitian_schema(name):
    """Every FROM/JOIN target must be schema-qualified into dietitian.*"""
    sql = CATALOG[name]["sql"]
    for kw, target in re.findall(r"\b(FROM|JOIN)\s+([a-zA-Z_.\"]+)", sql, re.IGNORECASE):
        assert target.startswith("dietitian."), (
            f"{name}: {kw} {target} — catalog queries may only touch the "
            f"dietitian schema"
        )


@pytest.mark.parametrize("name", list(CATALOG))
def test_sql_placeholders_match_declared_params(name):
    entry = CATALOG[name]
    declared = set((entry.get("params") or {}).keys())
    used = set(re.findall(r"%\((\w+)\)s", entry["sql"]))
    assert used == declared, (
        f"{name}: SQL placeholders {sorted(used)} != declared params "
        f"{sorted(declared)}"
    )


@pytest.mark.parametrize("name", list(CATALOG))
def test_no_string_interpolation_vectors(name):
    """Belt-and-braces: no f-string/%s/format-style holes in catalog SQL."""
    sql = CATALOG[name]["sql"]
    assert "{" not in sql and "}" not in sql
    # every % must be part of a named psycopg2 placeholder
    for m in re.finditer(r"%", sql):
        assert re.match(r"%\(\w+\)s", sql[m.start():]), (
            f"{name}: bare % at offset {m.start()} — only %(name)s allowed"
        )


@pytest.mark.parametrize("name", list(CATALOG))
def test_row_queries_have_bound_limit_or_id_filter(name):
    """Every query must bound its result set: a bound LIMIT or an id-list filter."""
    sql = CATALOG[name]["sql"]
    has_limit = re.search(r"LIMIT\s+%\(limit\)s", sql, re.IGNORECASE)
    has_id_filter = "ANY(%(ids)s)" in sql
    assert has_limit or has_id_filter, f"{name}: unbounded result set"


@pytest.mark.parametrize("name", list(CATALOG))
def test_int_params_have_max(name):
    """Un-capped ints are how someone asks for 10M rows — require max."""
    for pname, spec in (CATALOG[name].get("params") or {}).items():
        if spec.get("type") == "int":
            assert "max" in spec, f"{name}.{pname}: int param must declare max"
        if spec.get("type") == "int_list":
            assert "max_items" in spec, f"{name}.{pname}: int_list must declare max_items"


# ---------------------------------------------------------------------------
# Param coercion
# ---------------------------------------------------------------------------

def test_int_list_coercion_and_spaces():
    assert ndo_query.coerce_param("ids", {"type": "int_list", "max_items": 10}, "1, 2,3") == [1, 2, 3]


def test_int_list_rejects_non_int():
    with pytest.raises(SystemExit):
        ndo_query.coerce_param("ids", {"type": "int_list", "max_items": 10}, "1,x")


def test_int_list_rejects_too_many_items():
    with pytest.raises(SystemExit):
        ndo_query.coerce_param("ids", {"type": "int_list", "max_items": 2}, "1,2,3")


def test_int_clamped_to_max(capsys):
    assert ndo_query.coerce_param("limit", {"type": "int", "max": 100}, "5000") == 100


def test_required_param_missing_exits():
    with pytest.raises(SystemExit):
        ndo_query.coerce_param("code", {"type": "str", "required": True}, None)


def test_optional_str_defaults_to_empty_sentinel():
    assert ndo_query.coerce_param("source", {"type": "str", "default": ""}, None) == ""


def test_default_int_used_when_absent():
    assert ndo_query.coerce_param("limit", {"type": "int", "default": 200, "max": 1000}, None) == 200


def test_date_coercion():
    import datetime as dt
    assert ndo_query.coerce_param("since", {"type": "date"}, "2026-08-01") == dt.date(2026, 8, 1)
    with pytest.raises(SystemExit):
        ndo_query.coerce_param("since", {"type": "date"}, "08/01/2026")


# ---------------------------------------------------------------------------
# CLI-level guards (no DB touched: failures happen before any connection)
# ---------------------------------------------------------------------------

def test_unknown_query_exits():
    with pytest.raises(SystemExit):
        ndo_query.main(["definitely_not_a_query"])


def test_unknown_param_exits():
    with pytest.raises(SystemExit):
        ndo_query.main(["score_status", "--param", "nope=1"])


def test_list_works_without_db(capsys):
    assert ndo_query.main(["--list"]) == 0
    out = capsys.readouterr().out
    assert "score_status" in out and "approval_queue" in out


def test_score_qa_covers_the_score_envelope():
    """score_qa is the QA read-out — it must expose the score, the per-nutrient
    norms, the raw macro inputs, and the is_* tag evaluations, so dietitians can
    QA *why* a product scored what it did via the template rather than ad-hoc
    SELECT. A silent drop here would push QA back to raw queries."""
    sql = CATALOG["score_qa"]["sql"]
    for norm in ["sat_fat_norm", "sodium_norm", "added_sugars_norm",
                 "protein_norm", "fiber_to_carb_norm", "unsat_to_sat_fat_norm"]:
        assert norm in sql, f"score_qa missing per-nutrient norm: {norm}"
    for macro in ["calories", "sodium", "added_sugars", "protein", "dietary_fiber"]:
        assert macro in sql, f"score_qa missing macro input: {macro}"
    for tag in ["is_seed_oil", "is_artificial_colors", "is_whole_grain",
                "is_sugars_added", "is_msg"]:
        assert tag in sql, f"score_qa missing tag evaluation: {tag}"
    assert "ingredients_text" in sql
    # guard against silent truncation of the ~40-tag set
    tag_refs = sql.count("pm.is_")
    assert tag_refs >= 35, f"score_qa exposes only {tag_refs} is_* tags (expected ~40)"
