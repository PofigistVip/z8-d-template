"""Build the Z8 D template and assemble its original web client."""
from pathlib import Path
import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile

project = Path(__file__).resolve().parent
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--z8-d", type=Path, default=os.environ.get("Z8_D_ROOT"), required=not os.environ.get("Z8_D_ROOT"), help="Path to the z8-d checkout")
parser.add_argument("--original-z8", type=Path, default=os.environ.get("Z8_ORIGINAL_ROOT"), required=not os.environ.get("Z8_ORIGINAL_ROOT"), help="Path to the original org.zenframework.z8 checkout")
parser.add_argument("--postgres-bin", type=Path, help="PostgreSQL bin directory (or set Z8_PG_BIN)")
parser.add_argument("--core-library", type=Path, help="Use an existing compiled Z8 core library")
parser.add_argument("--library", type=Path, action="append", default=[], help="Additional compiled BL library (repeatable)")
options = parser.parse_args()
root, original = options.z8_d.resolve(), options.original_z8.resolve()
language = original / "org.zenframework.z8.lang/src/bl"
compiler = root / ("z8d.exe" if os.name == "nt" else "z8d")
if not language.is_dir() or not (root / "runtime/source/z8/runtime.d").is_file():
    raise SystemExit("Check the paths to z8-d and the original Z8 checkout")
if not compiler.is_file():
    raise SystemExit("Build the compiler first: dub build --compiler=dmd in z8-d")
pg_bin = options.postgres_bin or (Path(os.environ["Z8_PG_BIN"]) if os.environ.get("Z8_PG_BIN") else None)
if pg_bin is None and shutil.which("pg_config"):
    pg_bin = Path(subprocess.check_output(["pg_config", "--bindir"], text=True).strip())
if pg_bin is None:
    raise SystemExit("Pass --postgres-bin or set Z8_PG_BIN")
pg_bin = pg_bin.resolve()
if os.name == "nt" and not (pg_bin.parent / "lib/libpq.lib").is_file():
    raise SystemExit("PostgreSQL libpq.lib was not found")
sys.path.insert(0, str(root / "webserver"))
from build_civetweb import build
from prepare_client import assemble


def core_fingerprint():
    """Invalidate the local core cache when its inputs or D compiler change."""
    digest = hashlib.sha256()
    digest.update(subprocess.check_output(["dmd", "--version"]))
    for label, base, patterns in (("compiler", root, (compiler.name,)),
                                  ("language", language, ("*.bl",)),
                                  ("platform", root / "platform/bl", ("*.bl",)),
                                  ("runtime", root / "runtime/source", ("*.d", "*.txt"))):
        paths = [compiler] if label == "compiler" else sorted(path for pattern in patterns for path in base.rglob(pattern) if path.is_file())
        for path in paths:
            digest.update(label.encode() + b"/" + path.relative_to(base).as_posix().encode("utf-8") + b"\0")
            with path.open("rb") as source:
                while chunk := source.read(1024 * 1024):
                    digest.update(chunk)
            digest.update(b"\0")
    return digest.hexdigest()


def link_order(paths):
    """Link dependents before their dependencies, as required on Unix."""
    libraries = [(path, json.loads((path / "z8-library.json").read_text(encoding="utf-8"))) for path in paths]
    by_id = {manifest["id"]: (path, manifest) for path, manifest in libraries}
    ordered, visited = [], set()

    def visit(item):
        path, manifest = item
        if manifest["id"] in visited:
            return
        visited.add(manifest["id"])
        for requirement in manifest["requires"]:
            visit(by_id[requirement["id"]])
        ordered.append(path)

    for item in libraries:
        visit(item)
    return list(reversed(ordered))


def application_sources(folder):
    base = (project / "src/main" / folder).resolve()
    for line in (base / ".buildorder").read_text(encoding="utf-8").splitlines():
        name = line.strip()
        if not name or name.startswith("#"):
            continue
        path = (base / name).resolve()
        if base not in path.parents or not path.is_file():
            raise ValueError(f"Invalid application build-order entry: {name}")
        yield path


build_root = project / "build"
if build_root.is_symlink() or build_root.resolve() != build_root:
    raise SystemExit("The build directory must be inside the template project")
build_root.mkdir(parents=True, exist_ok=True)
generated = build_root / "generated"
if generated.is_symlink() or generated.resolve() != generated:
    raise SystemExit("The generated directory must be inside the build directory")
