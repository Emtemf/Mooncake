"""Build, replace and smoke-test Mooncake in an explicit Linux virtual environment."""

import argparse
import ctypes
import importlib.metadata
import json
import mmap
import os
import re
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
import uuid
from pathlib import Path

DIST = "mooncake-transfer-engine"
DIST_VARIANTS = (DIST, "mooncake-transfer-engine-npu")
PHASES = (
    "phase1_start_batch_put_us",
    "phase2_submit_transfers_us",
    "phase3_wait_transfers_us",
    "phase4_finalize_batch_put_us",
)


def run(args, **kwargs):
    print("+", " ".join(map(str, args)), flush=True)
    return subprocess.run(list(map(str, args)), check=True, **kwargs)


def validate_timing(text):
    lines = [line for line in text.splitlines() if "[BatchPutTiming]" in line]
    if not any(
        "status=ok" in line
        and "complete=true" in line
        and all(re.search(rf"\b{field}=\d+\b", line) for field in PHASES)
        for line in lines
    ):
        raise RuntimeError("No successful four-phase BatchPutTiming record; see smoke-client.log")


def probe():
    from mooncake import engine, store

    prefix = Path(sys.prefix).resolve()
    result = {}
    for name, module in (("engine", engine), ("store", store)):
        path = Path(module.__file__).resolve()
        if not path.is_relative_to(prefix):
            raise RuntimeError(f"{name} is shadowed outside target environment: {path}")
        run(["ldd", path])
        result[name] = str(path)
    # The timing implementation may be statically linked or in a loaded shared library.
    libraries = {Path(store.__file__).resolve()}
    for line in Path("/proc/self/maps").read_text().splitlines():
        value = line.split()[-1]
        if value.startswith("/") and ".so" in value:
            libraries.add(Path(value))
    marked = []
    for path in libraries:
        if path.is_file() and path.stat().st_size:
            with path.open("rb") as file, mmap.mmap(file.fileno(), 0, access=mmap.ACCESS_READ) as data:
                if data.find(b"phase4_finalize_batch_put_us=") >= 0:
                    marked.append(str(path))
    if not marked:
        raise RuntimeError("Loaded libraries do not contain the four-phase timing implementation")
    result["timing_libraries"] = marked
    print(json.dumps(result, indent=2), flush=True)


def smoke_client(port):
    from mooncake.store import MooncakeDistributedStore

    client = MooncakeDistributedStore()
    rc = client.setup("127.0.0.1:0", "P2PHANDSHAKE", 64 << 20, 16 << 20, "tcp", "", f"127.0.0.1:{port}")
    if rc != 0:
        raise RuntimeError(f"Smoke store setup failed: {rc}")
    payloads = [b"ttft-native-a" * 256, b"ttft-native-b" * 256]
    buffers = [ctypes.create_string_buffer(data) for data in payloads]
    keys = [f"ttft-installer-{uuid.uuid4().hex}" for _ in buffers]
    registered = []
    try:
        for buf in buffers:
            ptr = ctypes.addressof(buf)
            if client.register_buffer(ptr, ctypes.sizeof(buf)) != 0:
                raise RuntimeError("Smoke buffer registration failed")
            registered.append(ptr)
        result = client.batch_put_from_multi_buffers(
            keys, [[ctypes.addressof(buf)] for buf in buffers], [[len(data)] for data in payloads]
        )
        if list(result) != [0, 0]:
            raise RuntimeError(f"Smoke batch put failed: {result}")
        for key, expected in zip(keys, payloads):
            actual = client.get(key)
            if actual != expected:
                raise RuntimeError(f"Smoke GET payload mismatch: {key}")
        print("PASS: two registered-buffer PUT/GET payloads match", flush=True)
    finally:
        for ptr in registered:
            client.unregister_buffer(ptr)


