#!/usr/bin/env python3

#############################################################################################################################################################################
#   The license used for this file and its contents is: BSD-3-Clause                                                                                                        #
#                                                                                                                                                                           #
#   Copyright <2025> <Uri Herrera <uri_herrera@nxos.org>>                                                                                                                   #
#                                                                                                                                                                           #
#   Redistribution and use in source and binary forms, with or without modification, are permitted provided that the following conditions are met:                          #
#                                                                                                                                                                           #
#    1. Redistributions of source code must retain the above copyright notice, this list of conditions and the following disclaimer.                                        #
#                                                                                                                                                                           #
#    2. Redistributions in binary form must reproduce the above copyright notice, this list of conditions and the following disclaimer                                      #
#       in the documentation and/or other materials provided with the distribution.                                                                                         #
#                                                                                                                                                                           #
#    3. Neither the name of the copyright holder nor the names of its contributors may be used to endorse or promote products derived from this software                    #
#    without specific prior written permission.                                                                                                                              #
#                                                                                                                                                                           #
#   THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO,                      #
#   THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS                  #
#   BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE                 #
#   GOODS OR SERVICES; LOSS OF USE, DATA,   OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT,                      #
#   STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.   #
#############################################################################################################################################################################

import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

from rich.console import Console

console = Console()


DEFAULT_ROOTFS = Path("build/rootfs")
ARCHIVE_COMPRESSORS = {
    "gzip": ("gzip", ".tar.gz", 1, 9, 6),
    "xz": ("xz", ".tar.xz", 0, 9, 6),
    "bzip2": ("bzip2", ".tar.bz2", 1, 9, 9),
    "zstd": ("zstd", ".tar.zst", 1, 19, 3),
}


def _archive_compression(compression, level):
    if not isinstance(compression, str) or compression not in ARCHIVE_COMPRESSORS:
        supported = ", ".join(ARCHIVE_COMPRESSORS)
        raise ValueError(f"Unsupported archive compression '{compression}'; choose from {supported}")

    program, suffix, minimum, maximum, default = ARCHIVE_COMPRESSORS[compression]
    if level is None:
        level = default
    if isinstance(level, bool) or not isinstance(level, int) or not minimum <= level <= maximum:
        raise ValueError(
            f"Compression level for {compression} must be an integer from {minimum} to {maximum}"
        )
    return program, suffix, level


def _repositories(value, config_dir):
    if isinstance(value, dict):
        value = value.get("base", [])
    if not isinstance(value, list) or not value:
        raise ValueError("buildinfo.distrorepo must be a non-empty list of repositories")

    repositories = []
    architectures = set()
    for index, entry in enumerate(value, start=1):
        if not isinstance(entry, dict):
            raise ValueError(f"Repository {index} must be a YAML mapping")

        distro = str(entry.get("distro", "")).lower()
        uri = entry.get("url") or entry.get("uri")

        suite = entry.get("suite") or entry.get("release")
        architecture = entry.get("arch") or entry.get("architecture")
        components = entry.get("components", ["main"])
        key_url = entry.get("key_url")
        keyring = entry.get("keyring")

        if not isinstance(uri, str) or not uri.startswith(("http://", "https://", "file:")) or any(char.isspace() for char in uri):
            raise ValueError(f"Repository {index} needs a valid url/uri")
        if not isinstance(suite, str) or not suite or any(char.isspace() for char in suite):
            raise ValueError(f"Repository {index} needs a release or suite")
        if not isinstance(architecture, str) or not architecture or any(char.isspace() for char in architecture):
            raise ValueError(f"Repository {index} needs an arch")
        if isinstance(components, str):
            components = components.split()
        if not isinstance(components, list) or not components or not all(
            isinstance(item, str) and item and not any(char.isspace() for char in item)
            for item in components
        ):
            raise ValueError(f"Repository {index} components must be a non-empty list of names")
        if bool(key_url) == bool(keyring):
            raise ValueError(f"Repository {index} must specify exactly one of key_url or keyring")
        if key_url and (not isinstance(key_url, str) or not key_url.startswith(("http://", "https://"))):
            raise ValueError(f"Repository {index} key_url must be an HTTP(S) URL")

        key_path = None
        if keyring:
            if not isinstance(keyring, str):
                raise ValueError(f"Repository {index} keyring must be a file path")
            key_path = Path(keyring).expanduser()
            if not key_path.is_absolute():
                key_path = config_dir / key_path
            if not key_path.is_file():
                raise ValueError(f"Repository {index} keyring file does not exist: {key_path}")

        architectures.add(architecture)
        repositories.append({
            "name": str(entry.get("name") or urlparse(uri).hostname or f"repo-{index}"),
            "uri": uri.rstrip("/"),
            "suite": suite,
            "architecture": architecture,
            "components": components,
            "check_valid_until": entry.get("check_valid_until", distro != "debian-snapshot"),
            "key_url": key_url,
            "keyring": key_path,
        })

    if len(architectures) != 1:
        raise ValueError("All repositories must use the same architecture")
    return repositories, architectures.pop()


