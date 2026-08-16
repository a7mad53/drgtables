# Parity Harness — MS-DRG Table Verification

The rig that proves the pure-Python port is bit-identical to the Zig oracle reference.
Built oracle-agnostic: engines plug in behind one worker, and the diff is
order-sensitive across the entire output surface.

## Components

| File | Role |
|---|---|
| `schema.py` | Wire protocol (corpus record / worker response), full output field surface, `normalize_output`, the pinned `msdrg.mdb` sha256. |
| `worker.py` | Backend-agnostic JSONL worker. `--engine {oracle,pure,mock}`; per-record dispatch by `kind` (group/mce/convert). stdin/`--in` → stdout/`--out`. |
| `gen_corpus.py` | Seeded corpus generator. Universes bootstrapped from SQLite `msdrg.db`. `--kind {random,sweeps,malformed,smoke,full}`. |
| `parity_diff.py` | Order-sensitive structural diff; first-diff field path; per-field tally; nonzero exit on any divergence. |
| `parity.py` | Orchestrator: `run` / `gen` / `selftest`. Resolves engines, runs two workers, diffs. |
| `orcale_runner.py` | Standalone oracle runner for generating frozen response streams. |
| `_verify_orcale.py` | Throwaway oracle sanity check. |

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

## Usage

```bash
# Oracle-independent proof of the whole rig
python parity.py selftest

# Generate corpora (deterministic; sidecar <out>.meta.json records seed+shas)
python parity.py gen --kind smoke --out tests/fixtures/corpus/smoke.jsonl
python parity.py gen --kind full  --n 50000 --out tests/fixtures/corpus/full.jsonl

# Run engine A vs engine B over a corpus
python parity.py run --a mock --b mock --corpus smoke                  # Rig demo (smoke)
python parity.py run --a mock --b mock --corpus full                   # Rig demo (full 50,790 recs)

# Oracle determinism verification (smoke & full)
python parity.py run --a frozen:oracle_run1.jsonl --b frozen:oracle_run2.jsonl --corpus smoke
python parity.py run --a frozen:full_oracle_run1.jsonl --b frozen:full_oracle_run2.jsonl --corpus full

# Validate pure Python port against frozen oracle reference once ported
python parity.py run --a pure --b frozen:full_oracle_run1.jsonl --corpus full
```

## Parity Verification Results (Option A Completed)

- **Corpus Generation**: Generated `smoke.jsonl` (1,790 records) and `full.jsonl` (50,790 records).
- **Oracle Determinism**: Verified that independent runs of the `msdrg` v1.2.0 oracle produce **0 divergences** (100% bit-identical matches across 50,790 records).
- **Mock Rig Plumbing**: Confirmed end-to-end streaming worker subprocesses, JSON serialization, and deep field-by-field diff reporting.
