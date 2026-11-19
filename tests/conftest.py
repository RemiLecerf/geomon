"""Unit tests: pure computations, no database, no network.

Run them in the application image, on the working copy:

    docker compose run --rm test
"""

import os
import tempfile

# Settings are read when geomon is imported: point the data directory at a scratch folder, so
# that no test can touch real data (and no sites.json is found: the single site of the
# environment is used).
os.environ["GEOMON_DATA_DIR"] = tempfile.mkdtemp(prefix="geomon-test-")
os.environ.pop("GEOMON_SITES_FILE", None)
os.environ.setdefault("GEOMON_COMMUNES", "38259")