def snapshot(destination):
    installed = []
    for name in DIST_VARIANTS:
        try:
            installed.append(importlib.metadata.distribution(name))
        except importlib.metadata.PackageNotFoundError:
            pass
    if len(installed) != 1:
        raise RuntimeError("Expected exactly one Mooncake standard/NPU distribution in target environment")
    dist = installed[0]
    if dist.read_text("direct_url.json"):
        direct = json.loads(dist.read_text("direct_url.json"))
        if direct.get("dir_info", {}).get("editable"):
            raise RuntimeError("Editable Mooncake install: use a dedicated runtime environment first")
    prefix = Path(sys.prefix).resolve()
    files = []
    for name in dist.files or ():
        path = Path(dist.locate_file(name)).resolve()
        if not path.is_relative_to(prefix):
            raise RuntimeError(f"Existing distribution has files outside target environment: {path}")
        if path.is_file():
            files.append(path)
    if not files:
        raise RuntimeError("Existing Mooncake distribution has no backupable file manifest")
    with tarfile.open(destination / "previous.tar.gz", "w:gz") as archive:
        for path in files:
            archive.add(path, arcname=path.relative_to(prefix), recursive=False)
    manifest = {"prefix": str(prefix), "version": dist.version, "distribution": dist.metadata["Name"]}
    (destination / "backup.json").write_text(json.dumps(manifest, indent=2))


def restore(destination):
    manifest = json.loads((destination / "backup.json").read_text())
    prefix = Path(sys.prefix).resolve()
    if prefix != Path(manifest["prefix"]):
        raise RuntimeError("Rollback must use the original target interpreter")
    try:
        importlib.metadata.distribution(DIST)
    except importlib.metadata.PackageNotFoundError:
        pass
    else:
        run(["uv", "pip", "uninstall", "--python", sys.executable, DIST])
    with tarfile.open(destination / "previous.tar.gz") as archive:
        for member in archive.getmembers():
            if not member.isfile() or not (prefix / member.name).resolve().is_relative_to(prefix):
                raise RuntimeError("Unsafe backup member")
        for member in archive.getmembers():
            target = prefix / member.name
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.extractfile(member) as source, target.open("wb") as output:
                shutil.copyfileobj(source, output)
            target.chmod(member.mode)
    print("Restored previous Mooncake files. Restart serving processes before reuse.")


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def smoke(destination):
    dist = importlib.metadata.distribution(DIST)
    master = Path(dist.locate_file("mooncake/mooncake_master"))
    if not master.is_file():
        raise RuntimeError("Installed wheel does not contain mooncake_master")
    rpc, metrics = free_port(), free_port()
    while rpc == metrics:
        metrics = free_port()
    env = {**os.environ, "GLOG_logtostderr": "1", "GLOG_minloglevel": "0"}
    with (destination / "smoke-master.log").open("w") as output:
        proc = subprocess.Popen(
            [str(master), f"--rpc_port={rpc}", "--rpc_address=127.0.0.1",
             f"--metrics_port={metrics}", "--metrics_host=127.0.0.1", "--enable_http_metadata_server=false"],
            stdout=output, stderr=subprocess.STDOUT, env=env,
        )
        try:
            deadline = time.monotonic() + 30
            while True:
                if proc.poll() is not None:
                    raise RuntimeError("Temporary master exited; see smoke-master.log")
                try:
                    with socket.create_connection(("127.0.0.1", rpc), timeout=0.5):
                        break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise RuntimeError("Temporary master readiness timed out")
                    time.sleep(0.2)
            with (destination / "smoke-client.log").open("w") as log:
                run([sys.executable, __file__, "--smoke-client", str(rpc)],
                    stdout=log, stderr=subprocess.STDOUT, env=env, timeout=90)
            validate_timing((destination / "smoke-client.log").read_text())
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()


