#!/usr/bin/env python3
# SPDX-License-Identifier: BSD-3-Clause
# Copyright <2025> <Uri Herrera <uri_herrera@nxos.org>>

import shutil
import subprocess
from pathlib import Path
from rich.console import Console
import typer
# <---
# --->
console = Console()

# -- Extract .deb files into the correct package directory.

def extract_deb(deb_path, package_name, quiet=True):
    """Extracts a .deb package into its designated bootstrap."""

    if deb_path is None:
        return

    package_dir = Path.home() / ".cache/nbs-cli" / package_name
    rootfs_dir = package_dir / "bootstrap"
    deb_dir = package_dir / "debs"

    rootfs_dir.mkdir(parents=True, exist_ok=True)
    temp_dir = deb_dir / "temp"
    temp_dir.mkdir(parents=True, exist_ok=True)

    if not quiet:
        console.print(f"[bold cyan]🗄️ Extracting[/bold cyan]: {deb_path}...")

    try:
        subprocess.run(["ar", "x", deb_path], cwd=temp_dir, check=True)

        archive_files = list(temp_dir.glob("data.tar.*"))
        if not archive_files:
            if not quiet:
                console.print(f"[red]⛔ Error:[/red] No valid data archive found in {deb_path}.")
            return

        data_archive = archive_files[0]

        if data_archive.suffix == ".xz":
            subprocess.run(["tar", "xf", str(data_archive), "-C", str(rootfs_dir)], check=True)
        elif data_archive.suffix == ".gz":
            subprocess.run(["tar", "xzf", str(data_archive), "-C", str(rootfs_dir)], check=True)
        elif data_archive.suffix == ".zst":
            decompressed_archive = temp_dir / "data.tar"
            subprocess.run(
                ["unzstd", "-d", str(data_archive), "-o", str(decompressed_archive)],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )
            subprocess.run(["tar", "xf", str(decompressed_archive), "-C", str(rootfs_dir)], check=True)
        else:
            if not quiet:
                console.print(f"[red]⛔ Error:[/red] Unsupported archive format in {deb_path}.")
            return

        if not quiet:
            console.print(f"    ↪︎ 🔸[green]Extracted successfully[/green].")

        lib_dir = rootfs_dir / "usr/lib/"
        lib_dir.mkdir(parents=True, exist_ok=True)

    except subprocess.CalledProcessError as e:
        if not quiet:
            console.print(f"[red]⛔ Error:[/red] Extraction failed for {deb_path}: {e}")
        return

    finally:
        shutil.rmtree(temp_dir)
