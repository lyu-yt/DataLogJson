"""Command line access to the same service used by the GUI."""
import argparse
import json
from pathlib import Path

from .io import LoggerError, atomic_json, read_json
from .store import Store
from .layout import metadata_directory


def main(argv=None):
    parser = argparse.ArgumentParser(description="Local mouse experiment logger")
    parser.add_argument("--config", default=".logger-local/config.json")
    sub = parser.add_subparsers(dest="action", required=True)
    init = sub.add_parser("init")
    init.add_argument("archive")
    init.add_argument("--operator", required=True)
    new = sub.add_parser("draft")
    new.add_argument("source")
    expr = sub.add_parser("expression-draft", help="Create an expression draft from one mouse folder")
    expr.add_argument("folder")
    imaging = sub.add_parser("imaging-draft", help="Create an imaging draft; review side/channel/behavior fields before finalize --move")
    imaging.add_argument("folder")
    final = sub.add_parser("finalize")
    final.add_argument("draft_id")
    final.add_argument("--source-complete", action="store_true")
    final.add_argument("--move", action="store_true", help="Authorize direct moves; otherwise verified copies and source removal after the metadata Git commit")
    for name in ("browse", "recover", "rebuild-index", "rebuild-catalog", "commit-catalog"):
        sub.add_parser(name)
    preview = sub.add_parser("plan-paths", help="Preview readable path migration without changing archive files")
    preview.add_argument("--output", help="Save the preview JSON locally for review")
    migrate = sub.add_parser("migrate-paths", help="Explicitly apply a previously reviewed migration preview")
    migrate.add_argument("plan_json")
    for name in ("show", "verify"):
        p = sub.add_parser(name)
        p.add_argument("id")
    profile = sub.add_parser("mouse-profile", help="Read a mouse profile, or return an unsaved starter")
    profile.add_argument("mouse_id")
    save_profile = sub.add_parser("save-mouse-profile", help="Save an edited profile JSON with revision checking")
    save_profile.add_argument("profile_json")
    colony = sub.add_parser("colony-search", help="List workbook candidates; never automatically select an identity")
    colony.add_argument("workbook")
    colony.add_argument("mouse_id")
    relink = sub.add_parser("relink")
    relink.add_argument("id")
    relink.add_argument("root_id")
    relink.add_argument("relative_path")
    locate = sub.add_parser("locate")
    locate.add_argument("id")
    locate.add_argument("root_ids", nargs="+")
    amend = sub.add_parser("amend")
    amend.add_argument("id")
    amend.add_argument("reason")
    amend.add_argument("corrections_json", help="Path to strict JSON object of corrected values")
    run = sub.add_parser("run-python")
    run.add_argument("code_repo")
    run.add_argument("entry_point")
    run.add_argument("parameters_json")
    run.add_argument("input_ids", nargs="+")
    run.add_argument("--trusted", action="store_true")
    args = parser.parse_args(argv)
    config_path = Path(args.config).resolve()
    try:
        if args.action == "init":
            root = str(Path(args.archive).resolve())
            config = {"roots": {"archive": root}, "operator": args.operator, "timezone": "America/New_York"}
            store = Store(metadata_directory(root), config["roots"], config_path.parent)
            store.initialize()
            atomic_json(config_path, config)
            result = {"config": str(config_path), "archive": root}
        else:
            config = read_json(config_path)
            store = Store(metadata_directory(config["roots"]["archive"]), config["roots"], config_path.parent)
            match args.action:
                case "draft":
                    d = store.new(args.source)
                    d["record"]["operator"] = config["operator"]
                    result = store.save_draft(d)
                case "expression-draft":
                    d = store.new_expression(args.folder)
                    d["record"]["operator"] = config["operator"]
                    result = store.save_draft(d)
                case "imaging-draft":
                    d = store.new_imaging(args.folder)
                    d["record"]["operator"] = config["operator"]
                    result = store.save_draft(d)
                case "finalize":
                    result = store.finalize(store.load_draft(args.draft_id), move=args.move, source_complete=args.source_complete)
                case "browse": result = store.index()
                case "show": result = store.read_record(args.id)
                case "verify": result = store.verify_file(args.id)
                case "mouse-profile":
                    from .colony import new_profile
                    result = store.load_mouse_profile(args.mouse_id) or new_profile(args.mouse_id, config["operator"])
                case "save-mouse-profile": result = store.save_mouse_profile(read_json(args.profile_json), config["operator"])
                case "colony-search":
                    from .colony import search_workbook
                    result = search_workbook(args.workbook, args.mouse_id)
                case "recover": result = store.recover()
                case "rebuild-index": result = store.rebuild_index()
                case "rebuild-catalog": result = store.rebuild_catalog()
                case "commit-catalog": result = store.commit_catalog(config["operator"])
                case "plan-paths":
                    result = store.plan_readable_migration()
                    if args.output:
                        atomic_json(Path(args.output).resolve(), result)
                case "migrate-paths": result = store.migrate_paths(read_json(args.plan_json), config["operator"])
                case "locate": result = store.locate(args.id, args.root_ids)
                case "relink": result = store.relink(args.id, args.root_id, args.relative_path)
                case "amend": result = store.amend(args.id, config["operator"], args.reason, read_json(args.corrections_json))
                case "run-python":
                    from .analysis import run_python
                    result = run_python(store, args.code_repo, args.entry_point, args.input_ids,
                                        read_json(args.parameters_json), trusted=args.trusted)
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0
    except Exception as exc:
        parser.exit(1, f"Error: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
