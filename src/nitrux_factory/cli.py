#!/usr/bin/env python3
# SPDX-License-Identifier: BSD-3-Clause
# Copyright 2026 Nitrux Latinoamericana S.C.

"""Build root filesystems and ISO images for Nitrux."""

from pathlib import Path
from typing import Optional

import requests
import typer
import yaml
from rich.console import Console
from rich.markup import escape

from nitrux_factory.iso import build_iso
from nitrux_factory.rootfs.orchestrator import create_base_system
from nitrux_factory.snapshot import run_snapshot

app = typer.Typer(add_completion=False, no_args_is_help=True)
console = Console()


def _rootfs_config(path: Path) -> tuple[dict, Path]:
    path = path.expanduser().resolve()
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("Rootfs YAML must contain a mapping")
    buildinfo = data.get("buildinfo")
    if not isinstance(buildinfo, dict):
        raise ValueError("Rootfs YAML must contain a 'buildinfo' mapping")
    repositories = buildinfo.get("distrorepo")
    if repositories is None:
        raise ValueError("Rootfs YAML is missing 'buildinfo.distrorepo'")
    packages = data.get("base", data.get("packages", []))
    if not isinstance(packages, list) or not packages:
        raise ValueError("Rootfs YAML must define a non-empty 'base' or 'packages' list")
    excluded = data.get("exclude", [])
    if not isinstance(excluded, list) or not all(
        isinstance(package, str) and package.strip() for package in excluded
    ):
        raise ValueError("Rootfs YAML 'exclude' must be a list of package names")
    return {
        "repositories": repositories,
        "packages": packages,
        "excluded": excluded,
        "rootfs": buildinfo.get("rootfs", "build/rootfs"),
        "archive": buildinfo.get("archive"),
        "compression": buildinfo.get("compression", "xz"),
        "compression_level": buildinfo.get("compression_level"),
    }, path.parent


@app.command()
def build(
    rfs: Optional[Path] = typer.Option(None, "--rfs", help="Build a rootfs from this YAML file"),
    iso: Optional[Path] = typer.Option(None, "--iso", help="Build an ISO from this YAML file"),
):
    """Build either a root filesystem archive or an ISO image."""
    if (rfs is None) == (iso is None):
        Console(stderr=True).print("[bold red]⛔ Choose exactly one of --rfs or --iso[/bold red]")
        raise typer.Exit(2)

    try:
        if rfs is not None:
            config, config_dir = _rootfs_config(rfs)
            console.rule("[bold green]🧰 Factory · rootfs")
            summary = create_base_system(
                config["packages"],
                config["repositories"],
                rootfs_path=config["rootfs"],
                archive_path=config["archive"],
                config_dir=config_dir,
                excluded_packages=config["excluded"],
                compression=config["compression"],
                compression_level=config["compression_level"],
            )
            console.rule("[bold blue]📊 Build Summary")
            console.print(f"[cyan]Rootfs archive:[/cyan] {summary['archive']}")
        else:
            assert iso is not None
            console.rule("[bold green]🧰 Factory · ISO")
            summary = build_iso(iso)
            console.rule("[bold blue]📊 Build Summary")
            console.print(f"[cyan]ISO image:[/cyan] {summary['image']}")
            console.print(f"[cyan]SHA-512:[/cyan] {summary['checksum']}")
    except Exception as error:
        Console(stderr=True).print(f"[bold red]⛔ Build failed:[/bold red] {escape(str(error))}")
        raise typer.Exit(1) from error


@app.command()
def snapshot(config: Path = typer.Argument(..., exists=True, help="YAML package snapshot configuration")):
    """Download the configured package set from a Debian repository."""
    try:
        run_snapshot(config)
    except (OSError, ValueError, yaml.YAMLError, requests.exceptions.RequestException) as error:
        Console(stderr=True).print(f"[bold red]⛔ Snapshot failed:[/bold red] {escape(str(error))}")
        raise typer.Exit(1) from error


def main() -> None:
    app()


if __name__ == "__main__":
    main()
