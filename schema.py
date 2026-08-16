"""
Canonical schemas for the parity harness (Phase 1).

Defines the wire protocol shared by every component:

  corpus record (one JSON object per line, fed to a worker on stdin)
      {"id": <str>, "kind": "group"|"mce"|"convert", "input": {...}}

  worker response (one JSON object per line, emitted on stdout)
      {"id": <str>, "kind": ..., "ok": true,  "output": {...}}
      {"id": <str>, "kind": ..., "ok": false, "error": {"type": <str>, "message": <str>}}

Also declares the FULL output field surface both engines emit (grouper, MCE,
converter) so the harness can assert the schema covers everything (Phase-1 DoD:
"schemas cover the full output surface"), and a ``normalize_output`` that maps to
plain JSON types WITHOUT ever reordering lists (Ground Rule 8).

Field lists are transcribed from ``docs/ARCHITECTURE_INVENTORY.md`` §3.2/§3.3 and
the public TypedDicts in ``msdrg/grouper.py`` / ``msdrg/mce.py``.
"""

from __future__ import annotations

import hashlib
from typing import Any

SCHEMA_VERSION = 1

# The single reference data file — both engines MUST read these exact bytes
# (Ground Rule 3). Pinned in Phase 0 (PORTING_NOTES §4 / ARCHITECTURE_INVENTORY §8.1).
PINNED_MDB_SHA256 = "534c166cb2c0f78420a7691c51890432139862f1667e2edfc6374852612b2744"

KINDS = ("group", "mce", "convert")


# ---------------------------------------------------------------------------
# Full output surface (for schema-coverage assertions)
# ---------------------------------------------------------------------------

GROUP_RESULT_FIELDS = (
    "initial_drg",
    "initial_mdc",
    "initial_base_drg",
    "initial_drg_description",
    "initial_mdc_description",
    "initial_return_code",
    "initial_severity",
    "final_drg",
    "final_mdc",
    "final_base_drg",
    "final_drg_description",
    "final_mdc_description",
    "return_code",
    "final_severity",
    "pdx_output",
    "sdx_output",
    "proc_output",
    "grouper_flags",
    "conversions",
)

DIAGNOSIS_OUTPUT_FIELDS = (
    "code",
    "mdc",
    "severity",
    "drg_impact",
    "poa_error",
    "flags",
    "hacs",
)

HAC_OUTPUT_FIELDS = ("hac_number", "hac_list", "hac_status", "description")

PROCEDURE_OUTPUT_FIELDS = ("code", "is_or", "drg_impact", "flags", "hac_usage")

GROUPER_FLAGS_FIELDS = (
    "admit_dx_grouper_flag",
    "initial_drg_secondary_dx_cc_mcc",
    "final_drg_secondary_dx_cc_mcc",
    "num_hac_categories_satisfied",
    "hac_status_value",
)

CODE_CONVERSION_FIELDS = ("original", "converted", "code_type", "field")

MCE_RESULT_FIELDS = ("version", "edit_type", "edits")
MCE_EDIT_DETAIL_FIELDS = ("name", "count", "code_type", "edit_type")

CONVERT_RESULT_FIELDS = ("converted",)

# Enum value domains (JSON @tagName strings) — the port matches names, not ordinals.
RETURN_CODES = (
    "OK",
    "INVALID_PRINCIPAL_DIAGNOSIS",
    "INVALID_AGE",
    "INVALID_SEX",
    "INVALID_DISCHARGE_STATUS",
    "DX_CANNOT_BE_PDX",
    "UNGROUPABLE",
    "INVALID_PDX",
    "HAC_MISSING_ONE_POA",
    "HAC_STATUS_INVALID_MULT_HACS_POA_NOT_Y_W",
    "HAC_STATUS_INVALID_POA_N_OR_U",
    "HAC_STATUS_INVALID_POA_INVALID_OR_1",
)
SEVERITIES = ("NONE", "CC", "MCC")
MCE_EDIT_TYPES = ("NONE", "PREPAYMENT", "POSTPAYMENT", "BOTH", "INVALID_DISCHARGE_DATE")

