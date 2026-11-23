import argparse
from pathlib import Path
import sys
import traceback

from jobs import JobStore, default_data_dir, run_job


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", type=Path, default=default_data_dir())
    command = parser.add_mutually_exclusive_group()
    command.add_argument("--run", metavar="JOB_ID")
    command.add_argument("--thread", metavar="THREAD_ID", default="")
    args = parser.parse_args()
    try:
        store = JobStore(args.data_dir)
        if args.run:
            return run_job(store, args.run)
        from ui import launch
        launch(store, args.thread)
        return 0
    except Exception as exc:
        try:
            args.data_dir.mkdir(parents=True, exist_ok=True)
            with (args.data_dir / "errors.log").open("a", encoding="utf-8") as log:
                traceback.print_exc(file=log)
        except OSError:
            pass
        if not args.run:
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, str(exc), "Codex Auto Resume", 0x10)
        elif sys.stderr is not None:
            traceback.print_exc()
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
