# Parity harness (`tools/`) — Phase 1

The rig that will prove the pure-Python port is bit-identical to the Zig oracle.
Built oracle-agnostic: engines plug in behind one worker, and the diff is
order-sensitive across the entire output surface.

## Components

| File | Role |
|---|---|
| `schema.py` | Wire protocol (corpus record / worker response), full output field surface, `normalize_output`, the pinned `msdrg.mdb` sha256. |
| `worker.py` | Backend-agnostic JSONL worker. `--engine {oracle,pure,mock}`; per-record dispatch by `kind` (group/mce/convert). stdin/`--in` → stdout/`--out`. |
| `gen_corpus.py` | Seeded corpus generator. Universes bootstrapped from the SQLite export `../msdrg.db` (no oracle needed). `--kind {random,sweeps,malformed,smoke,full}`. |
| `parity_diff.py` | Order-sensitive structural diff; first-diff field path; per-field tally; nonzero exit on any divergence. |
| `parity.py` | Orchestrator: `run` / `gen` / `selftest`. Resolves engines, runs two workers, diffs. |
| `_verify_oracle.py` | Throwaway oracle sanity check (import resolves to wheel, data pin, one group/mce/convert). |

## Wire protocol

Corpus record (one JSON per line):
```json
{"id": "rand-g-0", "kind": "group", "input": { ...claim... }}
{"id": "c-1", "kind": "convert", "input": {"code": "A000", "code_type": "dx", "source_year": 2025, "target_year": 2026}}
```
Worker response:
```json
{"id": "rand-g-0", "kind": "group", "ok": true,  "output": { ...normalized... }}
{"id": "bad-g-11", "kind": "group", "ok": false, "error": {"type": "ValueError", "message": "..."}}
```
Exceptions are part of parity: a claim that raises in one engine must raise the
same **type** in the other (message compared only with `--match-error-message`).

## Usage

```bash
# oracle-independent proof of the whole rig (works today)
python tools/parity.py selftest

# generate corpora (deterministic; sidecar <out>.meta.json records seed+shas)
python tools/parity.py gen --kind smoke --out tests/fixtures/corpus/smoke.jsonl
python tools/parity.py gen --kind full  --n 50000 --out tests/fixtures/corpus/full.jsonl

# run engine A vs engine B over a corpus
python tools/parity.py run --a mock --b mock --corpus smoke      # rig demo
python tools/parity.py run --a pure --b oracle --corpus full     # the real thing (needs an oracle)

# pytest subset (new code only; NOT the repo suite)
pytest tests/unit -q
```

The `Makefile` mirrors these (`make selftest`, `make parity CORPUS=…`, `make gen-corpus`, `make test`).

## The oracle seam (why this is decoupled)

The Zig oracle's native `msdrg.dll` is **blocked by enterprise EDR on this host**
(read + load denied machine-wide; see `../PORTING_NOTES.md` §9.1). Until a
runnable oracle exists, `--a/--b oracle` won't run here. Three drop-in routes,
none requiring rework:

1. **`frozen:<path>`** — run the oracle once on a VM / unmanaged machine
   (manylinux wheel), pipe the same corpus through `worker.py --engine oracle`,
   copy the responses JSONL here, and diff with `--b frozen:path/to/oracle.jsonl`.
2. **`$DRGPY_ORACLE_PYTHON`** — point at a venv whose `msdrg` DLL is loadable
   (WSL/allow-listed); `--a/--b oracle` then runs live.
3. IT allow-lists the DLL → same as (2) with the local oracle venv.

Both engines always read the **pinned** `data/msdrg.mdb`
(`schema.PINNED_MDB_SHA256`); the orchestrator asserts it before any run that
touches real data (Ground Rule 3).
