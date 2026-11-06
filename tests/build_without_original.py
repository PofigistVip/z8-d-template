"""Build and exercise a temporary template copy using only the D checkout."""
from pathlib import Path
import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile

project = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--z8-d", type=Path, required=True)
parser.add_argument("--postgres-bin", type=Path, required=True)
options = parser.parse_args()
root, pg = options.z8_d.resolve(), options.postgres_bin.resolve()

with tempfile.TemporaryDirectory() as directory:
    stage = Path(directory)
    shutil.copytree(project / "src", stage / "src")
    shutil.copytree(project / "tests", stage / "tests")
    for name in ("build.py", "host.d", "server.properties"):
        shutil.copy2(project / name, stage / name)
    env = dict(os.environ, Z8_ORIGINAL_ROOT=str(stage / "missing-original"))
    incompatible = stage / "incompatible-library"
    incompatible.mkdir()
    (incompatible / "z8-library.json").write_text(json.dumps({"id": "old", "compiler": "another DMD"}), encoding="utf-8")
    failed = subprocess.run([sys.executable, str(stage / "build.py"), "--z8-d", str(root), "--postgres-bin", str(pg),
                             "--library", str(incompatible)], env=env, capture_output=True, text=True)
    assert failed.returncode and "another DMD version" in failed.stderr, failed.stderr
    subprocess.run([sys.executable, str(stage / "build.py"), "--z8-d", str(root), "--postgres-bin", str(pg)], env=env, check=True)
    subprocess.run([sys.executable, str(stage / "tests/e2e.py"), "--z8-d", str(root), "--postgres-bin", str(pg)], env=env, check=True)
print("Template built and ran without an original Z8 checkout")
