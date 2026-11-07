#!/usr/bin/env python3
# SPDX-License-Identifier: BSD-3-Clause
# Copyright <2025> <Uri Herrera <uri_herrera@nxos.org>>

import shutil
from pathlib import Path

import typer
# <---
# --->
# -- Define base directories.

cache_dir = Path.home() / ".cache/nbs-cli"

def cleanup_cache(package_name: str | None = None, quiet=False):
    """Remove the cache directory for a specific package or skip full cache cleanup."""
    console = typer.get_console()

    if package_name:
        target_dir = cache_dir / package_name

        if target_dir.exists():
            if not quiet:
                console.print(f"\n🧹 [bold]Cleaning up build cache for:[/bold] {package_name}\n")
            shutil.rmtree(target_dir, ignore_errors=True)
        else:
            if not quiet:
                console.print(f"\n🚨 [yellow]Warning:[/yellow] No build cache found for: {package_name}. Skipping cleanup.\n")
    else:
        if not quiet:
            console.print("\nℹ️ [dim]Skipping full cache cleanup. Only removing package-specific cache.[/dim]")
