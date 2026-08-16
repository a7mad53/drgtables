# Phase 6 — offline-oracle handoff

Phase 6's core DoD (**0 diffs at scale** + **3 fresh-seed acceptance runs** +
coverage) needs the Zig oracle, which is **EDR-blocked on this dev host**
(PORTING_NOTES §9.1). Everything below is generated + frozen + **pure-validated
on-host**; this doc is the recipe to run the oracle on the personal PC (via the
`github.com/a7mad53/drgtables` repo, same route as Phase 1) and close the gate.

## What each corpus is (deterministic; sha in `<name>.meta.json`)

| Corpus | Purpose | records | corpus_sha256 (first 16) |
|---|---|---|---|
| `full.jsonl` | standing Phase-1..5 corpus (already 0-diff vs `full_oracle_run1.jsonl`) | 50,790 | `f06ada3bac7bb9ba` |
| `scale.jsonl` | scale-up (150k random) for at-scale parity + DRG coverage | 150,000 | `9521dd3631b1c368` |
| `accept_43.jsonl` | acceptance #1 (seed 43, full = 50k random + sweeps + malformed) | ~50,790 | `8b6bf7f0d4228ab7` |
| `accept_44.jsonl` | acceptance #2 (seed 44) | ~50,790 | `664f6fcb683a2a7e` |
| `accept_45.jsonl` | acceptance #3 (seed 45) | ~50,790 | `a6aa434404070f75` |
| `targeted.jsonl` | targeted coverage (MDCs/return-codes/MCE edits/edit_types/marking-order) | 46 | `8ae6747a35d6fd4f` |

All under `tests/fixtures/corpus/`. Two data files, both sha-pinned:
- SQLite export `../msdrg.db` — `11fa4735b6e39738c8a71d50b19700509b77442fe4a89015ea65ecd9ed359527` (used to build corpora)
- LMDB `data/msdrg.mdb` — `534c166cb2c0f78420a7691c51890432139862f1667e2edfc6374852612b2744` (the engine data both the oracle and the pure port MUST read)

## You do NOT have to copy the big corpora — regenerate them on your PC

Every corpus is a pure function of (generator script, seed, `msdrg.db`), so it
rebuilds byte-identically anywhere. Phase 1 already proved this (PC-regenerated
`full.jsonl` matched, `f06ada3b…`).

