"""
Parity harness orchestrator (Phase 1).

Ties the pieces together: resolve a corpus, run it through two engines (each an
isolated ``worker.py`` subprocess, or a pre-computed ``frozen:`` output file),
diff the two response streams, and print a parity report.

Engine specs (``--a`` / ``--b``):
  pure          in-repo src/msdrg_pure, run with THIS interpreter
  oracle        installed Zig wheel, run with the oracle venv interpreter
                ($DRGPY_ORACLE_PYTHON or the local %LOCALAPPDATA%\\drgpy oracle venv)
  mock          deterministic synthetic engine (rig self-test; no data file)
  frozen:PATH   use PATH as engine responses verbatim (drop-in for an oracle run
                produced on another machine — the EDR-blocked-oracle escape hatch)

Subcommands:
  gen        thin wrapper over tools/gen_corpus.py
  run        --a <eng> --b <eng> --corpus <name|path>
  selftest   run every oracle-independent proof of the rig (Phase-1 evidence)

Commands (equivalently via the Makefile):
  python tools/parity.py run --a mock --b mock --corpus smoke
  python tools/parity.py run --a pure --b oracle --corpus full
  python tools/parity.py selftest
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO))
import gen_corpus  # noqa: E402
import parity_diff  # noqa: E402
import schema  # noqa: E402

WORKER = REPO / "worker.py"
DATA = REPO / "data" / "msdrg.mdb"
CORPUS_DIR = REPO / "tests" / "fixtures" / "corpus"
RUN_DIR = REPO / "tests" / "fixtures" / "_runs"

# Candidate oracle interpreters, in priority order (see PORTING_NOTES §9.1).
_ORACLE_VENVS = [
    Path(os.environ["LOCALAPPDATA"]) / "drgpy" / "oracle-venv" / "Scripts" / "python.exe"
    if os.environ.get("LOCALAPPDATA") else None,
    REPO.parent / ".oracle-venv" / "Scripts" / "python.exe",
]


def resolve_interpreter(engine: str) -> str:
    if engine in ("pure", "mock"):
        return sys.executable
    if engine == "oracle":
        env = os.environ.get("DRGPY_ORACLE_PYTHON")
        if env and Path(env).exists():
            return env
        for cand in _ORACLE_VENVS:
            if cand and cand.exists():
                return str(cand)
        raise SystemExit(
            "oracle interpreter not found. Set $DRGPY_ORACLE_PYTHON to the oracle "
            "venv python, or install msdrg into a venv. (Note: the oracle DLL is "
            "currently EDR-blocked on this host — see PORTING_NOTES §9.1.)"
        )
    raise SystemExit(f"unknown engine {engine!r}")


def resolve_corpus(name_or_path: str, seed: int = 42) -> Path:
    """A known name (smoke/full/…) -> tests/fixtures/corpus/<name>.jsonl (auto-gen
    smoke if missing); otherwise treat as a direct path."""
    p = Path(name_or_path)
    if p.exists():
        return p
    named = CORPUS_DIR / f"{name_or_path}.jsonl"
    if named.exists():
        return named
    if name_or_path == "smoke":
        print(f"[parity] smoke corpus missing; generating -> {named}")
        gen_corpus.main(["--kind", "smoke", "--out", str(named), "--seed", str(seed)])
        return named
    raise SystemExit(f"corpus not found: {name_or_path} (looked for {p} and {named})")


def run_engine(engine: str, corpus: Path, out_dir: Path) -> Path:
    """Produce a responses JSONL for *engine* over *corpus*; return its path."""
    out_dir.mkdir(parents=True, exist_ok=True)
    if engine.startswith("frozen:"):
        src = Path(engine.split(":", 1)[1])
        if not src.exists():
            raise SystemExit(f"frozen responses not found: {src}")
        return src
    out = out_dir / f"{corpus.stem}.{engine}.jsonl"
    interp = resolve_interpreter(engine)
    cmd = [interp, str(WORKER), "--engine", engine, "--data", str(DATA),
           "--in", str(corpus), "--out", str(out)]
    t0 = time.time()
    proc = subprocess.run(cmd, capture_output=True, text=True)
    dt = time.time() - t0
    if proc.returncode != 0:
        sys.stderr.write(proc.stderr)
        raise SystemExit(f"[parity] engine {engine!r} worker failed (rc={proc.returncode})")
    tail = proc.stderr.strip().splitlines()[-1] if proc.stderr.strip() else ""
    print(f"[parity] {engine:>12}: {dt:6.2f}s  {tail}  -> {out.name}")
    return out


def cmd_run(args) -> int:
    corpus = resolve_corpus(args.corpus, args.seed)
    # Data pin: any engine that actually reads the file must read the pinned bytes.
    if {args.a, args.b} & {"pure", "oracle"}:
        digest = schema.assert_data_pin(str(DATA))
        print(f"[parity] data pin OK: msdrg.mdb sha256={digest[:16]}...")
    print(f"[parity] corpus: {corpus}  ({sum(1 for _ in open(corpus, encoding='utf-8'))} records)")

    a_out = run_engine(args.a, corpus, RUN_DIR)
    b_out = run_engine(args.b, corpus, RUN_DIR)

    corpus_by_id = parity_diff._load_jsonl_by_id(corpus)
    a_by_id = parity_diff._load_jsonl_by_id(a_out)
    b_by_id = parity_diff._load_jsonl_by_id(b_out)
    order = list(corpus_by_id.keys())
    rep = parity_diff.compare(corpus_by_id, a_by_id, b_by_id, order,
                              match_error_message=args.match_error_message,
                              max_examples=args.max_examples)
    parity_diff.print_report(rep, args.a, args.b)
    return 0 if rep.clean else 1


def cmd_gen(args) -> int:
    forwarded = ["--kind", args.kind, "--out", args.out, "--seed", str(args.seed),
                 "--n", str(args.n)]
    return gen_corpus.main(forwarded)


# ---------------------------------------------------------------------------
# selftest — every oracle-independent proof of the rig (Phase-1 evidence)
# ---------------------------------------------------------------------------


def _diff_engine_unit_checks() -> list[tuple[str, bool]]:
    """Craft oracle-like response pairs and assert the diff engine classifies them."""
    from parity_diff import _resp_diff

    def ok(out):
        return {"id": "x", "kind": "group", "ok": True, "output": out}

    def er(t, m="m"):
        return {"id": "x", "kind": "group", "ok": False, "error": {"type": t, "message": m}}

    base = {"final_drg": 5, "sdx_output": [{"code": "A", "severity": "CC"},
                                           {"code": "B", "severity": "MCC"}]}
    checks = []
    # identical -> no diff
    checks.append(("identical->clean", _resp_diff(ok(base), ok(dict(base)), False) is None))
    # scalar field diff -> caught at that path
    b2 = {**base, "final_drg": 6}
    checks.append(("scalar-field->final_drg", _resp_diff(ok(base), ok(b2), False) == "output.final_drg"))
    # list REORDER (same set) -> caught (order-sensitive, Ground Rule 8)
    b3 = {**base, "sdx_output": list(reversed(base["sdx_output"]))}
    checks.append(("list-reorder->caught", _resp_diff(ok(base), ok(b3), False) is not None))
    # list length change -> caught
    b4 = {**base, "sdx_output": base["sdx_output"][:1]}
    checks.append(("list-length->caught", (_resp_diff(ok(base), ok(b4), False) or "").startswith("output.sdx_output")))
    # nested field diff -> deep path
    b5 = {**base, "sdx_output": [{"code": "A", "severity": "MCC"}, base["sdx_output"][1]]}
    checks.append(("nested->deep-path", _resp_diff(ok(base), ok(b5), False) == "output.sdx_output[0].severity"))
    # ok vs error -> 'ok'
    checks.append(("ok-vs-error->ok", _resp_diff(ok(base), er("ValueError"), False) == "ok"))
    # error type differs -> 'error.type'
    checks.append(("errtype->error.type", _resp_diff(er("ValueError"), er("RuntimeError"), False) == "error.type"))
    # same error type, msg differs, type-only mode -> clean
    checks.append(("errmsg-typeonly->clean", _resp_diff(er("ValueError", "a"), er("ValueError", "b"), False) is None))
    # same error type, msg differs, strict -> caught
    checks.append(("errmsg-strict->caught", _resp_diff(er("ValueError", "a"), er("ValueError", "b"), True) == "error.message"))
    # 1 vs 1.0 numeric equality -> clean
    checks.append(("int-vs-float-equal->clean", _resp_diff(ok({"n": 1}), ok({"n": 1.0}), False) is None))
    return checks


def cmd_selftest(args) -> int:
    print("#" * 72)
    print("# PARITY-RIG SELF-TEST (Phase 1, oracle-independent)")
    print("#" * 72)
    failures = 0

    # 1) Corpus determinism: regenerate smoke twice -> identical bytes.
    print("\n[1] corpus determinism")
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    p1 = RUN_DIR / "_det1.jsonl"
    p2 = RUN_DIR / "_det2.jsonl"
    gen_corpus.main(["--kind", "smoke", "--out", str(p1), "--seed", "42"])
    gen_corpus.main(["--kind", "smoke", "--out", str(p2), "--seed", "42"])
    s1 = schema.sha256_file(str(p1))
    s2 = schema.sha256_file(str(p2))
    det_ok = s1 == s2
    print(f"    two seed=42 smoke corpora -> {'IDENTICAL' if det_ok else 'DIFFER'} ({s1[:16]}...)")
    failures += 0 if det_ok else 1

    # 2) Diff-engine unit checks on crafted fixtures.
    print("\n[2] diff-engine unit checks")
    for name, passed in _diff_engine_unit_checks():
        print(f"    {'PASS' if passed else 'FAIL':4}  {name}")
        failures += 0 if passed else 1

    # 3) End-to-end mock-vs-mock over the smoke corpus -> must be CLEAN.
    #    Proves: worker subprocess protocol + streaming + normalization + diff on
    #    real structured outputs, and that a deterministic engine reproduces exactly.
    print("\n[3] end-to-end mock-vs-mock (smoke corpus)")
    corpus = resolve_corpus("smoke", 42)
    a_out = run_engine("mock", corpus, RUN_DIR)
    b_out = run_engine("mock", corpus, RUN_DIR)
    cby = parity_diff._load_jsonl_by_id(corpus)
    rep = parity_diff.compare(cby, parity_diff._load_jsonl_by_id(a_out),
                              parity_diff._load_jsonl_by_id(b_out), list(cby.keys()))
    print(f"    records={rep.total} matched={rep.matched} mismatched={rep.mismatched} "
          f"-> {'CLEAN' if rep.clean else 'DIVERGENT'}")
    failures += 0 if rep.clean else 1

    # 4) End-to-end pure-vs-pure over a small slice -> must be CLEAN (both raise the
    #    same NotImplementedError / ValueError). Proves the pure worker + subprocess
    #    plumbing + validation-error parity are wired correctly.
    print("\n[4] end-to-end pure-vs-pure (200-record slice)")
    slice_path = RUN_DIR / "_slice.jsonl"
    with open(corpus, encoding="utf-8") as fin, open(slice_path, "w", encoding="utf-8", newline="\n") as fout:
        for i, line in enumerate(fin):
            if i >= 200:
                break
            fout.write(line)
    pa = run_engine("pure", slice_path, RUN_DIR)
    pb = run_engine("pure", slice_path, RUN_DIR)
    scby = parity_diff._load_jsonl_by_id(slice_path)
    prep = parity_diff.compare(scby, parity_diff._load_jsonl_by_id(pa),
                               parity_diff._load_jsonl_by_id(pb), list(scby.keys()))
    # Also confirm the pure engine really is exercising both code paths.
    pa_recs = parity_diff._load_jsonl_by_id(pa)
    n_notimpl = sum(1 for r in pa_recs.values()
                    if not r["ok"] and r["error"]["type"] == "NotImplementedError")
    n_valerr = sum(1 for r in pa_recs.values()
                   if not r["ok"] and r["error"]["type"] == "ValueError")
    print(f"    records={prep.total} matched={prep.matched} mismatched={prep.mismatched} "
          f"-> {'CLEAN' if prep.clean else 'DIVERGENT'}")
    print(f"    pure engine exercised: NotImplementedError={n_notimpl}  ValueError={n_valerr}")
    failures += 0 if prep.clean else 1

    # 5) Schema coverage sanity: the smoke corpus reaches all three kinds.
    print("\n[5] schema/kind coverage in smoke corpus")
    kinds = {}
    for rec in cby.values():
        kinds[rec["kind"]] = kinds.get(rec["kind"], 0) + 1
    covered = all(k in kinds for k in schema.KINDS)
    print(f"    kinds present: {kinds}  -> {'ALL' if covered else 'MISSING'} of {schema.KINDS}")
    failures += 0 if covered else 1

    print("\n" + "#" * 72)
    print(f"# SELF-TEST {'PASS' if failures == 0 else f'FAIL ({failures} failure(s))'}")
    print("#" * 72)
    return 0 if failures == 0 else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="parity harness orchestrator")
    sub = ap.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="run engine A vs engine B over a corpus")
    r.add_argument("--a", default="pure")
    r.add_argument("--b", default="oracle")
    r.add_argument("--corpus", default="smoke")
    r.add_argument("--seed", type=int, default=42)
    r.add_argument("--match-error-message", action="store_true")
    r.add_argument("--max-examples", type=int, default=20)
    r.set_defaults(func=cmd_run)

    g = sub.add_parser("gen", help="generate a corpus")
    g.add_argument("--kind", required=True,
                   choices=["random", "sweeps", "malformed", "smoke", "full"])
    g.add_argument("--out", required=True)
    g.add_argument("--n", type=int, default=50000)
    g.add_argument("--seed", type=int, default=42)
    g.set_defaults(func=cmd_gen)

    s = sub.add_parser("selftest", help="oracle-independent proofs of the rig")
    s.set_defaults(func=cmd_selftest)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
