"""
Seeded corpus generator for the parity harness (Phase 1).

Emits JSONL corpus records (``tools/schema.py`` shape) covering the three engine
kinds. Code universes are bootstrapped from the SQLite export ``../msdrg.db``
(NOT from the oracle — its DLL is EDR-blocked, PORTING_NOTES §9.1). Everything is
deterministic given ``(seed, sqlite bytes)``; a sidecar ``<out>.meta.json`` records
the seed, per-kind counts, the SQLite sha256, universe sizes, and generator params
so any corpus is byte-identically regenerable (Ground Rule 6).

Corpora (``--kind``):
  * ``random``    — uniform random valid claims over the per-version code universes
  * ``sweeps``    — structured single-dimension sweeps (age/sex/discharge/POA/counts) x 9 versions
  * ``malformed`` — blank/invalid/truncated/lowercase/dotted/duplicate/over-limit/missing
  * ``smoke``     — small deterministic mix of all three (rig proof + CI smoke)
  * ``full``      — random(N) + all sweeps + all malformed

Usage:
  python tools/gen_corpus.py --kind smoke --out tests/fixtures/corpus/smoke.jsonl
  python tools/gen_corpus.py --kind full  --n 50000 --seed 42 --out tests/fixtures/corpus/full.jsonl
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import sqlite3
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "tools"))
import schema  # noqa: E402

# The SQLite export lives ONE LEVEL ABOVE the repo (Phase-0 §8.1).
DEFAULT_SQLITE = REPO.parent / "msdrg.db"

# A valid MCE discharge_date per fiscal year (mid-FY, within 20000101..21001231).
FY_DISCHARGE_DATE = {2023: 20230115, 2024: 20240115, 2025: 20250115,
                     2026: 20260115, 2027: 20270115}
VERSION_TO_YEAR = {400: 2023, 401: 2023, 410: 2024, 411: 2024, 420: 2025,
                   421: 2025, 430: 2026, 431: 2026, 440: 2027}

POA_VALUES = ["Y", "N", "U", "W", " ", None]  # engine-valid set (validation allows these)
POA_INVALID = ["1", "0", "X", "y"]  # rejected by _validation -> ValueError parity


# ---------------------------------------------------------------------------
# Code universes (from SQLite; deterministic, sorted)
# ---------------------------------------------------------------------------


class Universe:
    def __init__(self, sqlite_path: Path) -> None:
        self.path = sqlite_path
        self._c = sqlite3.connect(f"file:{sqlite_path}?mode=ro", uri=True)
        self._dx: dict[int, list[str]] = {}
        self._pr: dict[int, list[str]] = {}
        self._mce: dict[tuple[str, str], list[str]] = {}
        self._ds: list[int] | None = None
        self._conv: dict[str, list[str]] = {}
        self.sha256 = schema.sha256_file(str(sqlite_path))

    def dx_for(self, version: int) -> list[str]:
        if version not in self._dx:
            rows = self._c.execute(
                "select key from diagnosisAll where version_start<=? and version_end>=? order by key",
                (version, version),
            ).fetchall()
            self._dx[version] = [r[0] for r in rows]
        return self._dx[version]

    def pr_for(self, version: int) -> list[str]:
        if version not in self._pr:
            rows = self._c.execute(
                "select key from procedureAttributes where version_start<=? and version_end>=? order by key",
                (version, version),
            ).fetchall()
            self._pr[version] = [r[0] for r in rows]
        return self._pr[version]

    def _mce_codes(self, table: str, system: str) -> list[str]:
        key = (table, system)
        if key not in self._mce:
            rows = self._c.execute(
                f'select code from "{table}" where code_system=? order by code', (system,)
            ).fetchall()
            self._mce[key] = [r[0] for r in rows]
        return self._mce[key]

    def mce_dx(self, icd_version: int) -> list[str]:
        return self._mce_codes("mce_diagnosis_codes", "ICD10CM" if icd_version == 10 else "ICD9CM")

    def mce_pr(self, icd_version: int) -> list[str]:
        return self._mce_codes("mce_procedure_codes", "ICD10PCS" if icd_version == 10 else "ICD9SG")

    def discharge_statuses(self) -> list[int]:
        if self._ds is None:
            rows = self._c.execute(
                "select distinct code from mce_discharge_status order by code").fetchall()
            self._ds = [r[0] for r in rows]
        return self._ds

    def convert_sources(self, code_type: str) -> list[str]:
        if code_type not in self._conv:
            sys_ = "ICD10CM" if code_type == "dx" else "ICD10PCS"
            rows = self._c.execute(
                "select distinct current_code from icd_conversions where code_type=? "
                "order by current_code", (sys_,),
            ).fetchall()
            self._conv[code_type] = [r[0] for r in rows]
        return self._conv[code_type]

    def sizes(self) -> dict:
        return {
            "dx_v400": len(self.dx_for(400)),
            "dx_v440": len(self.dx_for(440)),
            "pr_v440": len(self.pr_for(440)),
            "mce_dx10": len(self.mce_dx(10)),
            "mce_dx9": len(self.mce_dx(9)),
            "discharge_statuses": len(self.discharge_statuses()),
        }


# ---------------------------------------------------------------------------
# Claim builders
# ---------------------------------------------------------------------------


def _dx(code: str, poa: str | None) -> dict:
    d = {"code": code}
    if poa is not None:
        d["poa"] = poa
    return d


def _random_group_claim(rng: random.Random, u: Universe) -> dict:
    version = rng.choice(schema.ALL_VERSIONS)
    dxs = u.dx_for(version)
    prs = u.pr_for(version)
    n_sdx = rng.randint(0, 8)
    n_pr = rng.randint(0, 5)
    claim = {
        "version": version,
        "age": rng.randint(0, 100),
        "sex": rng.choice([0, 1, 2]),
        "discharge_status": rng.choice(u.discharge_statuses()),
        "pdx": _dx(rng.choice(dxs), rng.choice(POA_VALUES)),
        "sdx": [_dx(rng.choice(dxs), rng.choice(POA_VALUES)) for _ in range(n_sdx)],
        "procedures": [{"code": rng.choice(prs)} for _ in range(n_pr)],
    }
    if rng.random() < 0.15:
        # exercise the client-side ICD conversion path
        yr = VERSION_TO_YEAR[version]
        other = rng.choice([y for y in FY_DISCHARGE_DATE if y != yr])
        claim["source_icd_version"] = other
    if rng.random() < 0.3:
        claim["hospital_status"] = rng.choice(["EXEMPT", "NOT_EXEMPT", "UNKNOWN"])
    if rng.random() < 0.3:
        claim["tie_breaker"] = rng.choice(["CLINICAL_SIGNIFICANCE", "ALPHABETICAL"])
    return claim


def _random_mce_claim(rng: random.Random, u: Universe) -> dict:
    icd = rng.choice([10, 10, 10, 9])  # mostly ICD-10
    dxs = u.mce_dx(icd)
    prs = u.mce_pr(icd)
    year = rng.choice(list(FY_DISCHARGE_DATE))
    claim = {
        "discharge_date": FY_DISCHARGE_DATE[year],
        "icd_version": icd,
        "age": rng.randint(0, 100),
        "sex": rng.choice([0, 1, 2]),
        "discharge_status": rng.choice(u.discharge_statuses()),
        "pdx": _dx(rng.choice(dxs), rng.choice(POA_VALUES)),
        "sdx": [_dx(rng.choice(dxs), rng.choice(POA_VALUES)) for _ in range(rng.randint(0, 6))],
        "procedures": [{"code": rng.choice(prs)} for _ in range(rng.randint(0, 4))] if prs else [],
    }
    return claim


def _random_convert_input(rng: random.Random, u: Universe) -> dict:
    code_type = rng.choice(["dx", "pr"])
    src = u.convert_sources(code_type)
    year = rng.choice(list(FY_DISCHARGE_DATE))
    # adjacent-year hop (converter only supports adjacent years)
    direction = rng.choice([-1, 1])
    target = year + direction
    if target not in FY_DISCHARGE_DATE:
        target = year - direction
    return {
        "code": rng.choice(src),
        "code_type": code_type,
        "source_year": year,
        "target_year": target,
    }


# ---------------------------------------------------------------------------
# Generators (each yields corpus records)
# ---------------------------------------------------------------------------


def gen_random(u: Universe, n: int, seed: int):
    rng = random.Random(seed)
    for i in range(n):
        roll = rng.random()
        if roll < 0.7:
            yield schema.make_record(f"rand-g-{i}", "group", _random_group_claim(rng, u))
        elif roll < 0.9:
            yield schema.make_record(f"rand-m-{i}", "mce", _random_mce_claim(rng, u))
        else:
            yield schema.make_record(f"rand-c-{i}", "convert", _random_convert_input(rng, u))


def _canonical_claim(u: Universe, version: int) -> dict:
    dxs = u.dx_for(version)
    prs = u.pr_for(version)
    return {
        "version": version,
        "age": 65,
        "sex": 0,
        "discharge_status": 1,
        "pdx": {"code": dxs[len(dxs) // 2]},
        "sdx": [{"code": dxs[len(dxs) // 3]}],
        "procedures": [{"code": prs[len(prs) // 2]}] if prs else [],
    }


def gen_sweeps(u: Universe):
    ages = [0, 1, 17, 18, 64, 65, 89, 124, 125, -1]
    sexes = [0, 1, 2, 3]  # 3 is invalid -> ValueError parity
    poa_all = POA_VALUES + POA_INVALID
    statuses = u.discharge_statuses() + [0, 999]  # 0/999 exercise invalid-status path
    for version in schema.ALL_VERSIONS:
        base = _canonical_claim(u, version)
        for a in ages:
            c = dict(base); c["age"] = a
            yield schema.make_record(f"sweep-age-{version}-{a}", "group", c)
        for s in sexes:
            c = dict(base); c["sex"] = s
            yield schema.make_record(f"sweep-sex-{version}-{s}", "group", c)
        for ds in statuses:
            c = dict(base); c["discharge_status"] = ds
            yield schema.make_record(f"sweep-ds-{version}-{ds}", "group", c)
        for p in poa_all:
            c = dict(base); c["pdx"] = _dx(base["pdx"]["code"], p)
            tag = "none" if p is None else ("space" if p == " " else p)
            yield schema.make_record(f"sweep-poa-{version}-{tag}", "group", c)
        dxs = u.dx_for(version)
        for k in (0, 1, 2, 5, 10, 20, 30):
            c = dict(base)
            c["sdx"] = [{"code": dxs[(i * 97) % len(dxs)]} for i in range(k)]
            yield schema.make_record(f"sweep-nsdx-{version}-{k}", "group", c)
        prs = u.pr_for(version)
        for k in (0, 1, 2, 5, 10, 20, 30):
            c = dict(base)
            c["procedures"] = [{"code": prs[(i * 97) % len(prs)]} for i in range(k)] if prs else []
            yield schema.make_record(f"sweep-nproc-{version}-{k}", "group", c)
        # MCE discharge-date + icd_version sweeps
        yr = VERSION_TO_YEAR[version]
        for icd in (10, 9):
            mce = {
                "discharge_date": FY_DISCHARGE_DATE[yr],
                "icd_version": icd,
                "age": 65, "sex": 0, "discharge_status": 1,
                "pdx": {"code": u.mce_dx(icd)[0]},
                "sdx": [], "procedures": [],
            }
            yield schema.make_record(f"sweep-mce-{version}-icd{icd}", "mce", mce)


def gen_malformed(u: Universe):
    v = 431
    dxs = u.dx_for(v)
    good = dxs[len(dxs) // 2]
    base = {"version": v, "age": 65, "sex": 0, "discharge_status": 1}

    def g(idx, claim):
        return schema.make_record(f"bad-g-{idx}", "group", claim)

    cases: list[dict] = [
        {**base, "pdx": {"code": ""}},                       # blank code
        {**base, "pdx": {"code": "   "}},                    # whitespace code
        {**base, "pdx": {"code": "ZZZZZ"}},                  # invalid code
        {**base, "pdx": {"code": "123"}},                    # numeric-ish invalid
        {**base, "pdx": {"code": good[:2]}},                 # truncated
        {**base, "pdx": {"code": good.lower()}},             # lowercase
        {**base, "pdx": {"code": good[:3] + "." + good[3:]}},  # dotted
        {**base, "pdx": {"code": good}, "sdx": [{"code": good}, {"code": good}]},  # duplicate sdx
        {**base, "pdx": {"code": good}, "sdx": [{"code": good}] * 100},  # over-limit sdx
        {**base, "pdx": {"code": good}, "procedures": [{"code": u.pr_for(v)[0]}] * 100},  # over-limit proc
        {"pdx": {"code": good}},                             # missing all demographics
        {**base},                                            # missing pdx -> ValueError
        {**base, "pdx": {"code": good}, "sex": 5},           # invalid sex -> ValueError
        {**base, "pdx": {"code": good}, "age": 3.5},         # non-int age -> ValueError
        {**base, "pdx": "I5033"},                            # pdx not a dict -> ValueError
        {**base, "pdx": {"nocode": 1}},                      # pdx missing 'code' -> ValueError
        {**base, "pdx": {"code": 123}},                      # code not str -> ValueError
        {**base, "pdx": {"code": good, "poa": "1"}},         # invalid poa -> ValueError
        {**base, "pdx": {"code": good}, "hospital_status": "BOGUS"},  # bad enum -> ValueError
        {**base, "pdx": {"code": good}, "tie_breaker": "BOGUS"},      # bad enum -> ValueError
    ]
    for i, c in enumerate(cases):
        yield g(i, c)

    # MCE-specific malformed
    mce_base = {"age": 65, "sex": 0, "discharge_status": 1, "pdx": {"code": good}}
    mce_cases = [
        {**mce_base},                                        # missing discharge_date -> ValueError
        {**mce_base, "discharge_date": 19990101},            # date too early -> ValueError
        {**mce_base, "discharge_date": 21010101},            # date too late -> ValueError
        {**mce_base, "discharge_date": 20250101, "icd_version": 8},  # bad icd_version -> ValueError
        {**mce_base, "discharge_date": 20250101, "pdx": {"code": "ZZZZZ"}},  # invalid code -> engine
    ]
    for i, c in enumerate(mce_cases):
        yield schema.make_record(f"bad-m-{i}", "mce", c)


def gen_smoke(u: Universe, seed: int):
    # Small deterministic mix: 1000 random + all sweeps + all malformed.
    yield from gen_random(u, 1000, seed)
    yield from gen_sweeps(u)
    yield from gen_malformed(u)


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


def write_corpus(records, out_path: Path) -> dict:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    counts: dict[str, int] = {}
    h = hashlib.sha256()
    with open(out_path, "w", encoding="utf-8", newline="\n") as f:
        for rec in records:
            counts[rec["kind"]] = counts.get(rec["kind"], 0) + 1
            line = json.dumps(rec, ensure_ascii=False, sort_keys=True) + "\n"
            f.write(line)
            h.update(line.encode("utf-8"))
    counts["total"] = sum(counts.values())
    counts["corpus_sha256"] = h.hexdigest()
    return counts


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="seeded parity corpus generator")
    ap.add_argument("--kind", required=True,
                    choices=["random", "sweeps", "malformed", "smoke", "full"])
    ap.add_argument("--out", required=True)
    ap.add_argument("--n", type=int, default=50000, help="random-claim count (random/full)")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--sqlite", default=str(DEFAULT_SQLITE))
    args = ap.parse_args(argv)

    sqlite_path = Path(args.sqlite)
    if not sqlite_path.exists():
        raise SystemExit(f"SQLite export not found: {sqlite_path} (needed for code universes)")
    u = Universe(sqlite_path)

    if args.kind == "random":
        records = gen_random(u, args.n, args.seed)
    elif args.kind == "sweeps":
        records = gen_sweeps(u)
    elif args.kind == "malformed":
        records = gen_malformed(u)
    elif args.kind == "smoke":
        records = gen_smoke(u, args.seed)
    else:  # full
        def _full():
            yield from gen_random(u, args.n, args.seed)
            yield from gen_sweeps(u)
            yield from gen_malformed(u)
        records = _full()

    out_path = Path(args.out)
    counts = write_corpus(records, out_path)

    meta = {
        "schema_version": schema.SCHEMA_VERSION,
        "kind": args.kind,
        "seed": args.seed,
        "n_random": args.n if args.kind in ("random", "full") else 0,
        "counts": counts,
        "sqlite_path": str(sqlite_path),
        "sqlite_sha256": u.sha256,
        "universe_sizes": u.sizes(),
        "generated_by": "tools/gen_corpus.py",
    }
    meta_path = out_path.with_suffix(out_path.suffix + ".meta.json")
    with open(meta_path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(meta, f, indent=2, sort_keys=True)
        f.write("\n")

    print(f"wrote {counts['total']} records -> {out_path}")
    print(f"  by kind: {{'group': {counts.get('group',0)}, 'mce': {counts.get('mce',0)}, "
          f"'convert': {counts.get('convert',0)}}}")
    print(f"  corpus sha256: {counts['corpus_sha256']}")
    print(f"  meta -> {meta_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