def _prepare_keyring(repository, index, key_dir):
    label = re.sub(r"[^A-Za-z0-9_.-]+", "-", repository["name"]).strip(".-") or f"repo-{index}"
    key_url = repository["key_url"]
    local_keyring = repository["keyring"]

    if local_keyring:
        source = local_keyring
        contents = source.read_bytes()
    else:
        with urllib.request.urlopen(key_url, timeout=30) as response:
            contents = response.read()

    if not contents:
        raise ValueError(f"Repository key is empty: {key_url or local_keyring}")

    if b"-----BEGIN PGP PUBLIC KEY BLOCK-----" in contents:
        suffix = ".asc"
    else:
        suffix = ".gpg"

    filename = f"{index}-{label}{suffix}"
    destination = key_dir / filename
    destination.write_bytes(contents)
    destination.chmod(0o644)
    repository["signed_by"] = f"/etc/apt/keyrings/{filename}"
    repository["host_key"] = str(destination)


def _unmount_rootfs_mounts(rootfs_path):
    mountinfo = Path("/proc/self/mountinfo")
    try:
        entries = mountinfo.read_text().splitlines()
    except OSError as error:
        raise RuntimeError(f"Unable to inspect mount points before replacing {rootfs_path}: {error}") from error

    mount_roots = [rootfs_path / name for name in ("dev", "proc", "sys")]
    stale_mounts = []
    for entry in entries:
        fields = entry.split()
        if len(fields) < 5:
            continue
        mountpoint = re.sub(r"\\([0-7]{3})", lambda match: chr(int(match.group(1), 8)), fields[4])
        for mount_root in mount_roots:
            root = str(mount_root)
            if mountpoint == root or mountpoint.startswith(root + "/"):
                stale_mounts.append(mountpoint)
                break

    for mountpoint in sorted(stale_mounts, key=len, reverse=True):
        result = subprocess.run(["umount", mountpoint], capture_output=True, text=True)
        if result.returncode:
            message = (result.stderr or result.stdout).strip()
            raise RuntimeError(
                f"Unable to unmount stale chroot mount {mountpoint}: {message or 'umount failed'}"
            )


def _prepare_dev(rootfs_path, mounts):
    dev_path = rootfs_path / "dev"
    for name in ("pts", "shm", "mqueue"):
        (dev_path / name).mkdir(parents=True, exist_ok=True)

    device_specs = (
        ("null", 1, 3),
        ("zero", 1, 5),
        ("full", 1, 7),
        ("random", 1, 8),
        ("urandom", 1, 9),
        ("tty", 5, 0),
        ("console", 5, 1),
        ("ptmx", 5, 2),
    )
    created_nodes = []
    try:
        for name, major, minor in device_specs:
            device_path = dev_path / name
            if device_path.exists():
                continue
            subprocess.run(
                ["mknod", "-m", "666", str(device_path), "c", str(major), str(minor)],
                check=True,
                capture_output=True,
                text=True,
            )
            created_nodes.append(device_path)
        null_fd = os.open(dev_path / "null", os.O_WRONLY)
        try:
            os.write(null_fd, b"nbs")
        finally:
            os.close(null_fd)
    except (OSError, subprocess.CalledProcessError):
        for device_path in created_nodes:
            if device_path.exists():
                device_path.unlink()
        console.print("[yellow]Static device nodes are unavailable; trying individual device binds[/yellow]")
    else:
        for link, target in (
            ("fd", "/proc/self/fd"),
            ("stdin", "/proc/self/fd/0"),
            ("stdout", "/proc/self/fd/1"),
            ("stderr", "/proc/self/fd/2"),
        ):
            link_path = dev_path / link
            if not link_path.exists() and not link_path.is_symlink():
                link_path.symlink_to(target)
        return []

    cleanup_paths = []
    bound_devices = []
    for name, _, _ in device_specs:
        source = Path("/dev") / name
        try:
            source_mode = source.stat().st_mode
        except OSError:
            continue
        if not stat.S_ISCHR(source_mode):
            continue

        destination = dev_path / name
        if destination.is_symlink():
            destination.unlink()
        if not destination.exists():
            destination.touch()
        cleanup_paths.append(destination)
        result = subprocess.run(
            ["mount", "--bind", str(source), str(destination)],
            capture_output=True,
            text=True,
        )
        if result.returncode:
            message = (result.stderr or result.stdout).strip()
            console.print(f"[yellow]Cannot bind /dev/{name}: {message or 'mount failed'}[/yellow]")
            for mounted_path in reversed(bound_devices):
                unmount = subprocess.run(["umount", str(mounted_path)], capture_output=True, text=True)
                if unmount.returncode:
                    unmount_message = (unmount.stderr or unmount.stdout).strip()
                    raise RuntimeError(
                        f"Unable to unmount temporary device {mounted_path}: "
                        f"{unmount_message or 'umount failed'}"
                    )
            bound_devices.clear()
            break
        bound_devices.append(destination)

    mounts.extend(bound_devices)
    for link, target in (
        ("fd", "/proc/self/fd"),
        ("stdin", "/proc/self/fd/0"),
        ("stdout", "/proc/self/fd/1"),
        ("stderr", "/proc/self/fd/2"),
        ("ptmx", "pts/ptmx"),
    ):
        link_path = dev_path / link
        if not link_path.exists() and not link_path.is_symlink():
            link_path.symlink_to(target)

    for name, _, _ in device_specs:
        device_path = dev_path / name
        if not device_path.exists():
            device_path.touch()
            device_path.chmod(0o666)
            cleanup_paths.append(device_path)
    return cleanup_paths

