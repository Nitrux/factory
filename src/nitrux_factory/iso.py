#!/usr/bin/env python3
# SPDX-License-Identifier: BSD-3-Clause
# Copyright 2026 Nitrux Latinoamericana S.C.

"""Build an ISO from a rootfs archive using externally supplied ISO assets."""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import urlparse

import requests
import yaml
from rich.console import Console
from rich.progress import (
    BarColumn,
    DownloadColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeRemainingColumn,
    TransferSpeedColumn,
)
from rich.text import Text

console = Console()


def _load_config(path: Path) -> tuple[dict, Path]:
    path = path.expanduser().resolve()
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("iso"), dict):
        raise ValueError("ISO YAML must contain an 'iso' mapping")
    return data["iso"], path.parent


def _resolve_path(value: str, config_dir: Path) -> Path:
    path = Path(value).expanduser()
    return path.resolve() if path.is_absolute() else (config_dir / path).resolve()


def _iso_data_dir(value, config_dir: Path) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("iso.data_dir must specify an ISO asset directory")
    data_dir = _resolve_path(value, config_dir)
    required = (
        "bootstrap",
        "configs/scripts/stages",
        "configs/scripts/others",
        "configs/files/grub_files",
        "tooling/runch",
        "tooling/mkiso",
    )
    missing = [relative for relative in required if not (data_dir / relative).exists()]
    if missing:
        raise FileNotFoundError(
            f"ISO asset directory {data_dir} is missing: {', '.join(missing)}"
        )
    return data_dir


