"""
Parity diff engine (Phase 1).

Compares two response streams (engine A vs engine B) aligned by record ``id`` and
reports every divergence field-by-field. Ordering is significant everywhere
(Ground Rule 8): lists compare element-by-element; a length change is a diff.

A record diverges when:
  * one side produced a result and the other raised            (``ok`` mismatch), or
  * both produced results whose structures differ, or
  * both raised but the exception TYPE differs (message compared only with
    ``--match-error-message``; validation/runtime messages are Python-side and
    identical between engines, but keeping type-only by default avoids coupling
    the suite to message wording).

On mismatch the report prints the originating claim, both full outputs, and the
first differing field path. Exit code is nonzero if any divergence is found.

Usable as a library (``compare``) or CLI.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO))
import schema  # noqa: E402

_MISSING = object()


def _first_diff(a: Any, b: Any, path: str = "") -> str | None:
    """Return the first differing field path between *a* and *b*, or None if equal.

    Order-sensitive for lists. Type differences and missing keys/elements are diffs.
    """
    if type(a) is not type(b):
        # int/float can be JSON-equal (e.g. 1 vs 1.0); treat numerically only if equal value
        if isinstance(a, (int, float)) and isinstance(b, (int, float)) and not isinstance(a, bool) and not isinstance(b, bool):
            return None if a == b else (path or "<root>")
        return path or "<root>"
    if isinstance(a, dict):
        keys = list(dict.fromkeys(list(a.keys()) + list(b.keys())))
        for k in keys:
            av = a.get(k, _MISSING)
            bv = b.get(k, _MISSING)
            sub = f"{path}.{k}" if path else k
            if av is _MISSING or bv is _MISSING:
                return sub
            d = _first_diff(av, bv, sub)
            if d is not None:
                return d
        return None
    if isinstance(a, list):
        if len(a) != len(b):
            return f"{path}[len {len(a)}!={len(b)}]"
        for i, (av, bv) in enumerate(zip(a, b)):
            d = _first_diff(av, bv, f"{path}[{i}]")
            if d is not None:
                return d
        return None
    return None if a == b else (path or "<root>")


def _resp_diff(ra: dict, rb: dict, match_error_message: bool) -> str | None:
    """Compare two response records for the same id; return diff path or None."""
    if ra.get("ok") != rb.get("ok"):
        return "ok"
    if ra.get("ok"):
        return _first_diff(ra.get("output"), rb.get("output"), "output")
    # both errored
    ea, eb = ra.get("error", {}), rb.get("error", {})
    if ea.get("type") != eb.get("type"):
        return "error.type"
    if match_error_message and ea.get("message") != eb.get("message"):
        return "error.message"
    return None


@dataclass
class DiffReport:
    total: int = 0
    matched: int = 0
    mismatched: int = 0
    only_a: int = 0  # ids present in A but not B
    only_b: int = 0
    by_field: dict[str, int] = field(default_factory=dict)
    examples: list[dict] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        return self.mismatched == 0 and self.only_a == 0 and self.only_b == 0

    @property
    def mismatch_rate(self) -> float:
        return (self.mismatched / self.total) if self.total else 0.0


def _load_jsonl_by_id(path: Path) -> dict[str, dict]:
    out: dict[str, dict] = {}
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            out[rec["id"]] = rec
    return out


def compare(
    corpus_by_id: dict[str, dict],
    a_by_id: dict[str, dict],
    b_by_id: dict[str, dict],
    order: list[str],
    *,
    match_error_message: bool = False,
    max_examples: int = 20,
) -> DiffReport:
    rep = DiffReport()
    for rid in order:
        rep.total += 1
        ra = a_by_id.get(rid)
        rb = b_by_id.get(rid)
        if ra is None or rb is None:
            if ra is not None:
                rep.only_a += 1
            if rb is not None:
                rep.only_b += 1
            rep.mismatched += 1
            rep.by_field["<missing response>"] = rep.by_field.get("<missing response>", 0) + 1
            if len(rep.examples) < max_examples:
                rep.examples.append({
                    "id": rid, "field": "<missing response>",
                    "claim": corpus_by_id.get(rid),
                    "a": ra, "b": rb,
                })
            continue
        d = _resp_diff(ra, rb, match_error_message)
        if d is None:
            rep.matched += 1
        else:
            rep.mismatched += 1
            rep.by_field[d] = rep.by_field.get(d, 0) + 1
            if len(rep.examples) < max_examples:
                rep.examples.append({
                    "id": rid, "field": d,
                    "claim": corpus_by_id.get(rid),
                    "a": ra, "b": rb,
                })
    return rep


def print_report(rep: DiffReport, a_name: str, b_name: str, show_examples: int = 10) -> None:
    print("=" * 72)
    print(f"PARITY DIFF  {a_name}  vs  {b_name}")
    print("=" * 72)
    print(f"  records compared : {rep.total}")
    print(f"  matched          : {rep.matched}")
    print(f"  mismatched       : {rep.mismatched}  ({rep.mismatch_rate * 100:.4f}%)")
    if rep.only_a or rep.only_b:
        print(f"  only in {a_name:>6} : {rep.only_a}")
        print(f"  only in {b_name:>6} : {rep.only_b}")
    if rep.by_field:
        print("  mismatches by first-diff field:")
        for fld, n in sorted(rep.by_field.items(), key=lambda kv: (-kv[1], kv[0])):
            print(f"      {n:8d}  {fld}")
    if rep.examples:
        print("-" * 72)
        print(f"  first {min(show_examples, len(rep.examples))} example(s):")
        for ex in rep.examples[:show_examples]:
            print(f"  ---- id={ex['id']}  first-diff={ex['field']} ----")
            print(f"    claim : {json.dumps(ex['claim'], ensure_ascii=False, sort_keys=True)}")
            print(f"    {a_name:>6}: {json.dumps(ex['a'], ensure_ascii=False, sort_keys=True)}")
            print(f"    {b_name:>6}: {json.dumps(ex['b'], ensure_ascii=False, sort_keys=True)}")
    print("=" * 72)
    print("RESULT:", "CLEAN - 0 divergences" if rep.clean else f"DIVERGENT - {rep.mismatched} mismatch(es)")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="parity diff engine")
    ap.add_argument("--corpus", required=True, help="corpus JSONL (for claim context on mismatch)")
    ap.add_argument("--a", required=True, help="engine A responses JSONL")
    ap.add_argument("--b", required=True, help="engine B responses JSONL")
    ap.add_argument("--a-name", default="A")
    ap.add_argument("--b-name", default="B")
    ap.add_argument("--match-error-message", action="store_true")
    ap.add_argument("--max-examples", type=int, default=20)
    args = ap.parse_args(argv)

    corpus = _load_jsonl_by_id(Path(args.corpus))
    a = _load_jsonl_by_id(Path(args.a))
    b = _load_jsonl_by_id(Path(args.b))
    order = list(corpus.keys())
    rep = compare(corpus, a, b, order,
                  match_error_message=args.match_error_message,
                  max_examples=args.max_examples)
    print_report(rep, args.a_name, args.b_name)
    return 0 if rep.clean else 1


if __name__ == "__main__":
    raise SystemExit(main())
