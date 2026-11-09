"""Headless iOS simulator transport for rune-dev. DX compiles only; simctl owns launch."""

import json
import os
from pathlib import Path
import plistlib
import secrets
import shlex
import shutil
import subprocess
import tempfile
import time
import uuid


def atomic_bytes(path, data):
    temp = path.with_suffix(".rune-tmp")
    temp.write_bytes(data)
    temp.replace(path)


def read_json(path):
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return None


def wait_for(path, timeout=20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = read_json(path)
        if value is not None:
            return value
        time.sleep(0.05)
    raise RuntimeError(f"simulator did not write {path.name}; check the app's console")


def dx_result(dx, command, flags, cwd, env, field, stdin=None):
    """Read the structured result without printing the compiler's captured environment."""
    result = subprocess.run(
        [str(dx), *command, "--json-output", *flags], cwd=cwd, env=env,
        input=stdin, capture_output=True, text=True,
    )
    payload = None
    for line in result.stdout.splitlines():
        try:
            row = json.loads(line)
            if "json" in row:
                payload = json.loads(row["json"]).get(field, payload)
            if row.get("level") in ("WARN", "ERROR") and "message" in row:
                print(row["message"], flush=True)
        except (ValueError, AttributeError):
            continue
    if result.returncode or payload is None:
        raise RuntimeError(f"dx {' '.join(command)} failed: {result.stderr.strip()}")
    return payload


def simctl(sim, action, *args, options=(), env=None, check=True):
    result = subprocess.run(
        ["xcrun", "simctl", action, *options, sim, *args], env=env,
        capture_output=True, text=True,
    )
    if check and result.returncode:
        raise RuntimeError(f"simctl {action}: {result.stderr.strip()}")
    return result


def run_simulator(opts, dx, package, cargo_config, app_args, env, mirror, crate,
                  allowed, guard, baseline, read_sources, restart):
    try:
        sim = str(uuid.UUID(opts.ios_simulator))
    except ValueError:
        opts.parser.error("--ios-simulator expects a simulator UDID")
    if os.uname().sysname != "Darwin" or os.uname().machine != "arm64":
        opts.parser.error("--ios-simulator needs an Apple silicon Mac")
    simctl(sim, "bootstatus", "-b")

    cwd = mirror / crate
    features = ",".join(filter(None, ("hot", opts.features)))
    flags = ["--platform", "ios", "--target", "aarch64-apple-ios-sim", "--package", package,
             "--features", features, "--profile", "rune-hot"]
    if opts.bin:
        flags += ["--bin", opts.bin]
    if cargo_config:
        flags += ["--cargo-args=" + shlex.join(arg for c in cargo_config for arg in ("--config", c))]
    if opts.prepare:
        script = (cwd / opts.prepare).resolve()
        if not script.is_relative_to(cwd) or not script.is_file():
            opts.parser.error("--prepare must name a script inside the app crate")
        archive = (mirror / "rune-ios-shell.a").resolve()
        subprocess.run(["/bin/bash", str(script), str(archive)], cwd=cwd, env=env, check=True)
        flags += ["--rustc-args=" + shlex.quote(f"-Clink-arg={archive}")]

    with tempfile.TemporaryDirectory(prefix="rune-ios-hot-") as tmp:
        flags += ["--session-cache-dir", tmp]
        print("rune-hot: building simulator patch base", flush=True)
        artifacts = dx_result(dx, ["build", "--fat-binary"], flags, cwd, env, "BuildsFinished")["client"]
        rustc_args = artifacts["rustc_args"]
        crate_name = Path(artifacts["exe"]).name.replace("-", "_")
        if "--crate-name" not in rustc_args or rustc_args[rustc_args.index("--crate-name") + 1] != crate_name:
            raise RuntimeError("dx did not export the application binary's rustc arguments; use --prepare for the shell")
        bundle = Path(artifacts["path"])
        identifier = plistlib.loads((bundle / "Info.plist").read_bytes())["CFBundleIdentifier"]
        if not identifier.endswith(".hot"):
            raise RuntimeError("the simulator hot bundle identifier must end in .hot to isolate app data")

        simctl(sim, "install", str(bundle))
        if opts.sim_microphone:
            simctl(sim, "privacy", "grant", "microphone", identifier)
        folder = Path(simctl(sim, "get_app_container", identifier, "data").stdout.strip())
        token = secrets.token_hex(16)
        mailbox = folder / "tmp" / f"rune-hot-{token}"
        launch_env = dict(env, SIMCTL_CHILD_RUNE_IOS_HOT=token, SIMCTL_CHILD_RUNE_HOT="1",
                          SIMCTL_CHILD_RUNE_DEV_GUARDED="1")
        simctl(sim, "launch", identifier, *app_args, options=("--terminate-running-process",), env=launch_env)
        try:
            ready = wait_for(mailbox / "ready.json", timeout=40)
            print(f"rune-hot: simulator ready (pid {ready['pid']})", flush=True)
            generation = 0
            candidate = Path(tmp) / "candidate.rs"
            rejected = None
            while True:
                time.sleep(0.15)
                try:
                    current = read_sources()
                except OSError:
                    continue
                if current == baseline or current == rejected:
                    continue
                changed = [p for p, data in current.items() if baseline.get(p) != data]
                incompatible = (set(current) ^ set(baseline)) | {p for p in changed if not allowed(p)}
                if incompatible:
                    print("rune-hot: restart needed: " + ", ".join(sorted(incompatible)), flush=True)
                    raise restart()
                invalid = False
                for path in changed:
                    candidate.write_bytes(current[path])
                    code = subprocess.run([str(guard), str(mirror / path), str(candidate)]).returncode
                    if code == 3:
                        raise restart()
                    invalid |= code != 0
                if invalid:
                    rejected = current
                    continue
                for path in changed:
                    atomic_bytes(mirror / path, current[path])
                baseline = current
                rejected = None
                start = time.monotonic()
                try:
                    patch = dx_result(dx, ["tools", "hotpatch", "--aslr-reference", str(ready["aslr"])],
                                      flags, cwd, env, "Hotpatch", json.dumps(artifacts))
                except RuntimeError as error:
                    print(f"rune-hot: {error}; waiting for the next edit", flush=True)
                    continue
                table = patch["jump_table"]
                generation += 1
                lib = f"patch-{generation}.dylib"
                shutil.copyfile(table["lib"], mailbox / lib)
                table["lib"] = lib
                atomic_bytes(mailbox / "patch.json", json.dumps({
                    **ready, "generation": generation, "table": table
                }).encode())
                wait_for_patch(mailbox, ready["pid"], generation)
                elapsed = round((time.monotonic() - start) * 1000)
                print(f"rune-hot: simulator presented patch {generation} (pid {ready['pid']}, {elapsed} ms)", flush=True)
        finally:
            simctl(sim, "terminate", identifier, check=False)
            shutil.rmtree(mailbox, ignore_errors=True)


def wait_for_patch(mailbox, pid, generation):
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        error = read_json(mailbox / "error.json")
        if error and error["generation"] == generation:
            raise RuntimeError(error["error"])
        frame = read_json(mailbox / "presented.json")
        if frame and frame["pid"] == pid and frame["generation"] == generation:
            return
        time.sleep(0.05)
    raise RuntimeError(f"simulator did not present patch {generation}")
