#!/usr/bin/env python3
# SPDX-License-Identifier: BSD-3-Clause
# Copyright <2025> <Uri Herrera <uri_herrera@nxos.org>>

"""Download configured Debian packages from a repository."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import lzma
import os
import re
from pathlib import Path, PurePosixPath
import tempfile
from urllib.parse import urljoin

import requests
import yaml
from rich.console import Console
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TaskProgressColumn,
    TextColumn,
)
from rich.table import Column


def read_config(path: Path) -> dict:
    """Load and validate package snapshot settings from a YAML file."""
    config = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(config, dict):
        raise ValueError(f"{path}: expected a YAML mapping")
    required = {
        "repo_url",
        "suite",
        "component",
        "architecture",
        "output_dir",
        "packages",
    }
    missing = required - config.keys()
    if missing:
        raise ValueError(f"{path}: missing {', '.join(sorted(missing))}")
    return config


def package_names(value: str | list[str]) -> list[str]:
    """Parse unique package names from a string or YAML list."""
    values = value if isinstance(value, list) else [value]
    names = set()
    for item in values:
        if not isinstance(item, str):
            raise ValueError("packages must contain strings")
        for name in re.split(r"[\s,]+", item.strip()):
            if name:
                names.add(re.sub(r":([A-Za-z0-9][A-Za-z0-9-]*)$", "", name))
    return sorted(names)


def package_filenames(
    repository: str,
    suite: str,
    component: str,
    architecture: str,
    requested: set[str],
) -> dict[str, tuple[str, str]]:
    """Find requested package filenames in the repository's compressed index."""
    index_url = (
        f"{repository.rstrip('/')}/dists/{suite}/{component}/"
        f"binary-{architecture}/Packages"
    )
    content = None
    for extension, decompress in ((".gz", gzip.decompress), (".xz", lzma.decompress)):
        response = requests.get(f"{index_url}{extension}", timeout=30)
        if response.status_code == 404:
            response.close()
            continue
        response.raise_for_status()
        try:
            content = decompress(response.content).decode("utf-8", errors="ignore")
            break
        except (OSError, lzma.LZMAError):
            continue

    if content is None:
        raise ValueError(f"Could not read package index from {index_url}")

    filenames: dict[str, tuple[str, str]] = {}
    for paragraph in re.split(r"\n\s*\n", content.strip()):
        fields = {}
        for line in paragraph.splitlines():
            if line[:1].isspace():
                continue
            name, separator, value = line.partition(": ")
            if separator:
                fields[name] = value

        package = fields.get("Package")
        filename = fields.get("Filename")
        checksum = fields.get("SHA256")
        if package not in requested or not filename:
            continue
        relative_path = PurePosixPath(filename)
        if (
            relative_path.is_absolute()
            or ".." in relative_path.parts
            or "\\" in filename
            or re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", filename)
        ):
            raise ValueError(f"Unsafe package filename in repository index: {filename}")
        if not isinstance(checksum, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", checksum):
            raise ValueError(f"Package index has no valid SHA256 for {package}")
        filenames.setdefault(package, (filename, checksum.lower()))
        if filenames.keys() >= requested:
            break
    return filenames


def download_file(url: str, destination: Path, expected_sha256: str) -> None:
    """Download a package to a temporary file and verify the index SHA256."""
    if not re.fullmatch(r"[0-9a-fA-F]{64}", expected_sha256):
        raise ValueError("Expected package SHA256 must contain 64 hexadecimal characters")

    digest = hashlib.sha256()
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(
            prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent, delete=False
        ) as output:
            temporary_path = Path(output.name)
            with requests.get(url, stream=True, timeout=30) as response:
                response.raise_for_status()
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    if chunk:
                        output.write(chunk)
                        digest.update(chunk)

        os.chmod(temporary_path, 0o644)
        actual_sha256 = digest.hexdigest()
        if actual_sha256 != expected_sha256.lower():
            raise ValueError(
                f"SHA256 mismatch for {destination.name}: "
                f"expected {expected_sha256.lower()}, got {actual_sha256}"
            )
        os.replace(temporary_path, destination)
    except Exception:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        raise


def run_snapshot(config_path: Path) -> int:
    """Download configured packages into the output directory."""
    config = read_config(config_path)
    packages = package_names(config["packages"])
    if not packages:
        raise ValueError("No package names were provided")

    repository = str(config["repo_url"]).rstrip("/")
    filenames = package_filenames(
        repository,
        str(config["suite"]),
        str(config["component"]),
        str(config["architecture"]),
        set(packages),
    )
    available = [package for package in packages if package in filenames]
    missing = sorted(set(packages) - filenames.keys())
    if not available:
        raise ValueError("No requested packages were found in the repository index")

    output_dir = Path(config["output_dir"]).expanduser()
    output_dir.mkdir(parents=True, exist_ok=True)

    console = Console()
    console.rule("[bold green]📦 Factory · package snapshot")
    console.print(
        f"[cyan]Downloading packages from[/cyan] "
        f"{config['suite']} ([bold]{config['architecture']}[/bold])"
    )
    console.print()
    with Progress(
        SpinnerColumn(),
        TextColumn(
            "[cyan]{task.fields[package]}",
            table_column=Column(width=28, overflow="ellipsis", no_wrap=True),
        ),
        BarColumn(bar_width=28),
        TaskProgressColumn(),
        MofNCompleteColumn(
            table_column=Column(width=7, justify="right", no_wrap=True)
        ),
        console=console,
        transient=False,
    ) as progress:
        task = progress.add_task("download", total=len(available), package="")
        for package in available:
            filename, checksum = filenames[package]
            url = urljoin(repository + "/", filename)
            destination = output_dir / Path(filename).name
            progress.update(task, package=package)
            download_file(url, destination, checksum)
            progress.advance(task)

    console.print()
    console.print(
        f"[bold green]✓ Downloaded {len(available)} packages[/bold green] "
        f"to {output_dir}"
    )
    if missing:
        console.print(
            f"[yellow]Skipped {len(missing)} unavailable packages:[/yellow] "
            f"{', '.join(missing)}"
        )
    return 0


def main() -> int:
    """Run the standalone package snapshot command."""
    parser = argparse.ArgumentParser(prog="factory snapshot", description=__doc__)
    parser.add_argument("config", type=Path, help="YAML repository configuration")
    args = parser.parse_args()
    return run_snapshot(args.config)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (
        OSError,
        ValueError,
        requests.exceptions.RequestException,
        yaml.YAMLError,
    ) as error:
        Console(stderr=True).print(f"[bold red]⛔ Error:[/bold red] {error}")
        raise SystemExit(1) from error
