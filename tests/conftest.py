"""Use a throw-away procurement database for the whole test session (set before any app import)."""
import os
import tempfile

os.environ.setdefault("PROC_DB_URL", f"sqlite:///{tempfile.mkdtemp(prefix='constructai_proc_test_')}/procurement.db")
