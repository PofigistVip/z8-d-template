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
parser.add_argument("--openxlsx-dir", type=Path)
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
    extra = ["--openxlsx-dir", str(options.openxlsx_dir.resolve())] if options.openxlsx_dir else []
    subprocess.run([sys.executable, str(stage / "build.py"), "--z8-d", str(root), "--postgres-bin", str(pg), *extra], env=env, check=True)
    resources = root / "vendor/z8/org.zenframework.z8.server/WEB-INF/resources"
    original_resources = sorted(resources.glob("*.nls"))
    assert original_resources, "The bundled server translations are missing"
    for original_resource in original_resources:
        assert (stage / "build/resources" / original_resource.name).read_bytes() == original_resource.read_bytes()
    if options.openxlsx_dir:
        bridge = stage / "build" / ("z8_openxlsx.dll" if os.name == "nt" else "libz8_openxlsx.so")
        assert bridge.is_file(), "OpenXLSX library was not deployed"
    if os.name == "nt":
        invalid = stage / "invalid-ldap.properties"
        invalid.write_text("ldap.read.pageSize=-1\n", encoding="ascii")
        env["PATH"] = str(pg) + os.pathsep + env.get("PATH", "")
        failed = subprocess.run([str(stage / "build/z8-template.exe"), "unused", "public", "--config", str(invalid)],
                                env=env, capture_output=True, text=True)
        output = failed.stdout + failed.stderr
        assert failed.returncode and "LDAP timeout and page size must be nonnegative" in output, (failed.returncode, output)
    subprocess.run([sys.executable, str(stage / "tests/e2e.py"), "--z8-d", str(root), "--postgres-bin", str(pg)] + (["--openxlsx"] if options.openxlsx_dir else []), env=env, check=True)
print("Template built and ran without an original Z8 checkout")
