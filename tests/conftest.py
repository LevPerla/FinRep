import os


# Existing tests exercise the legacy CSV adapters unless they opt into SQLite.
os.environ.setdefault("FINREP_STORAGE_BACKEND", "csv")
