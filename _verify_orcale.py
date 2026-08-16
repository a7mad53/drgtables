"""One-shot Phase-1 oracle sanity check (throwaway, safe to delete).

Run with the ORACLE venv interpreter. Confirms:
  1. `import msdrg` resolves to the INSTALLED wheel, not the repo source tree.
  2. sha256 of the repo data/msdrg.mdb matches the Phase-0 pinned value.
  3. The oracle can group a claim, run MCE, and convert a code against that file.
"""

import hashlib
import sys
from pathlib import Path

PINNED_MDB_SHA256 = "534c166cb2c0f78420a7691c51890432139862f1667e2edfc6374852612b2744"

REPO = Path(__file__).resolve().parents[1]
DATA_FILE = REPO / "data" / "msdrg.mdb"


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> int:
    import msdrg

    print("msdrg module file:", msdrg.__file__)
    print("msdrg version    :", msdrg.__version__)
    assert "site-packages" in msdrg.__file__, (
        "FAIL: import resolved to the repo source tree, not the installed wheel"
    )

    digest = sha256(DATA_FILE)
    print("data file        :", DATA_FILE)
    print("data sha256       :", digest)
    assert digest == PINNED_MDB_SHA256, f"FAIL: data sha256 drift ({digest})"

    data_dir = str(DATA_FILE)

    with msdrg.MsdrgGrouper(data_dir=data_dir) as g:
        r = g.group(
            msdrg.create_claim(
                version=431, age=65, sex=0, discharge_status=1,
                pdx="I5033", sdx=["E1165", "N179"],
            )
        )
        print("group final_drg  :", r["final_drg"], "mdc:", r["final_mdc"],
              "rc:", r["return_code"])
        assert r["final_drg"] is not None

    with msdrg.MceEditor(data_dir=data_dir) as m:
        mr = m.edit(
            msdrg.create_mce_input(
                discharge_date=20250101, age=65, sex=0, discharge_status=1,
                pdx="I5033",
            )
        )
        print("mce edit_type    :", mr["edit_type"], "edits:", len(mr["edits"]))

    with msdrg.IcdConverter(data_dir=data_dir) as c:
        conv = c.convert_dx("A000", source_year=2025, target_year=2026)
        print("convert A000     :", conv)

    print("ORACLE OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
