"""
Backend-agnostic parity worker (Phase 1).

Reads corpus records (JSONL) and writes response records (JSONL), one per line,
per ``tools/schema.py``. Dispatch is per-record by ``kind`` (group / mce / convert)
so a single mixed corpus streams through one worker.

    python tools/worker.py --engine oracle --data data/msdrg.mdb < corpus.jsonl > out.jsonl
    python tools/worker.py --engine pure   --data data/msdrg.mdb --in corpus.jsonl --out out.jsonl

Engines:
  * ``oracle`` — the installed Zig-backed ``msdrg`` wheel. MUST be run with the
    oracle venv's interpreter (``$DRGPY_ORACLE_PYTHON`` or the local oracle venv).
    Resolves ``import msdrg`` to site-packages because this file lives in
    ``tools/`` (not a package dir), so the repo source tree never shadows it.
  * ``pure``   — the in-repo ``src/msdrg_pure`` port. Adds ``src/`` to sys.path.
  * ``mock``   — a deterministic synthetic engine (no data file needed) used only
    to self-test the harness plumbing until a real oracle is runnable. It is NOT
    a DRG model; it just emits full-shape, reproducible outputs.

Files are read/written as UTF-8 explicitly (Windows default is cp1252). Output is
flushed in batches so a reader can stream results.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "tools"))
import schema  # noqa: E402  (local import after path setup)


# ---------------------------------------------------------------------------
# Engine adapters — each exposes group(claim)/mce(claim)/convert(input) -> dict
# and may raise; the driver turns exceptions into error records.
# ---------------------------------------------------------------------------


class OracleEngine:
    name = "oracle"

    def __init__(self, data_path: str) -> None:
        import msdrg  # resolved to the installed wheel

        if "site-packages" not in (msdrg.__file__ or ""):
            raise RuntimeError(
                f"oracle 'import msdrg' resolved to {msdrg.__file__!r}, not the "
                "installed wheel — refusing to run (Ground Rule 2: oracle is sealed)."
            )
        self._msdrg = msdrg
        self._g = msdrg.MsdrgGrouper(data_dir=data_path)
        self._m = msdrg.MceEditor(data_dir=data_path)
        self._c = msdrg.IcdConverter(data_dir=data_path)

    def group(self, claim: dict) -> dict:
        return self._g.group(claim)

    def mce(self, claim: dict) -> dict:
        return self._m.edit(claim)

    def convert(self, inp: dict) -> dict:
        fn = self._c.convert_dx if inp["code_type"] == "dx" else self._c.convert_pr
        return {"converted": fn(inp["code"], inp["source_year"], inp["target_year"])}

    def close(self) -> None:
        for obj in (self._g, self._m, self._c):
            try:
                obj.close()
            except Exception:
                pass


class PureEngineAdapter:
    name = "pure"

    def __init__(self, data_path: str) -> None:
        sys.path.insert(0, str(REPO / "src"))
        import msdrg_pure

        self._pkg = msdrg_pure
        self._g = msdrg_pure.MsdrgGrouper(data_dir=data_path)
        self._m = msdrg_pure.MceEditor(data_dir=data_path)
        self._c = msdrg_pure.IcdConverter(data_dir=data_path)

    def group(self, claim: dict) -> dict:
        return self._g.group(claim)

    def mce(self, claim: dict) -> dict:
        return self._m.edit(claim)

    def convert(self, inp: dict) -> dict:
        fn = self._c.convert_dx if inp["code_type"] == "dx" else self._c.convert_pr
        return {"converted": fn(inp["code"], inp["source_year"], inp["target_year"])}

    def close(self) -> None:
        for obj in (self._g, self._m, self._c):
            try:
                obj.close()
            except Exception:
                pass


class MockEngine:
    """Deterministic synthetic engine — proves harness wiring, not DRG logic.

    Every output is a pure function of the input (stable hashing), so two mock
    workers over the same corpus produce byte-identical responses. This lets the
    orchestrator demonstrate an end-to-end, deterministic engine-vs-engine run
    while the real oracle is EDR-blocked (PORTING_NOTES §9.1).
    """

    name = "mock"

    def __init__(self, data_path: str | None = None) -> None:
        self.data_path = data_path

    @staticmethod
    def _h(*parts: object) -> int:
        s = "|".join(str(p) for p in parts)
        return int.from_bytes(hashlib.sha256(s.encode()).digest()[:4], "big")

    def group(self, claim: dict) -> dict:
        pdx = (claim.get("pdx") or {}).get("code", "")
        ver = claim.get("version", 0)
        base = 1 + self._h("drg", pdx, ver) % 999
        mdc = 1 + self._h("mdc", pdx) % 25
        sev = schema.SEVERITIES[self._h("sev", pdx) % 3]
        rc = schema.RETURN_CODES[self._h("rc", pdx, ver) % len(schema.RETURN_CODES)]

        def dx_out(code: str, poa: str | None) -> dict:
            return {
                "code": code,
                "mdc": mdc,
                "severity": schema.SEVERITIES[self._h("dxsev", code) % 3],
                "drg_impact": "AFFECTS_DRG" if self._h("imp", code) % 2 else "NONE",
                "poa_error": "NONE",
                "flags": ["VALID"] + (["AFFECTS_DRG"] if self._h("f", code) % 2 else []),
                "hacs": [],
            }

        sdx = claim.get("sdx", []) or []
        procs = claim.get("procedures", []) or []
        return {
            "initial_drg": base,
            "initial_mdc": mdc,
            "initial_base_drg": base,
            "initial_drg_description": f"MOCK DRG {base}",
            "initial_mdc_description": f"MOCK MDC {mdc}",
            "initial_return_code": rc,
            "initial_severity": sev,
            "final_drg": base,
            "final_mdc": mdc,
            "final_base_drg": base,
            "final_drg_description": f"MOCK DRG {base}",
            "final_mdc_description": f"MOCK MDC {mdc}",
            "return_code": rc,
            "final_severity": sev,
            "pdx_output": dx_out(pdx, (claim.get("pdx") or {}).get("poa")),
            "sdx_output": [dx_out(d.get("code", ""), d.get("poa")) for d in sdx],
            "proc_output": [
                {
                    "code": p.get("code", ""),
                    "is_or": bool(self._h("or", p.get("code", "")) % 2),
                    "drg_impact": "NONE",
                    "flags": [],
                    "hac_usage": [],
                }
                for p in procs
            ],
            "grouper_flags": {
                "admit_dx_grouper_flag": "DX_NOT_GIVEN",
                "initial_drg_secondary_dx_cc_mcc": sev,
                "final_drg_secondary_dx_cc_mcc": sev,
                "num_hac_categories_satisfied": 0,
                "hac_status_value": "NOT_APPLICABLE",
            },
        }

    def mce(self, claim: dict) -> dict:
        pdx = (claim.get("pdx") or {}).get("code", "")
        fires = self._h("mce", pdx) % 3 == 0
        edits = []
        et = "NONE"
        if fires:
            name = schema.MCE_ALL_EDITS[self._h("edit", pdx) % len(schema.MCE_ALL_EDITS)]
            et = "PREPAYMENT"
            edits = [{"name": name, "count": 1, "code_type": "DIAGNOSIS", "edit_type": et}]
        return {"version": 20260930, "edit_type": et, "edits": edits}

    def convert(self, inp: dict) -> dict:
        code = inp["code"].upper().replace(".", "")[:8]
        return {"converted": code}

    def close(self) -> None:
        pass


ENGINES = {"oracle": OracleEngine, "pure": PureEngineAdapter, "mock": MockEngine}


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


def run(engine, fin: io.TextIOBase, fout: io.TextIOBase) -> tuple[int, int]:
    ok = err = 0
    flush_every = 2000
    for i, line in enumerate(fin, 1):
        line = line.strip()
        if not line:
            continue
        rec = json.loads(line)
        schema.validate_record(rec)
        rid, kind, inp = rec["id"], rec["kind"], rec["input"]
        try:
            if kind == "group":
                out = engine.group(inp)
            elif kind == "mce":
                out = engine.mce(inp)
            else:
                out = engine.convert(inp)
            resp = schema.make_ok(rid, kind, schema.normalize_output(kind, out))
            ok += 1
        except Exception as exc:  # noqa: BLE001 — exceptions are part of parity
            resp = schema.make_err(rid, kind, exc)
            err += 1
        fout.write(json.dumps(resp, ensure_ascii=False, sort_keys=True) + "\n")
        if i % flush_every == 0:
            fout.flush()
    fout.flush()
    return ok, err


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="parity worker")
    ap.add_argument("--engine", required=True, choices=list(ENGINES))
    ap.add_argument("--data", default=str(REPO / "data" / "msdrg.mdb"),
                    help="path to msdrg.mdb (dir or file)")
    ap.add_argument("--in", dest="infile", default=None, help="input JSONL (default stdin)")
    ap.add_argument("--out", dest="outfile", default=None, help="output JSONL (default stdout)")
    args = ap.parse_args(argv)

    engine_cls = ENGINES[args.engine]
    engine = engine_cls() if args.engine == "mock" else engine_cls(args.data)

    fin = (open(args.infile, "r", encoding="utf-8") if args.infile
           else io.TextIOWrapper(sys.stdin.buffer, encoding="utf-8"))
    fout = (open(args.outfile, "w", encoding="utf-8", newline="\n") if args.outfile
            else io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", newline="\n"))
    try:
        ok, err = run(engine, fin, fout)
    finally:
        engine.close()
        if args.infile:
            fin.close()
        if args.outfile:
            fout.close()
    print(f"[worker:{args.engine}] ok={ok} err={err}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
