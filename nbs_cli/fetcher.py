#!/usr/bin/env python3
# SPDX-License-Identifier: BSD-3-Clause
# Copyright <2025> <Uri Herrera <uri_herrera@nxos.org>>

import gzip
import random
import re
import sys
import time
from urllib.parse import urljoin, urlparse
from collections import defaultdict
from pathlib import Path
from threading import Lock

import requests
from debian import debian_support
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from concurrent.futures import ThreadPoolExecutor, as_completed
from rich.console import Console
import typer

from .utensils import cleanup_cache
# <---
# --->
# -- Base cache directory for downloads.

cache_dir = Path.home() / ".cache/nbs-cli"


# -- Debounce per host.

last_access_time = {}
access_lock = Lock()
MIN_DELAY_PER_HOST = 0.3


# -- Use retry strategy and session reuse for connection pooling.

retry_strategy = Retry(
    total=3,
    status_forcelist=[429, 500, 502, 503, 504],
    backoff_factor=0.3,
)
adapter = HTTPAdapter(max_retries=retry_strategy)

session = requests.Session()

metadata_cache = {}
session.mount("http://", adapter)
session.mount("https://", adapter)


console = Console()

def build_probe_tasks(repos, pkg_name, quiet):
    """
    Build probe tasks using the repository URLs provided in the YAML.
    """
    tasks = []
    for repo in repos:
        if "ppa" in repo:
            continue

        uri = repo.get("url") or repo.get("uri")
        release = repo.get("release") or repo.get("suite")
        arch = repo.get("arch") or repo.get("architecture")
        components = repo.get("components", ["main"])

        if not (uri and release and arch):
            if not quiet:
                console.print(
                    f"[red]⛔ Error:[/red] Missing required repo keys for [bold]{pkg_name} → {repo}[/bold]"
                )
            continue

        for component in components:
            tasks.append((uri.rstrip("/"), release, arch, pkg_name, component))

    return tasks


def get_latest_deb(pkg_name, repos, package_name, log_lock, quiet=True):
    """Download the latest .deb package for the given pkg_name by probing all mirrors concurrently using threads."""

    package_dir = cache_dir / package_name
    deb_dir = package_dir / "debs"
    deb_dir.mkdir(parents=True, exist_ok=True)

    if not repos:
        raise RuntimeError(f"[red]⛔ No valid repositories provided for {pkg_name}.")

    probe_tasks = build_probe_tasks(repos, pkg_name, quiet)

    fetch_failures = []
    no_metadata = []
    candidates = []
    mirror_logs = []

    for repo in repos:
        if "ppa" in repo:
            candidate = fetch_from_ppa(pkg_name, repo, package_name, deb_dir, quiet)
            if candidate:
                candidates.append(candidate)

    def probe_mirror(task):
        mirror, release, arch, pkg_name, component = task
        try:
            result, status_msg = fetch_package_metadata(mirror, release, arch, pkg_name, component)
            return (task, result, status_msg, None)
        except Exception as e:
            return (task, None, None, e)

    with ThreadPoolExecutor(max_workers=5) as executor:
        future_to_task = {executor.submit(probe_mirror, task): task for task in probe_tasks}
        for future in as_completed(future_to_task):
            mirror, release, arch, pkg_name, component = future_to_task[future]
            try:
                _, result, status_msg, exception = future.result()
                if result:
                    filename, version_str = result
                    version = debian_support.Version(version_str)
                    deb_url = f"{mirror}/{filename}"
                    candidates.append({
                        "version": version,
                        "version_str": version_str,
                        "url": deb_url,
                        "path": deb_dir / f"{pkg_name}.deb",
                        "source": f"{mirror} [{component}]"
                    })
                elif status_msg and not quiet:
                    if "Unable to fetch metadata" in status_msg:
                        fetch_failures.append(status_msg)
                    elif "No metadata" in status_msg:
                        no_metadata.append(status_msg)
                    else:
                        mirror_logs.append(status_msg)
                elif exception and not quiet:
                    mirror_logs.append(f"⛔ Unhandled error for {pkg_name} from {mirror} [{component}]: {exception}")
            except Exception as e:
                if not quiet:
                    mirror_logs.append(f"⛔ Unexpected error for {pkg_name}: {e}")

    if not quiet:
        for group, label in [(fetch_failures, "[red]⛔ Fetch failed"),
                             (no_metadata, "[yellow]🚧 No metadata"),
                             (mirror_logs, "[dim]ℹ️ Mirror log")]:
            if group:
                console.print("\n".join(f"{label}: {line}" for line in group))

    if not candidates:
        raise RuntimeError(f"[red]⛔ Unable to find '{pkg_name}' in any repository after probing {len(probe_tasks)} mirrors/components.")

    version_groups = defaultdict(list)
    for c in candidates:
        version_groups[c["version"]].append(c)

    sorted_versions = sorted(version_groups.keys(), reverse=True)

    shuffled_candidates = []
    for version in sorted_versions:
        mirrors = version_groups[version]
        random.shuffle(mirrors)
        shuffled_candidates.extend(mirrors)

    best = shuffled_candidates[0]

    if not quiet and log_lock:
        with log_lock:
            console.print(f"[cyan]📦 Package:[/cyan] {pkg_name}")
            console.print(f"[cyan]🔹 Version:[/cyan] {best['version_str']}")
            console.print(f"[cyan]🔹 Source:[/cyan]  {best['source']}")
            console.print("")
            console.print(f"[green]📥 Downloading:[/green] [bold]{pkg_name}[/bold] from: {best['url']}")

    download_errors = []

    for candidate in shuffled_candidates:
        url = candidate["url"]
        path = candidate["path"]
        try:
            return download_file(url, path, quiet=quiet)
        except RuntimeError as e:
            download_errors.append(f"{pkg_name}: {e} ← {url}")

    for candidate in shuffled_candidates:
        url = candidate["url"]
        path = candidate["path"]
        try:
            if not quiet:
                console.print(f"[yellow]🔁 Retrying download for:[/yellow] {pkg_name} from: {url}")
            return download_file(url, path, quiet=quiet)
        except RuntimeError as e:
            download_errors.append(f"{pkg_name} (retry): {e} ← {url}")

    if not quiet and download_errors and log_lock:
        with log_lock:
            console.print("\n".join(f"[red]🚧 {msg}" for msg in download_errors))

    raise RuntimeError(f"[red]⛔ All mirrors failed to download: {pkg_name}.")