def _rootfs_config(value, config_dir: Path) -> tuple[str, Path | None, str | None]:
    if isinstance(value, str):
        value = {"path": value}
    if not isinstance(value, dict):
        raise ValueError("iso.rootfs must be a path or a mapping with path/url")
    path = value.get("path")
    url = value.get("url")
    checksum = value.get("sha256")
    checksum_url = value.get("sha256_url")
    if bool(path) == bool(url):
        raise ValueError("iso.rootfs must specify exactly one of path or url")
    if path is not None:
        if not isinstance(path, str) or not path.strip():
            raise ValueError("iso.rootfs.path must be a file path")
        if checksum is not None and (
            not isinstance(checksum, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", checksum)
        ):
            raise ValueError("iso.rootfs.sha256 must contain 64 hexadecimal characters")
        return "path", _resolve_path(path, config_dir), checksum.lower() if checksum else None
    if not isinstance(url, str) or urlparse(url).scheme not in ("http", "https"):
        raise ValueError("iso.rootfs.url must be an HTTP(S) URL")
    if checksum is None and isinstance(checksum_url, str) and checksum_url.startswith(("http://", "https://")):
        response = requests.get(checksum_url, timeout=30)
        response.raise_for_status()
        fields = response.text.split()
        checksum = fields[0] if fields else None
    if not isinstance(checksum, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", checksum):
        raise ValueError("Remote rootfs archives must specify sha256 or a sha256_url sidecar")
    return "url", None, checksum.lower()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as archive:
        for block in iter(lambda: archive.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _download_rootfs(url: str, destination: Path, expected_sha256: str) -> Path:
    console.print("[cyan]Downloading rootfs archive[/cyan]")
    digest = hashlib.sha256()
    with requests.get(url, stream=True, timeout=(30, 120)) as response:
        response.raise_for_status()
        total = int(response.headers.get("content-length", 0)) or None
        with destination.open("wb") as output, Progress(
            SpinnerColumn(),
            TextColumn("[cyan]{task.description}[/cyan]"),
            BarColumn(),
            DownloadColumn(),
            TransferSpeedColumn(),
            TimeRemainingColumn(),
            console=console,
        ) as progress:
            task = progress.add_task("Rootfs archive", total=total)
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    output.write(chunk)
                    digest.update(chunk)
                    progress.update(task, advance=len(chunk))
    actual = digest.hexdigest()
    if actual != expected_sha256:
        destination.unlink(missing_ok=True)
        raise ValueError(f"Rootfs SHA-256 mismatch: expected {expected_sha256}, got {actual}")
    return destination


def _iso_stages(value, data_dir: Path) -> list[str]:
    if not isinstance(value, list) or not value or not all(isinstance(stage, str) for stage in value):
        raise ValueError("iso.stages must be a non-empty list of stage names")
    stages = value
    if len(stages) != len(set(stages)):
        raise ValueError("iso.stages must not contain duplicates")

    stage_dir = data_dir / "configs/scripts/stages"
    available = {path.name for path in stage_dir.iterdir() if path.is_file()}
    for stage in stages:
        if not re.fullmatch(r"[0-9]{2}-[A-Za-z0-9_.-]+", stage) or stage.startswith("00-"):
            raise ValueError(f"Invalid ISO stage name: {stage}")
        if stage not in available:
            raise ValueError(f"ISO stage does not exist in {stage_dir}: {stage}")
    return stages


def _run_streamed(command: list[str], *, cwd: Path, environment: dict[str, str], log_path: Path) -> None:
    console.print(f"[cyan]Running[/cyan] {command[-1]}")
    with log_path.open("w", encoding="utf-8") as log:
        process = subprocess.Popen(
            command,
            cwd=cwd,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            errors="replace",
        )
        assert process.stdout is not None
        for line in process.stdout:
            log.write(line)
            log.flush()
            console.print(Text.from_ansi(line.rstrip("\n")), end="\n")
        result = process.wait()
    if result:
        raise RuntimeError(f"ISO stage runner failed with exit code {result}; see {log_path}")


def build_iso(config_path: Path) -> dict[str, Path]:
    """Build a bootable ISO from a local or checksum-pinned rootfs archive."""
    if os.geteuid() != 0:
        raise RuntimeError("ISO builds require root privileges for chroot mounts and image tools")
    config, config_dir = _load_config(config_path)
    rootfs_kind, rootfs_path, rootfs_checksum = _rootfs_config(config.get("rootfs"), config_dir)
    data_dir = _iso_data_dir(config.get("data_dir"), config_dir)
    stages = _iso_stages(config.get("stages"), data_dir)
    flavor = config.get("flavor", "mesa")
    if flavor not in ("mesa", "nvopen"):
        raise ValueError("iso.flavor must be 'mesa' or 'nvopen'")
    label = config.get("volume_label", "NITRUX")
    if not isinstance(label, str) or not label or len(label) > 32:
        raise ValueError("iso.volume_label must contain 1 to 32 characters")
    squashfs = config.get("squashfs", {})
    if not isinstance(squashfs, dict):
        raise ValueError("iso.squashfs must be a mapping")
    compression = squashfs.get("compression", "zstd")
    compression_level = squashfs.get("level")
    block_size = squashfs.get("block_size", 262144)
    memory = squashfs.get("memory", "4G")
    compression_levels = {"zstd": (1, 22), "xz": None, "gzip": (1, 9), "lzo": (1, 9), "lz4": (1, 12)}
    if compression not in compression_levels:
        raise ValueError("Unsupported SquashFS compression algorithm")
    level_range = compression_levels[compression]
    if level_range is None:
        if compression_level is not None:
            raise ValueError("iso.squashfs.level is not supported by the xz compressor")
    else:
        minimum_level, maximum_level = level_range
        if compression_level is None:
            if compression != "lz4":
                compression_level = {"zstd": 18, "gzip": 9, "lzo": 8}[compression]
        if compression_level is not None and (
            isinstance(compression_level, bool)
            or not isinstance(compression_level, int)
            or not minimum_level <= compression_level <= maximum_level
        ):
            raise ValueError(f"iso.squashfs.level for {compression} must be from {minimum_level} to {maximum_level}")
    if isinstance(block_size, bool) or not isinstance(block_size, int) or not 4096 <= block_size <= 1048576:
        raise ValueError("iso.squashfs.block_size must be an integer from 4096 to 1048576")
    if not isinstance(memory, str) or not re.fullmatch(r"[1-9][0-9]*[KMG]", memory, re.IGNORECASE):
        raise ValueError("iso.squashfs.memory must be a size such as 4G")

    output_value = config.get("output_dir", "build-artifacts")
    if not isinstance(output_value, str) or not output_value.strip():
        raise ValueError("iso.output_dir must be a directory path")
    output_dir = _resolve_path(output_value, config_dir)
    image_name = config.get("image", f"nitrux-{flavor}-amd64.iso")
    if not isinstance(image_name, str) or Path(image_name).name != image_name or not image_name.endswith(".iso"):
        raise ValueError("iso.image must be a filename ending in .iso")
    output_dir.mkdir(parents=True, exist_ok=True)
    image_path = output_dir / image_name
    if image_path.exists():
        raise FileExistsError(f"Refusing to overwrite existing ISO: {image_path}")

    if rootfs_kind == "path":
        if rootfs_path is None or not rootfs_path.is_file():
            raise FileNotFoundError(f"Rootfs archive does not exist: {rootfs_path}")
        if rootfs_checksum:
            actual = _sha256(rootfs_path)
            if actual != rootfs_checksum:
                raise ValueError(f"Rootfs SHA-256 mismatch: expected {rootfs_checksum}, got {actual}")

    if config.get("prepare_host", True):
        console.rule("[bold green]🧰 Factory · ISO")
        console.print("[cyan]Installing ISO build dependencies[/cyan]")
        host_dependency_stage = data_dir / "configs/scripts/stages/00-install-host-packages"
        if not host_dependency_stage.is_file():
            raise FileNotFoundError(
                f"ISO asset directory is missing host dependency stage: {host_dependency_stage}"
            )
        subprocess.run(["bash", str(host_dependency_stage)], cwd=data_dir, check=True)

    required_tools = (
        "unshare", "chroot", "mount", "umount", "mountpoint", "lsof", "fuser",
        "tar", "gzip", "xz", "bzip2", "zstd", "mksquashfs", "sbverify", "sha512sum", "xorriso", "grub-mkimage",
        "mkfs.vfat", "mmd", "mcopy",
    )
    missing_tools = [name for name in required_tools if shutil.which(name) is None]
    if missing_tools:
        raise RuntimeError(f"Missing ISO build tools: {', '.join(missing_tools)}")
    if compression == "lz4" and compression_level is not None:
        result = subprocess.run(
            ["mksquashfs", "-help-comp", "lz4"], capture_output=True, text=True
        )
        if result.returncode or "-Xcompression-level" not in result.stdout + result.stderr:
            raise RuntimeError(
                "The installed mksquashfs does not support LZ4 compression levels; "
                "omit iso.squashfs.level or use another compressor"
            )

    work_value = config.get("work_dir")
    if work_value is None:
        work_parent = None
    elif isinstance(work_value, str) and work_value.strip():
        work_parent = _resolve_path(work_value, config_dir)
        work_parent.mkdir(parents=True, exist_ok=True)
    else:
        raise ValueError("iso.work_dir must be a directory path")

    temporary = tempfile.TemporaryDirectory(prefix="factory-iso-", dir=work_parent)
    try:
        work_dir = Path(temporary.name)
        root_dir = work_dir / "root"
        iso_dir = work_dir / "iso"
        root_dir.mkdir()
        iso_dir.mkdir()
        if rootfs_kind == "url":
            assert isinstance(config.get("rootfs"), dict)
            rootfs_path = _download_rootfs(config["rootfs"]["url"], work_dir / "rootfs.tar", rootfs_checksum or "")
        assert rootfs_path is not None

        console.print(f"[cyan]Extracting rootfs[/cyan] {rootfs_path}")
        subprocess.run(
            [
                "tar", "--numeric-owner", "--xattrs", "--xattrs-include=*", "--acls",
                "-xf", str(rootfs_path), "-C", str(root_dir),
            ],
            check=True,
        )

        tooling_dir = work_dir / "tooling"
        tooling_dir.mkdir()
        for tool_name in ("runch", "mkiso"):
            supplied_tool = data_dir / "tooling" / tool_name
            installed_tool = tooling_dir / tool_name
            shutil.copy2(supplied_tool, installed_tool)
            installed_tool.chmod(0o755)

        env = os.environ.copy()
        env["PATH"] = f"{tooling_dir}:{env.get('PATH', '')}"
        env["ISO_BRANCH"] = flavor
        iso_flavor = "ISO_BRANCH=nvopen" if flavor == "nvopen" else "ISO_BRANCH=mesa"
        (root_dir / "etc/iso-release").write_text(iso_flavor + "\n", encoding="utf-8")

        bootstrap_command = [
            "unshare", "--mount", "--", "bash", "-c",
            'printf "\\n>--- Starting Runch ---\\n"; exec runch "$@"', "_",
            "-m", f"{data_dir}:/configs",
            "-r", "configs",
            str(root_dir), "bash", "/configs/bootstrap", *stages,
        ]
        log_path = output_dir / (image_name.removesuffix(".iso") + ".bstlog")
        _run_streamed(bootstrap_command, cwd=root_dir, environment=env, log_path=log_path)

        package_lists = []
        for filename in ("installed_pkgs_start.txt", "installed_pkgs_end.txt"):
            source = root_dir / filename
            if not source.is_file():
                raise RuntimeError(f"ISO bootstrap did not produce {filename}")
            suffix = "rootfs.buildlist" if filename == "installed_pkgs_start.txt" else "buildlist"
            destination = output_dir / (image_name.removesuffix(".iso") + "." + suffix)
            shutil.copy2(source, destination)
            package_lists.append(destination)
        for source in root_dir.glob("var-lib-dpkg-*.tar.xz"):
            shutil.copy2(source, output_dir / source.name)

        boot_dir = iso_dir / "boot"
        boot_dir.mkdir()
        kernels = sorted((root_dir / "boot").glob("vmlinuz*"))
        initrds = sorted((root_dir / "boot").glob("initrd*"))
        if not kernels or not initrds:
            raise RuntimeError("ISO rootfs has no kernel or initramfs")
        shutil.copy2(kernels[-1], boot_dir / "kernel")
        shutil.copy2(initrds[-1], boot_dir / "initramfs")
        if subprocess.run(["sbverify", "--list", str(boot_dir / "kernel")], capture_output=True).returncode:
            raise RuntimeError("The kernel copied to the ISO is not signed or has an invalid signature")

        squashfs_dir = iso_dir / "casper"
        squashfs_dir.mkdir()
        console.print("[cyan]Creating SquashFS filesystem[/cyan]")
        squashfs_command = [
            "mksquashfs", str(root_dir), str(squashfs_dir / "filesystem.squashfs"),
            "-comp", compression,
        ]
        if compression_level is not None:
            if compression == "lz4":
                squashfs_command.append("-Xhc")
            squashfs_command.extend(("-Xcompression-level", str(compression_level)))
        squashfs_command.extend((
            "-b", str(block_size), "-noappend", "-quiet", "-mem", memory,
        ))
        subprocess.run(
            squashfs_command,
            check=True,
        )

        grub = data_dir / "configs/files/grub_files"
        console.print("[cyan]Creating bootable ISO[/cyan]")
        subprocess.run(
            [
                "mkiso", "-V", label, "-e",
                "--shim", str(root_dir / "usr/lib/shim/shimx64.efi.signed"),
                "--grub-efi", str(root_dir / "usr/lib/grub/x86_64-efi-signed/gcdx64.efi.signed"),
                "--mok-manager", str(root_dir / "usr/lib/shim/mmx64.efi.signed"),
                "-g", str(grub / "live/grub.cfg"),
                "-g", str(grub / "live/loopback.cfg"),
                "-t", str(grub / "theme/nitrux"),
                str(iso_dir), str(image_path),
            ],
            env=env,
            check=True,
        )
        checksum_path = output_dir / (image_name.removesuffix(".iso") + ".sha512")
        with checksum_path.open("w", encoding="utf-8") as checksum_file:
            subprocess.run(
                ["sha512sum", image_name],
                cwd=output_dir,
                check=True,
                stdout=checksum_file,
                text=True,
            )
        console.print(f"[bold green]✓ ISO created[/bold green] {image_path}")
        return {"image": image_path, "checksum": checksum_path, "package_lists": package_lists}
    finally:
        temporary.cleanup()