if generated.exists():
    if not generated.is_dir():
        raise SystemExit("The generated path is not a directory")
    shutil.rmtree(generated)
generated.mkdir()

core = options.core_library.resolve() if options.core_library else root / "build/libraries/core"
if options.core_library:
    if not (core / "z8-library.json").is_file():
        raise SystemExit("The compiled core library was not found")
else:
    fingerprint = core_fingerprint()
    marker = core / ".source-fingerprint"
    if not marker.is_file() or marker.read_text(encoding="ascii").strip() != fingerprint:
        subprocess.run([str(compiler), "library", str(language), str(core), "core",
                        "--dependency", str(root / "platform/bl")], check=True)
        marker.write_text(fingerprint + "\n", encoding="ascii")
libraries = [core, *(path.resolve() for path in options.library)]

# Keep the generated D modules for inspection. The compiler's temporary
# objects and libraries are created here too, then removed after the build.
with tempfile.TemporaryDirectory(prefix=".build-", dir=generated) as directory:
    temp = Path(directory)
    command = [str(compiler), "project", str(project / "src/main/bl"), str(generated)]
    for library in libraries:
        command += ["--library", str(library)]
    subprocess.run(command, check=True)
    runtime_source = root / "runtime/source"
    runtime = temp / ("z8-runtime.lib" if os.name == "nt" else "libz8-runtime.a")
    subprocess.run(["dmd", "-lib", "-I" + str(runtime_source), "-J" + str(runtime_source),
                    str(runtime_source / "z8/runtime.d"), str(runtime_source / "z8/runtime_source.d"),
                    "-od=" + str(temp), "-of=" + str(runtime)], cwd=temp, check=True)
    civet = temp / ("civetweb.obj" if os.name == "nt" else "civetweb.o")
    build(civet)
    output = project / "build" / ("z8-template.exe" if os.name == "nt" else "z8-template")
    output.parent.mkdir(parents=True, exist_ok=True)
    args = ["-I" + str(generated), *("-I" + str(library / "d") for library in libraries), "-I" + str(root / "postgres/source"),
            "-I" + str(root / "webserver/source"), "-I" + str(root / "integrations/postgres_webserver/source"),
            "-I" + str(runtime_source), "-J" + str(runtime_source), *map(str, sorted(generated.rglob("*.d"))),
            *map(str, (root / "postgres/source/z8/database").glob("*.d")),
            str(root / "webserver/source/z8/webserver/civetweb.d"),
            str(root / "integrations/postgres_webserver/source/z8/integration/postgres_authority.d"),
            str(project / "host.d"), str(civet),
            *(str(library / ("z8-library.lib" if os.name == "nt" else "libz8-library.a")) for library in link_order(libraries)),
            str(runtime), "-of=" + str(output)]
    args += [str(pg_bin.parent / "lib/libpq.lib")] if os.name == "nt" else ["-L-lpq", "-L-lpthread", "-L-ldl"]
    response = temp / "dmd.rsp"
    response.write_text("\n".join('"' + arg.replace('\\', '/') + '"' for arg in args), encoding="utf-8")
    subprocess.run(["dmd", "@" + str(response)], cwd=temp, check=True)

    public = temp / "public"
    assemble(original, public)
    for folder, base_name, target in (("js", "revolt.js", "z8-template.js"),
                                       ("css", "css/revolt.css", "css/z8-template.css")):
        content = (public / base_name).read_bytes() + b"\n"
        content += b"\n".join(path.read_bytes() for path in application_sources(folder)) + b"\n"
        (public / target).write_bytes(content)
    index = (project / "src/main/html/index.html").read_bytes()
    (public / "index.html").write_bytes(index.replace(b"${project.name}", b"z8-template"))
    debug = public / "debug"
    (debug / "css").mkdir(parents=True)
    shutil.copyfile(public / "z8-template.js", debug / "z8-template.js")
    shutil.copyfile(public / "css/z8-template.css", debug / "css/z8-template.css")
    for name in ("fonts", "img"):
        shutil.copytree(public / "css" / name, debug / "css" / name)
    index = (project / "src/main/html/debug.html").read_bytes()
    (public / "debug.html").write_bytes(index.replace(b"${project.name}", b"z8-template"))
    destination = project / "build/public"
    if destination.exists():
        shutil.rmtree(destination)
    shutil.copytree(public, destination)
print("Built", output)
print("Generated D modules", generated)
print("Prepared client", destination)
