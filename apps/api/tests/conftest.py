"""Test data is isolated from both the developer DB and Docker's real XML."""
import os, tempfile
from pathlib import Path
_test_root=Path(tempfile.mkdtemp(prefix="nfe-tests-"))
os.environ["DATABASE_URL"]="sqlite:///"+(_test_root/"test.sqlite").as_posix()
for key,directory in (("OBJECTS_DIR","objects"),("STAGING_DIR","staging"),("EXPORTS_DIR","exports"),("BACKUPS_DIR","backups")):
    os.environ[key]=str(_test_root/directory)