- **`scale.jsonl` + `accept_43/44/45.jsonl`** — need only `tools/gen_corpus.py` +
  `tools/schema.py` + `msdrg.db` (no `.mdb`, no pure port):
  ```
  python tools/gen_corpus.py --kind random --n 150000 --seed 42 --out scale.jsonl
  python tools/gen_corpus.py --kind full   --n 50000  --seed 43 --out accept_43.jsonl   # then 44, 45
  ```
  Each run prints its `corpus sha256` — **check it against the table above.** A match
  ⇒ byte-identical to mine. (The `drgtables` repo already ships `gen_corpus.py` +
  `msdrg.db`; if a sha differs, that copy of `gen_corpus.py` has drifted — use THIS
  repo's `tools/gen_corpus.py`.)
- **`targeted.jsonl`** is the exception: its generator (`tools/gen_targeted.py`) calls
  the pure engine to confirm each claim, so regenerating it needs the `msdrg_pure`
  source + both data files. It is only 46 records (~11 KB) and **committed to the
  repo** — just copy that one small file instead of regenerating it.

## Two ways to get the parity result

### Option A (simplest) — run BOTH engines on the PC, diff there
If you put the pure port (`src/msdrg_pure/`, stdlib-only) on the same PC, run the
harness directly with `--a pure --b oracle` and nothing needs copying back:
```
python tools/parity.py run --corpus scale.jsonl      --a pure --b oracle   # expect 0 divergences
python tools/parity.py run --corpus targeted.jsonl   --a pure --b oracle
python tools/parity.py run --corpus accept_43.jsonl  --a pure --b oracle   # then 44, 45
python tools/coverage_audit.py --out docs/COVERAGE.md \
    <oracle-or-pure response jsonls...>     # union coverage
```
Point the harness at the oracle interpreter via `$DRGPY_ORACLE_PYTHON` (see
`tools/parity.py`). Report back: the divergence count per run (want 0) + the
refreshed `docs/COVERAGE.md`.

### Option B — run only the oracle on the PC, diff back on this host
1. Oracle over each corpus (reading the pinned `msdrg.mdb`):
   ```
   python oracle_runner.py --in scale.jsonl      --out scale_oracle.jsonl
   python oracle_runner.py --in accept_43.jsonl  --out accept_43_oracle.jsonl   # 44, 45
   python oracle_runner.py --in targeted.jsonl   --out targeted_oracle.jsonl
   ```
2. Copy the `*_oracle.jsonl` back to `mz-drg/tests/fixtures/oracle/` (gitignored).
3. Diff + coverage here:
   ```
   python tools/parity.py run --corpus tests/fixtures/corpus/scale.jsonl \
       --a pure --b frozen:tests/fixtures/oracle/scale_oracle.jsonl
   python tools/parity.py run --corpus tests/fixtures/corpus/accept_43.jsonl \
       --a pure --b frozen:tests/fixtures/oracle/accept_43_oracle.jsonl   # 44, 45
   python tools/parity.py run --corpus tests/fixtures/corpus/targeted.jsonl \
       --a pure --b frozen:tests/fixtures/oracle/targeted_oracle.jsonl
   python tools/coverage_audit.py --out docs/COVERAGE.md \
       tests/fixtures/oracle/full_oracle_run1.jsonl \
       tests/fixtures/oracle/scale_oracle.jsonl \
       tests/fixtures/oracle/targeted_oracle.jsonl \
       tests/fixtures/oracle/accept_4{3,4,5}_oracle.jsonl
   ```

Any divergence → capture the failing record as a regression fixture FIRST
(Ground Rule 4), then fix. The one pre-identified risk is the dx-marking
`remaining_attributes` hashmap-order item (PORTING_NOTES §13.3) — the
`targeted.jsonl` `tgt-g-markorder-*` records are built to stress it; if one
diverges, replicate the Zig `StringHashMap` order for that dict.

## Projected coverage (pure-confirmed, pending oracle) — `docs/COVERAGE_PROJECTED.md`

Baseline (oracle) → projected (oracle ∪ scale.pure ∪ targeted.pure):
DRGs 547→**636/813**, output MDCs 0–24→**0–25** (all reachable), return codes 9/10,
**MCE edits 13→32/33 reachable + all 5 edit_types**. The offline oracle run over
`scale.jsonl` + `targeted.jsonl` should reproduce these bit-for-bit.

## Unreachable-by-construction / by-data (documented, NOT gaps — see docs/COVERAGE.md)

- Grouper return codes: `INVALID_PRINCIPAL_DIAGNOSIS` (never set in Zig),
  `INVALID_AGE` (no formula text `"INVALID_AGE"` in the data). Both 0 occurrences in
  ~141k claims.
- `UNGROUPABLE`: 0 occurrences in ~141k valid claims — the code path exists but every
  valid PDX's MDC has a matching medical fallback → **empirically unreachable**
  (observe at scale against the oracle; not a fixable gap).
- **MDC 29** is unreachable as an *output* MDC (marking-internal only; the unrelated-OR
  DRGs 981–989 keep the PDX's MDC as `final_mdc`). Output-MDC universe is 0–25.
- MCE edits `TYPE_OF_AGE_CONFLICT`, `INVALID_POA`, `LIMITED_COVERAGE_ARTIFICIAL_HEART`
  (by-design); `LIMITED_COVERAGE_PANCREAS` (by-data: both `lcov_pancreasxp` codes also
  carry `ncov45`→NON_COVERED, which preempts LCOV).
- MCE edits reachable **only via ICD-9** data (flags absent from the ICD-10 masters):
  the transplant `LIMITED_COVERAGE_*`, `NONSPECIFIC_PDX`, `NONSPECIFIC_OR`,
  `MEDICARE_IS_SECONDARY_PAYER`, `BILATERAL`, `OPEN_BIOPSY` — `targeted.jsonl` covers
  these with `icd_version=9` claims + legacy discharge dates.

## Acceptance / gate

DoD: scale + 3 acceptance runs all 0 divergences; `docs/COVERAGE.md` committed;
corpora frozen (shas in the `.meta.json` sidecars). **Then: human review of the
parity + coverage report** (first gate since Phase 0).
