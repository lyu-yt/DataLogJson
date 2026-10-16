"""Create clearly synthetic files and a finalized example in a NEW directory."""
import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from experiment_logger.store import Store


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--destination", required=True)
    args = parser.parse_args()
    dest = Path(args.destination).resolve()
    if dest.exists():
        parser.error("Destination must not exist; demo never overwrites existing files")
    source = dest / "synthetic-source" / "261015" / "007" / "001.csv"
    source.parent.mkdir(parents=True)
    source.write_text("\n".join("\t".join(map(str, [i / 10, i / 20, 0, 0, 0, 0, 0, 0, 0, i % 2, 3, 0, i])) for i in range(100)) + "\n")
    root = dest / "archive"
    store = Store(root / "metadata-json", {"archive": str(root)}, dest / "local-state")
    store.initialize()
    d = store.new(source)
    d["record"].update(operator="SYNTHETIC EXAMPLE", experiment_type="water linear push", notes="SYNTHETIC: not an animal experiment")
    d["record"]["metadata"].update(mouse_weight_g=25.0, training_day=12, is_imaging_session=False)
    d = store.save_draft(d)
    print(store.finalize(d, source_complete=True))


if __name__ == "__main__":
    main()