def cached_settings(path):
    settings = {}
    for line in path.read_text().splitlines():
        match = re.fullmatch(r"([^:#][^:]*):([^=]+)=(.*)", line)
        if not match:
            continue
        key, kind, value = match.groups()
        if kind == "BOOL" and key.startswith(("USE_", "WITH_", "STORE_USE_", "ENABLE_", "BUILD_")):
            settings[key] = value
        elif key in ("CMAKE_PREFIX_PATH", "CMAKE_C_COMPILER", "CMAKE_CXX_COMPILER", "CMAKE_BUILD_TYPE"):
            settings[key] = value
    if not any(settings.get(key) in ("ON", "TRUE", "1") for key in ("USE_ASCEND", "USE_ASCEND_DIRECT")):
        raise RuntimeError("Existing cache does not enable Ascend; supply the correct --cache")
    return settings


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--transport", choices=("ascend-direct", "ascend"))
    parser.add_argument("--cache", type=Path, help="Reuse feature/toolchain settings from an existing CMakeCache.txt")
    parser.add_argument("--jobs", type=int, default=4)
    parser.add_argument("--cmake", action="append", default=[], help="Extra CMake KEY=VALUE; repeat as needed")
    parser.add_argument("--rollback", type=Path)
    parser.add_argument("--probe", action="store_true")
    parser.add_argument("--smoke-client", type=int)
    args = parser.parse_args()
    if args.probe:
        return probe()
    if args.smoke_client:
        return smoke_client(args.smoke_client)
    if sys.platform != "linux" or sys.prefix == sys.base_prefix:
        parser.error("Run on Linux with uv run --no-project /path/to/runtime/.venv/bin/python")
    if not shutil.which("uv"):
        parser.error("uv must be on PATH")
    if args.rollback:
        return restore(args.rollback.resolve())
    if args.jobs < 1:
        parser.error("--jobs must be positive")
    for tool in ("cmake", "c++", "ldd", "git"):
        if not shutil.which(tool):
            parser.error(f"Missing build prerequisite: {tool}")
    root = Path(__file__).resolve().parents[1]
    timing_source = root / "mooncake-store/src/client_service.cpp"
    if "phase4_finalize_batch_put_us=" not in timing_source.read_text():
        parser.error("Source lacks TTFT native patch; do not replace the installed package")
    cache = args.cache or root / "build/CMakeCache.txt"
    if not cache.is_file() and args.transport is None:
        parser.error("Supply --cache /existing/build/CMakeCache.txt or explicitly choose --transport")
    destination = Path(tempfile.mkdtemp(prefix="mooncake-ttft-", dir=Path.home()))
    shutil.copy2(__file__, destination / "installer.py")
    print(f"Build logs, backup and rollback helper: {destination}", flush=True)
    snapshot(destination)
    revision = subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip()
    (destination / "source-revision.txt").write_text(revision + "\n")
    settings = cached_settings(cache) if cache.is_file() else {
        "USE_ASCEND_DIRECT": "ON" if args.transport == "ascend-direct" else "OFF",
        "USE_ASCEND": "ON" if args.transport == "ascend" else "OFF",
        "USE_CUDA": "OFF", "USE_ETCD": "OFF", "USE_HTTP": "OFF", "USE_REDIS": "OFF",
    }
    settings.update({
        "USE_TCP": "ON", "WITH_TE": "ON", "WITH_STORE": "ON", "WITH_EP": "OFF",
        "WITH_STORE_RUST": "OFF", "WITH_STORE_GO": "OFF", "WITH_P2P_STORE": "OFF",
        "BUILD_UNIT_TESTS": "OFF", "BUILD_EXAMPLES": "OFF", "BUILD_BENCHMARK": "OFF",
    })
    for value in args.cmake:
        key, sep, setting = value.partition("=")
        if not sep or not key or not setting:
            parser.error("--cmake requires KEY=VALUE")
        settings[key] = setting
    (destination / "build-settings.json").write_text(json.dumps(settings, indent=2))
    command = ["uv", "build", "--wheel", "--python", sys.executable, "--out-dir", str(destination / "wheel"),
               "--config-setting", f"build-dir={destination / 'build'}"]
    for key, value in settings.items():
        command += ["--config-setting", f"cmake.define.{key}={value}"]
    with (destination / "build.log").open("w") as log:
        run(command, cwd=root, env={**os.environ, "CMAKE_BUILD_PARALLEL_LEVEL": str(args.jobs)},
            stdout=log, stderr=subprocess.STDOUT)
    wheels = list((destination / "wheel").glob("*.whl"))
    if len(wheels) != 1:
        raise RuntimeError("Expected exactly one built wheel")
    try:
        previous = json.loads((destination / "backup.json").read_text())["distribution"]
        if previous != DIST:
            run(["uv", "pip", "uninstall", "--python", sys.executable, previous])
        run(["uv", "pip", "install", "--python", sys.executable, "--reinstall", "--no-deps", wheels[0]])
        with (destination / "loaded-library.log").open("w") as log:
            run([sys.executable, __file__, "--probe"], cwd=destination,
                stdout=log, stderr=subprocess.STDOUT, timeout=45)
        smoke(destination)
    except BaseException:
        restore(destination)
        raise
    print(f"PASS: build/install, loaded native timing, TCP PUT/GET and four-phase logs. Evidence: {destination}")
    print("This is a local TCP smoke, not Ascend RDMA/HCCS or model-output validation.")
    print("Restart vLLM workers so they load the new library. Existing master was not touched.")
    print(f"Rollback: uv run --no-project {sys.executable} {destination / 'installer.py'} --rollback {destination}")


if __name__ == "__main__":
    main()
