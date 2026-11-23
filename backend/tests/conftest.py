import os
import sys
import tempfile

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
os.environ.setdefault("NESTING_DATA_DIR", tempfile.mkdtemp(prefix="nesting_test_"))