# The 36 MCE edits in ALL_EDITS index order (mce_json_api.zig:138-178).
MCE_ALL_EDITS = (
    "INVALID_CODE",
    "SEX_CONFLICT",
    "AGE_CONFLICT",
    "QUESTIONABLE_ADMISSION",
    "MANIFESTATION_AS_PDX",
    "NONSPECIFIC_PDX",
    "E_CODE_AS_PDX",
    "UNACCEPTABLE_PDX",
    "DUPLICATE_OF_PDX",
    "MEDICARE_IS_SECONDARY_PAYER",
    "REQUIRES_SDX",
    "NONSPECIFIC_OR",
    "OPEN_BIOPSY",
    "NON_COVERED",
    "BILATERAL",
    "LIMITED_COVERAGE_LVRS",
    "LIMITED_COVERAGE",
    "LIMITED_COVERAGE_LUNG_TRANSPLANT",
    "QUESTIONABLE_OBSTETRIC_ADMISSION",
    "LIMITED_COVERAGE_COMBINATION_HEART_LUNG",
    "LIMITED_COVERAGE_HEART_TRANSPLANT",
    "LIMITED_COVERAGE_HEART_IMPLANT",
    "LIMITED_COVERAGE_INTESTINE",
    "LIMITED_COVERAGE_LIVER",
    "INVALID_ADMIT_DX",
    "INVALID_AGE",
    "INVALID_SEX",
    "INVALID_DISCHARGE_STATUS",
    "LIMITED_COVERAGE_KIDNEY",
    "LIMITED_COVERAGE_PANCREAS",
    "TYPE_OF_AGE_CONFLICT",  # declared but never fired
    "INVALID_POA",  # declared but never fired
    "LIMITED_COVERAGE_ARTIFICIAL_HEART",  # declared but never fired
    "WRONG_PROCEDURE_PERFORMED",
    "INCONSISTENT_WITH_LENGTH_OF_STAY",
    "UNSPECIFIED",
)

ALL_VERSIONS = (400, 401, 410, 411, 420, 421, 430, 431, 440)


# ---------------------------------------------------------------------------
# Record / response construction + light validation
# ---------------------------------------------------------------------------


def make_record(id: str, kind: str, input: dict[str, Any]) -> dict[str, Any]:
    if kind not in KINDS:
        raise ValueError(f"unknown kind {kind!r}; expected one of {KINDS}")
    return {"id": id, "kind": kind, "input": input}


def make_ok(id: str, kind: str, output: dict[str, Any]) -> dict[str, Any]:
    return {"id": id, "kind": kind, "ok": True, "output": output}


def make_err(id: str, kind: str, exc: BaseException) -> dict[str, Any]:
    return {
        "id": id,
        "kind": kind,
        "ok": False,
        "error": {"type": type(exc).__name__, "message": str(exc)},
    }


def validate_record(rec: dict[str, Any]) -> None:
    """Raise ValueError if *rec* is not a well-formed corpus record."""
    for key in ("id", "kind", "input"):
        if key not in rec:
            raise ValueError(f"record missing {key!r}: {rec!r}")
    if rec["kind"] not in KINDS:
        raise ValueError(f"record has unknown kind {rec['kind']!r}")
    if not isinstance(rec["input"], dict):
        raise ValueError(f"record 'input' must be an object, got {type(rec['input']).__name__}")


# ---------------------------------------------------------------------------
# Normalization (JSON types only — never reorders lists)
# ---------------------------------------------------------------------------


def normalize_output(kind: str, output: Any) -> Any:
    """Canonicalize an engine output to comparable JSON types.

    The engines already emit JSON, so this is near-identity. Its only jobs:
      * wrap the converter's bare string result as ``{"converted": <str>}`` so
        all three kinds share the "output is an object" shape;
      * leave every list order untouched (Ground Rule 8).
    """
    if kind == "convert":
        if isinstance(output, dict):
            return output
        return {"converted": output}
    return output


# ---------------------------------------------------------------------------
# Data-pin helper
# ---------------------------------------------------------------------------


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def assert_data_pin(path: str) -> str:
    """Verify *path* is the pinned msdrg.mdb; return its sha256. Raises on drift."""
    digest = sha256_file(path)
    if digest != PINNED_MDB_SHA256:
        raise SystemExit(
            f"DATA PIN MISMATCH for {path}\n  expected {PINNED_MDB_SHA256}\n  got      {digest}\n"
            "Both engines must read the pinned msdrg.mdb (Ground Rule 3)."
        )
    return digest
