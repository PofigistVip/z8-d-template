"""Build the Z8 D template and assemble its original web client."""
from pathlib import Path
import argparse
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

# Keep the generated D modules for inspection. The compiler's temporary
# objects and libraries are created here too, then removed after the build.
with tempfile.TemporaryDirectory(prefix=".build-", dir=generated) as directory:
    temp = Path(directory)
    subprocess.run([str(compiler), "project", str(project / "src/main/bl"), str(generated),
                    "--dependency", str(language), "--dependency", str(root / "platform/bl")], check=True)
    runtime_source = root / "runtime/source"
    runtime = temp / ("z8-runtime.lib" if os.name == "nt" else "libz8-runtime.a")
    subprocess.run(["dmd", "-lib", "-I" + str(runtime_source), "-J" + str(runtime_source),
                    str(runtime_source / "z8/runtime.d"), str(runtime_source / "z8/runtime_source.d"),
                    "-od=" + str(temp), "-of=" + str(runtime)], cwd=temp, check=True)
    civet = temp / ("civetweb.obj" if os.name == "nt" else "civetweb.o")
    build(civet)
    output = project / "build" / ("z8-template.exe" if os.name == "nt" else "z8-template")
    output.parent.mkdir(parents=True, exist_ok=True)
    args = ["-I" + str(generated), "-I" + str(root / "postgres/source"),
            "-I" + str(root / "webserver/source"), "-I" + str(root / "integrations/postgres_webserver/source"),
            "-I" + str(runtime_source), "-J" + str(runtime_source), *map(str, sorted(generated.rglob("*.d"))),
            *map(str, (root / "postgres/source/z8/database").glob("*.d")),
            str(root / "webserver/source/z8/webserver/civetweb.d"),
            str(root / "integrations/postgres_webserver/source/z8/integration/postgres_authority.d"),
            str(project / "host.d"), str(civet), str(runtime), "-of=" + str(output)]
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
