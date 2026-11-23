"""Stop future sends during uninstall while retaining the job history."""

from jobs import RUNNING_STATES, JobStore, default_data_dir


def main():
    root = default_data_dir()
    if not (root / "jobs.sqlite3").is_file():
        return
    store = JobStore(root)
    store.reconcile_workers()
    for job in store.all():
        try:
            if job["state"] == "scheduled":
                store.cancel(job["id"])
            elif job["state"] in RUNNING_STATES and job["continue_enabled"]:
                store.stop_followups(job["id"])
        except RuntimeError:
            current = store.get(job["id"])
            if current["state"] == "scheduled":
                raise
            if current["state"] in RUNNING_STATES and current["continue_enabled"]:
                store.stop_followups(job["id"])


if __name__ == "__main__":
    main()