def _write_sources(repositories, rootfs_path):
    runtime_lines = []
    image_lines = []
    for repository in repositories:
        arch = repository["architecture"]
        components = " ".join(repository["components"])
        source_options = [f"arch={arch}"]
        if not repository["check_valid_until"]:
            source_options.append("check-valid-until=no")
        runtime_options = " ".join([*source_options, f"signed-by={repository['host_key']}"])
        image_options = " ".join([*source_options, f"signed-by={repository['signed_by']}"])
        runtime_lines.append(
            f"deb [{runtime_options}] {repository['uri']} {repository['suite']} {components}"
        )
        image_lines.append(
            f"deb [{image_options}] {repository['uri']} {repository['suite']} {components}"
        )

    runtime_sources = rootfs_path / "etc/apt/sources.list"
    runtime_sources.write_text("\n".join(runtime_lines) + "\n")
    return runtime_sources, "\n".join(image_lines) + "\n"


def _install_packages(rootfs_path, architecture, packages, excluded_packages):
    interpreter = sys.executable
    try:
        import apt
        import apt_pkg
    except ImportError as error:
        interpreter = "/usr/bin/python3"
        if not Path(interpreter).is_file():
            raise RuntimeError(
                f"python3-apt is unavailable to NBS Python {sys.executable}: {error}"
            ) from error
        console.print(
            f"[yellow]NBS Python {sys.executable} cannot import python3-apt; using {interpreter}[/yellow]"
        )

    worker = Path(__file__).with_name("apt_worker.py")
    environment = os.environ.copy()
    environment.pop("APT_CONFIG", None)
    payload = json.dumps({
        "rootfs": str(rootfs_path),
        "architecture": architecture,
        "packages": packages,
        "excluded_packages": excluded_packages,
    })
    try:
        subprocess.run(
            [interpreter, str(worker)], input=payload, text=True, env=environment, check=True,
        )
    except subprocess.CalledProcessError as error:
        raise RuntimeError(
            f"APT installation failed under {interpreter}: exit code {error.returncode}"
        ) from error


