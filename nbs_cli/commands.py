#!/usr/bin/env python3
# SPDX-License-Identifier: BSD-3-Clause
# Copyright <2025> <Uri Herrera <uri_herrera@nxos.org>>

import subprocess
import typer
import yaml
from pathlib import Path
from rich.console import Console
from rich.markup import escape

from nbs_cli.orchestrator import create_base_system

console = Console()


def build(config: Path = typer.Argument(..., exists=True, help="Path to the YAML config file")):
    """Build and archive a package-installed root filesystem from YAML."""
    console.rule("[bold green]🧰 Nitrux Bootstrap System")

    try:
        with config.open(encoding="utf-8") as config_file:
            data = yaml.safe_load(config_file)
        if not isinstance(data, dict):
            raise ValueError("The YAML document must contain a mapping")

        buildinfo = data.get("buildinfo")
        if not isinstance(buildinfo, dict):
            raise ValueError("Missing or invalid 'buildinfo' mapping")
        repos = buildinfo.get("distrorepo")
        if repos is None:
            raise ValueError("Missing 'buildinfo.distrorepo'")

        packages = data.get("base", data.get("packages", []))
        if not isinstance(packages, list) or not packages:
            raise ValueError("The YAML must define a non-empty 'base' or 'packages' list")
        excluded_packages = data.get("exclude", [])
        if not isinstance(excluded_packages, list) or not all(
            isinstance(package, str) and package.strip() for package in excluded_packages
        ):
            raise ValueError("The YAML 'exclude' field must be a list of package names")

        summary = create_base_system(
            packages,
            repos,
            rootfs_path=buildinfo.get("rootfs", "build/rootfs"),
            archive_path=buildinfo.get("archive"),
            config_dir=config.resolve().parent,
            excluded_packages=excluded_packages,
            compression=buildinfo.get("compression", "xz"),
            compression_level=buildinfo.get("compression_level"),
        )
    except (OSError, yaml.YAMLError, ValueError, RuntimeError) as error:
        typer.secho(f"⛔ Build failed: {escape(str(error))}", fg=typer.colors.RED)
        raise typer.Exit(1) from error
    except Exception as error:
        if isinstance(error, subprocess.CalledProcessError):
            message = f"Command failed with exit code {error.returncode}: {error.cmd}"
        else:
            message = str(error)
        typer.secho(f"⛔ Build failed: {escape(message)}", fg=typer.colors.RED)
        raise typer.Exit(1) from error

    console.rule("[bold blue]📊 Build Summary")
    typer.secho(f"📁 Chroot: {escape(str(summary['rootfs']))}", fg=typer.colors.CYAN)
    typer.secho(f"📦 Rootfs archive: {escape(str(summary['archive']))}", fg=typer.colors.CYAN)


def hello():
    """Simple hello command to demonstrate multi-command interface."""
    typer.echo("Hello, world!")
