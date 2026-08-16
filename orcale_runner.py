#!/usr/bin/env python3
"""
Standalone ORACLE runner — run this on a machine where the ``msdrg`` wheel LOADS
(i.e. NOT the EDR-locked work host; a personal/unmanaged computer, any OS with a
PyPI wheel: Windows, macOS, or Linux).

It is fully self-contained: it has NO dependency on the rest of the repo, so you
only copy THIS file plus a corpus ``.jsonl`` over. It reads corpus records and
writes response records in the exact envelope the parity harness expects, so the
output drops straight into ``parity.py`` as a ``frozen:<path>`` engine.

────────────────────────────────────────────────────────────────────────────────
QUICK START (on your personal computer)
────────────────────────────────────────────────────────────────────────────────
  1. Install Python 3.11+ (any), then:
         pip install msdrg==1.2.0
  2. Copy this file and the corpus (e.g. smoke.jsonl) into an empty folder.
  3. Run it TWICE (two independent runs → proves oracle determinism):
         python oracle_runner.py --in smoke.jsonl --out oracle_run1.jsonl
         python oracle_runner.py --in smoke.jsonl --out oracle_run2.jsonl
  4. Copy oracle_run1.jsonl and oracle_run2.jsonl back to the work machine.

Then on the work machine (closes the Phase-1 "oracle-vs-oracle clean" DoD):
     python tools/parity.py run --a frozen:PATH/oracle_run1.jsonl \\
                                --b frozen:PATH/oracle_run2.jsonl --corpus smoke
And, once the pure engine is ported, diff pure vs the frozen oracle reference:
     python tools/parity.py run --a pure --b frozen:PATH/oracle_run1.jsonl --corpus smoke
────────────────────────────────────────────────────────────────────────────────

IMPORTANT — same data bytes (Ground Rule 3): the v1.2.0 wheel BUNDLES its own
``msdrg.mdb``. This script verifies that bundled file's sha256 == the pinned value
below and refuses to run on a mismatch. If it ever mismatches, copy the work
machine's ``data/msdrg.mdb`` over and pass ``--data path/to/msdrg.mdb``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys

# Phase-0 pinned reference-data hash (PORTING_NOTES §4 / ARCHITECTURE_INVENTORY §8.1).
PINNED_MDB_SHA256 = "534c166cb2c0f78420a7691c51890432139862f1667e2edfc6374852612b2744"


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _normalize_convert(out) -> dict:
    return out if isinstance(out, dict) else {"converted": out}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="standalone msdrg oracle runner")
    ap.add_argument("--in", dest="infile", required=True, help="corpus JSONL")
    ap.add_argument("--out", dest="outfile", required=True, help="responses JSONL")
    ap.add_argument("--data", default=None,
                    help="path to msdrg.mdb (default: the wheel's bundled data)")
    ap.add_argument("--skip-pin-check", action="store_true",
                    help="do not verify the bundled data sha256 (not recommended)")
    args = ap.parse_args(argv)

    import msdrg

    mfile = msdrg.__file__ or ""
    if "site-packages" not in mfile:
        print(f"WARNING: 'import msdrg' resolved to {mfile!r} — this looks like a source "
              "tree, not the installed wheel. Run from an EMPTY folder so the wheel wins.",
              file=sys.stderr)
    print(f"[oracle_runner] msdrg {getattr(msdrg, '__version__', '?')}  ({mfile})",
          file=sys.stderr)

    # Resolve + verify the data file the oracle will actually read.
    data_path = args.data
    if data_path is None:
        try:
            from msdrg._native import find_data_path
            data_path = find_data_path()
        except Exception as e:  # pragma: no cover
            print(f"[oracle_runner] could not locate bundled data ({e}); "
                  "pass --data explicitly.", file=sys.stderr)
    if data_path and not args.skip_pin_check:
        digest = sha256_file(data_path)
        status = "OK" if digest == PINNED_MDB_SHA256 else "MISMATCH"
        print(f"[oracle_runner] data {data_path}\n[oracle_runner] data sha256 {digest} [{status}]",
              file=sys.stderr)
        if digest != PINNED_MDB_SHA256:
            print("ERROR: data sha256 does not match the pinned msdrg.mdb. Copy the work "
                  "machine's data/msdrg.mdb over and pass --data.", file=sys.stderr)
            return 2

    corpus_sha = sha256_file(args.infile)
    print(f"[oracle_runner] corpus {args.infile}\n[oracle_runner] corpus sha256 {corpus_sha}",
          file=sys.stderr)

    kw = {"data_dir": data_path} if data_path else {}
    g = msdrg.MsdrgGrouper(**kw)
    m = msdrg.MceEditor(**kw)
    c = msdrg.IcdConverter(**kw)

    ok = err = 0
    try:
        with open(args.infile, "r", encoding="utf-8") as fin, \
             open(args.outfile, "w", encoding="utf-8", newline="\n") as fout:
            for i, line in enumerate(fin, 1):
                line = line.strip()
                if not line:
                    continue
                rec = json.loads(line)
                rid, kind, inp = rec["id"], rec["kind"], rec["input"]
                try:
                    if kind == "group":
                        out = g.group(inp)
                    elif kind == "mce":
                        out = m.edit(inp)
                    elif kind == "convert":
                        fn = c.convert_dx if inp["code_type"] == "dx" else c.convert_pr
                        out = {"converted": fn(inp["code"], inp["source_year"], inp["target_year"])}
                    else:
                        raise ValueError(f"unknown kind {kind!r}")
                    resp = {"id": rid, "kind": kind, "ok": True,
                            "output": _normalize_convert(out) if kind == "convert" else out}
                    ok += 1
                except Exception as exc:  # exceptions are part of parity
                    resp = {"id": rid, "kind": kind, "ok": False,
                            "error": {"type": type(exc).__name__, "message": str(exc)}}
                    err += 1
                fout.write(json.dumps(resp, ensure_ascii=False, sort_keys=True) + "\n")
                if i % 5000 == 0:
                    print(f"[oracle_runner] {i} records...", file=sys.stderr)
    finally:
        for obj in (g, m, c):
            try:
                obj.close()
            except Exception:
                pass

    print(f"[oracle_runner] DONE ok={ok} err={err} -> {args.outfile}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