def create_base_system(
    package_list,
    repos,
    rootfs_path=DEFAULT_ROOTFS,
    archive_path=None,
    config_dir=Path.cwd(),
    excluded_packages=None,
    compression="xz",
    compression_level=None,
):
    """Create a root filesystem with APT and archive it for iso-tool."""
    if os.geteuid() != 0:
        raise RuntimeError("NBS must run as root to install packages into a chroot")

    if not isinstance(package_list, list) or not package_list or not all(
        isinstance(package, str) and package.strip() for package in package_list
    ):
        raise ValueError("The YAML base package list must contain package names")
    packages = list(dict.fromkeys(package.strip() for package in package_list))
    if excluded_packages is None:
        excluded_packages = []
    if not isinstance(excluded_packages, list) or not all(
        isinstance(package, str)
        and package.strip()
        and not any(char.isspace() for char in package.strip())
        for package in excluded_packages
    ):
        raise ValueError("The YAML exclude list must contain package names")
    excluded_packages = list(dict.fromkeys(package.strip() for package in excluded_packages))
    excluded_set = set(excluded_packages)
    packages = [package for package in packages if package not in excluded_set]

    compression_program, compression_suffix, compression_level = _archive_compression(
        compression, compression_level
    )
    repositories, architecture = _repositories(repos, Path(config_dir).resolve())
    if not isinstance(rootfs_path, (str, Path)):
        raise ValueError("buildinfo.rootfs must be a filesystem path")
    if archive_path is not None and not isinstance(archive_path, (str, Path)):
        raise ValueError("buildinfo.archive must be a filesystem path")
    rootfs_path = Path(rootfs_path).expanduser()
    if rootfs_path.is_symlink():
        raise ValueError(f"Refusing to replace a symlink as the chroot: {rootfs_path}")
    rootfs_path = rootfs_path.resolve()
    archive_path = Path(archive_path or rootfs_path.with_suffix(compression_suffix)).expanduser().resolve()
    if rootfs_path == Path("/") or archive_path == rootfs_path or rootfs_path in archive_path.parents:
        raise ValueError("The rootfs archive must be outside the chroot directory")

    marker = rootfs_path / ".nbs-rootfs"
    console.print(f"[bold]Creating chroot:[/bold] {rootfs_path}")
    if rootfs_path.is_symlink():
        raise ValueError(f"Refusing to replace a symlink as the chroot: {rootfs_path}")
    if rootfs_path.exists():
        if not rootfs_path.is_dir() or not marker.is_file():
            raise ValueError(f"Refusing to replace a directory not created by NBS: {rootfs_path}")
        _unmount_rootfs_mounts(rootfs_path)
        shutil.rmtree(rootfs_path)
    rootfs_path.mkdir(parents=True, exist_ok=True)
    marker.touch()

    for relative in (
        "etc/apt/keyrings",
        "etc/apt/sources.list.d",
        "var/lib/dpkg",
        "var/lib/apt/lists/partial",
        "var/cache/apt/archives/partial",
        "var/log/apt",
        "tmp",
        "dev",
        "proc",
        "sys",
        "run",
    ):
        (rootfs_path / relative).mkdir(parents=True, exist_ok=True)
    status_file = rootfs_path / "var/lib/dpkg/status"
    status_file.touch()

    key_dir = rootfs_path / "etc/apt/keyrings"
    for index, repository in enumerate(repositories, start=1):
        _prepare_keyring(repository, index, key_dir)

    mounts = []
    temporary_devices = []
    policy_rc = rootfs_path / "usr/sbin/policy-rc.d"
    policy_rc.parent.mkdir(parents=True, exist_ok=True)
    policy_rc.write_text("#!/bin/sh\nexit 101\n")
    policy_rc.chmod(0o755)
    build_error = None
    try:
        temporary_devices = _prepare_dev(rootfs_path, mounts)
        runtime_sources, image_sources = _write_sources(repositories, rootfs_path)
        _install_packages(rootfs_path, architecture, packages, excluded_packages)
        runtime_sources.unlink()
        (rootfs_path / "etc/apt/sources.list.d/nbs.list").write_text(image_sources)
        for deb_file in (rootfs_path / "var/cache/apt/archives").glob("*.deb"):
            deb_file.unlink()
    except BaseException as error:
        build_error = error
        raise
    finally:
        cleanup_errors = []
        for destination in reversed(mounts):
            result = subprocess.run(["umount", str(destination)], capture_output=True, text=True)
            if result.returncode:
                message = (result.stderr or result.stdout).strip()
                cleanup_errors.append(f"{destination}: {message or 'umount failed'}")
        if policy_rc.exists():
            policy_rc.unlink()
        for device_path in temporary_devices:
            if device_path.exists():
                device_path.unlink()
        if cleanup_errors:
            message = "; ".join(cleanup_errors)
            if build_error is None:
                raise RuntimeError(f"Failed to unmount temporary device binds: {message}")
            console.print(f"[yellow]Temporary device cleanup issue: {message}[/yellow]")

    archive_path.parent.mkdir(parents=True, exist_ok=True)
    console.print(f"[bold]Creating rootfs archive:[/bold] {archive_path}")
    with tempfile.NamedTemporaryFile(
        prefix=f".{archive_path.name}.",
        suffix=".tmp",
        dir=archive_path.parent,
        delete=False,
    ) as temporary_archive:
        temporary_archive_path = Path(temporary_archive.name)

    marker.unlink()
    try:
        subprocess.run(
            [
                "tar", "--numeric-owner", "--xattrs", "--acls",
                "--use-compress-program", f"{compression_program} -{compression_level}",
                "-cf", str(temporary_archive_path), "-C", str(rootfs_path), ".",
            ],
            check=True,
        )
        os.replace(temporary_archive_path, archive_path)
    except Exception:
        if temporary_archive_path.exists():
            temporary_archive_path.unlink()
        marker.touch()
        raise

    return {
        "success": packages,
        "failed": [],
        "skipped": [],
        "rootfs": rootfs_path,
        "archive": archive_path,
    }