def fetch_package_metadata(mirror, release, arch, pkg_name, component="main", retries=3):
    """Fetch the package filename and version from Packages.gz metadata, with retry on failure."""
    packages_url = f"{mirror}/dists/{release}/{component}/binary-{arch}/Packages.gz"
    delay_range = (0.2, 0.6)
    cache_key = (mirror, release, arch, component)

    for attempt in range(1, retries + 1):
        try:
            if cache_key in metadata_cache:
                lines = metadata_cache[cache_key]
            else:
                response = session.get(packages_url, timeout=20, stream=True)
                response.raise_for_status()

                try:
                    with gzip.open(response.raw, "rt", encoding="utf-8", errors="ignore") as f:
                        lines = f.readlines()
                        metadata_cache[cache_key] = lines
                except (OSError, EOFError, gzip.BadGzipFile) as gz_err:
                    return None, f"⛔ Failed to decompress metadata from {packages_url}: {gz_err}"

            current_package = None
            filename = None
            version = None

            for line in lines:
                line = line.strip()

                if line.startswith("Package: "):
                    current_package = line.split("Package: ", 1)[1]
                    filename = None
                    version = None

                elif line.startswith("Version: ") and current_package == pkg_name:
                    version = line.split("Version: ", 1)[1]

                elif line.startswith("Filename: ") and current_package == pkg_name:
                    filename = line.split("Filename: ", 1)[1]

                if current_package == pkg_name and filename and version:
                    return (filename, version), None

            return None, f"⛔ No metadata for '{pkg_name}' from: {mirror} [{component}]"

        except requests.exceptions.RequestException as e:
            if attempt < retries:
                time.sleep(random.uniform(*delay_range))
                continue

            if isinstance(e, requests.exceptions.Timeout):
                reason = "Timeout"
            elif isinstance(e, requests.exceptions.ConnectionError):
                reason = "Connection error"
            elif isinstance(e, requests.exceptions.HTTPError) and e.response is not None:
                reason = f"HTTP {e.response.status_code}"
            else:
                reason = e.__class__.__name__

            mirror_host = urlparse(packages_url).hostname
            return None, f"🚧 Failed to fetch metadata from: {mirror_host}: {reason} (after {retries} attempts)"

    return None, f"🚧 Unexpected error for '{pkg_name}' from {mirror} [{component}]"


def download_file(url, destination, quiet=True):
    try:
        response = session.get(url, timeout=20, stream=True)
        response.raise_for_status()

        dl_chunk_size = 1024 * 1024  # 1MB

        with open(destination, "wb") as f:
            for chunk in response.iter_content(chunk_size=dl_chunk_size):
                if chunk:
                    f.write(chunk)

        if not quiet:
            console.print(f"    ↪︎ 🔸[green]Saved to:[/green] {destination}")
            console.print("")

        return destination

    except requests.exceptions.RequestException as e:
        if isinstance(e, requests.exceptions.HTTPError) and e.response is not None:
            raise RuntimeError(f"🧾 HTTP {e.response.status_code}")
        elif isinstance(e, requests.exceptions.SSLError):
            raise RuntimeError("🔒 SSL error")
        elif isinstance(e, requests.exceptions.Timeout):
            raise RuntimeError("⌛ Timeout")
        elif isinstance(e, requests.exceptions.ConnectionError):
            if "NameResolutionError" in str(e):
                raise RuntimeError("🌐 DNS resolution failed")
            raise RuntimeError("🔌 Connection failed")
        else:
            raise RuntimeError(f"🚧 {e.__class__.__name__}")


def print_grouped_logs(logs):
    """Group and print logs with visual separation by error type."""
    fetch_errors = [msg for msg in logs if "Failed to fetch metadata" in msg]
    decompress_errors = [msg for msg in logs if "Failed to decompress metadata" in msg]
    no_metadata = [msg for msg in logs if "No metadata" in msg]
    unhandled = [msg for msg in logs if msg not in fetch_errors + decompress_errors + no_metadata]

    if fetch_errors:
        console.print("\n[bold red]🚧 Failed to Fetch Metadata:[/bold red]")
        for line in fetch_errors:
            console.print(f"  {line}")

    if decompress_errors:
        console.print("\n[bold red]📦 Failed to Decompress Metadata:[/bold red]")
        for line in decompress_errors:
            console.print(f"  {line}")

    if no_metadata:
        console.print("\n[bold yellow]📭 No Metadata Found:[/bold yellow]")
        for line in no_metadata:
            console.print(f"  {line}")

    if unhandled:
        console.print("\n[bold magenta]❓ Unhandled Errors:[/bold magenta]")
        for line in unhandled:
            console.print(f"  {line}")
