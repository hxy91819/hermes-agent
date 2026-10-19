#!/usr/bin/env python3
"""Package one verified PM payload for standalone and legacy image consumers."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
import sys
import tomllib


def file_sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def package(payload: Path, output: Path, tag: str, source: Path) -> None:
    commit = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=source, text=True, timeout=30
    ).strip()
    base = (source / ".fork/branches").read_text(encoding="utf-8-sig")
    version = next(
        line.split()[1].removeprefix("v")
        for line in base.splitlines()
        if line.startswith("base ")
    )
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from scripts.write_install_stamp import write_stamp

    write_stamp(
        payload / "hermes-agent/install-stamp.json", commit=commit,
        base_version=version, distance=0, display_version=version,
        source="fork-release", update_mechanism="external", runtime_dir="..",
    )
    output.mkdir()
    with tarfile.open(
        output / "hermes-ubuntu-x86_64.tar.gz", "w:gz", dereference=False, compresslevel=1
    ) as archive:
        archive.add(payload, arcname=".")
    print("standalone archive complete", flush=True)
    stage = output / "image-stage"
    top = stage / "hermes-agent"
    shutil.copytree(payload / "hermes-agent", top, symlinks=True)
    # Upstream deliberately excludes scripts from its runtime snapshot.
    with tempfile.TemporaryDirectory(prefix="hermes-image-scripts-") as scratch:
        archived = Path(scratch) / "scripts.tar"
        subprocess.run(
            ["git", "archive", "--format=tar", "--output", str(archived), commit, "scripts"],
            cwd=source, check=True, timeout=60,
        )
        with tarfile.open(archived) as scripts:
            scripts.extractall(top, filter="data")
    # The legacy image verifies pyproject version against src_version before running stages.
    project = top / "pyproject.toml"
    old_version = tomllib.loads(project.read_text(encoding="utf-8-sig"))["project"][
        "version"
    ]
    text = project.read_text(encoding="utf-8-sig").replace(
        f'version = "{old_version}"', f'version = "{version}"', 1
    )
    project.write_text(text, encoding="utf-8")
    shutil.move(top / "scripts/install.sh", top / "scripts/install-upstream.sh")
    shutil.copy2(source / "scripts/fork-image-install.sh", top / "scripts/install.sh")
    shutil.move(payload, top / ".fork-payload")
    (top / "venv").symlink_to(".fork-payload/venv", target_is_directory=True)
    command = top / ".fork-payload/venv/bin/hermes"
    command.unlink(missing_ok=True)
    command.symlink_to("../../bin/hermes")
    stamp = top / "install-stamp.json"
    info = json.loads(stamp.read_text(encoding="utf-8-sig"))
    info["runtimeDir"] = ".fork-payload"
    stamp.write_text(json.dumps(info, indent=2) + "\n", encoding="utf-8")
    with tarfile.open(
        output / "hermes-agent.tar.gz", "w:gz", dereference=False, compresslevel=1
    ) as archive:
        archive.add(top, arcname="hermes-agent")
    print("image archive complete", flush=True)
    shutil.rmtree(stage)
    digest = file_sha256(output / "hermes-agent.tar.gz")
    metadata = {
        "version": tag,
        "src_version": version,
        "git_commit": commit[:12],
        "git_commit_full": commit,
        "cos_url": f"https://github.com/hxy91819/hermes-agent/releases/download/{tag}/hermes-agent.tar.gz",
        "sha256": digest,
    }
    (output / "latest.json").write_text(
        json.dumps(metadata, indent=2) + "\n", encoding="utf-8"
    )
    (output / "release.json").write_text(
        json.dumps(
            {
                "tag": tag,
                "commit": commit,
                "platform": "ubuntu-24.04-x86_64",
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    checksums = []
    for path in sorted(output.iterdir()):
        checksums.append(f"{file_sha256(path)}  {path.name}\n")
    (output / "SHA256SUMS").write_text("".join(checksums), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--payload", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--tag", required=True)
    args = parser.parse_args()
    package(args.payload, args.out, args.tag, Path(__file__).resolve().parents[1])


if __name__ == "__main__":
    main()
