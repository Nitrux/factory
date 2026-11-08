#!/usr/bin/env python3
# SPDX-License-Identifier: BSD-3-Clause
# Copyright <2025> <Uri Herrera <uri_herrera@nxos.org>>

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import warnings


# Factory's fixed minbase roots. Repository priority and Essential flags do not
# expand this list; APT resolves only these roots and their hard dependencies.
MINBASE_PACKAGES = (
    "apt",
    "base-files",
    "base-passwd",
    "bash",
    "coreutils",
    "dash",
    "debconf",
    "debianutils",
    "diffutils",
    "dpkg",
    "findutils",
    "grep",
    "gzip",
    "hostname",
    "init-system-helpers",
    "libc-bin",
    "login",
    "mawk",
    "mount",
    "ncurses-base",
    "ncurses-bin",
    "passwd",
    "perl-base",
    "sed",
    "sysvinit-utils",
    "tar",
    "tzdata",
)


APT_HOOKS = (
    "DPkg::Pre-Invoke",
    "DPkg::Post-Invoke",
    "DPkg::Pre-Install-Pkgs",
    "APT::Update::Pre-Invoke",
    "APT::Update::Post-Invoke",
    "APT::Update::Post-Invoke-Success",
)


def _clear_hooks(config):
    for key in APT_HOOKS:
        config.clear(key)


def _configure_apt(apt_pkg, root, architecture, config_dir):
    apt_pkg.init_config()
    for key, value in {
        "Dir": str(root),
        "Dir::Etc": str(root / "etc/apt"),
        "Dir::Etc::main": "/dev/null",
        "Dir::Etc::parts": str(config_dir / "empty"),
        "Dir::Etc::sourcelist": str(root / "etc/apt/sources.list"),
        "Dir::Etc::sourceparts": str(root / "etc/apt/sources.list.d"),
        "Dir::Etc::trusted": str(root / "etc/apt/trusted.gpg"),
        "Dir::Etc::trustedparts": str(root / "etc/apt/trusted.gpg.d"),
        "Dir::Etc::preferences": str(root / "etc/apt/preferences"),
        "Dir::Etc::preferencesparts": str(root / "etc/apt/preferences.d"),
        "Dir::State": str(root / "var/lib/apt"),
        "Dir::State::status": str(root / "var/lib/dpkg/status"),
        "Dir::State::lists": str(root / "var/lib/apt/lists"),
        "Dir::Cache": str(root / "var/cache/apt"),
        "Dir::Cache::archives": str(root / "var/cache/apt/archives"),
        "Dir::Log": str(root / "var/log/apt"),
        "APT::Architecture": architecture,
        "APT::Install-Recommends": "false",
        "APT::Install-Suggests": "false",
    }.items():
        apt_pkg.config.set(key, value)
    apt_pkg.config.clear("APT::Architectures")
    apt_pkg.config.set("APT::Architectures::", architecture)
    apt_pkg.config.clear("DPkg::Options")
    apt_pkg.config.set("DPkg::Options::", f"--root={root}")
    _clear_hooks(apt_pkg.config)


def _prepare_usr(root):
    for name in ("bin", "sbin", "lib", "lib32", "lib64", "libo32", "libx32"):
        destination = root / "usr" / name
        destination.mkdir(parents=True, exist_ok=True)
        link = root / name
        if not link.is_symlink():
            if link.exists():
                # Bootstrap runs before package extraction, so existing aliases
                # must be empty directories rather than partially merged trees.
                link.rmdir()
            link.symlink_to(f"usr/{name}")



def _prepare_bootstrap_links(root):
    # Alternatives have not run yet. Provide the shell and awk entry points
    # needed by maintainer scripts from the runtimes already extracted.
    for name, providers in (("sh", ("dash", "bash")), ("awk", ("mawk", "gawk", "original-awk"))):
        link = root / "usr/bin" / name
        if link.exists():
            continue
        for provider in providers:
            if (link.parent / provider).exists():
                if link.is_symlink():
                    link.unlink()
                link.symlink_to(provider)
                break


def _extract_archive(archive, root):
    # Preserve the merged-/usr directory aliases when an older package still
    # contains entries under /bin, /sbin, or /lib.
    with subprocess.Popen(
        ["dpkg-deb", "--fsys-tarfile", str(archive)], stdout=subprocess.PIPE
    ) as payload:
        try:
            subprocess.run(
                [
                    "tar", "--extract", "--file=-", "--keep-directory-symlink",
                    "--preserve-permissions", "--numeric-owner", "-C", str(root),
                ],
                stdin=payload.stdout,
                check=True,
            )
        finally:
            payload.stdout.close()
        if payload.wait():
            raise RuntimeError(f"Unable to extract bootstrap package: {archive}")


def _mark_packages(cache, names):
    with cache.actiongroup():
        for name in names:
            try:
                package = cache[name]
            except KeyError as error:
                raise RuntimeError(
                    f"Package is not available from the configured repositories: {name}"
                ) from error
            if package.candidate is None:
                raise RuntimeError(
                    f"Package has no install candidate in the configured repositories: {name}"
                )
            package.mark_install()
    if cache.broken_count:
        raise RuntimeError(f"APT dependency resolution left {cache.broken_count} broken packages")


def _check_exclusions(cache, excluded_packages):
    excluded = set(excluded_packages)
    for package in cache.get_changes():
        if package.name in excluded or package.fullname in excluded:
            raise RuntimeError(f"APT selected an excluded package: {package.fullname}")


def _minbase_packages(architecture, excluded_packages, uses_https=False):
    excluded = set(excluded_packages)
    names = list(MINBASE_PACKAGES)
    if uses_https:
        names.append("ca-certificates")
    return sorted(
        name for name in names
        if not excluded.intersection((name, f"{name}:{architecture}", f"{name}:all"))
    )


def _fetch_binary(package, archives, progress):
    with warnings.catch_warnings():
        warnings.filterwarnings(
            "ignore",
            message=r"W:Download is performed unsandboxed as root\b.*",
            category=Warning,
        )
        return package.candidate.fetch_binary(
            destdir=str(archives), progress=progress(), allow_unauthenticated=False
        )


def _status(message):
    if sys.stdout.isatty():
        print(f"\033[1;36m{message}\033[0m", flush=True)
    else:
        print(message, flush=True)


def _download_bootstrap_packages(changes, archives, progress_factory):
    if not changes:
        return []

    _status(f"Downloading {len(changes)} minbase packages")
    try:
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
    except ImportError:
        downloaded = []
        total = len(changes)
        for index, package in enumerate(changes, start=1):
            downloaded.append(_fetch_binary(package, archives, progress_factory))
            if sys.stdout.isatty():
                width = 28
                complete = width * index // total
                bar = "━" * complete + "─" * (width - complete)
                package_label = package.name if len(package.name) <= 32 else package.name[:29] + "..."
                sys.stdout.write(
                    f"\r\033[1;36m{package_label:<32}\033[0m "
                    f"\033[36m{bar}\033[0m {index}/{total}"
                )
                sys.stdout.flush()
        if sys.stdout.isatty():
            sys.stdout.write("\n")
            sys.stdout.flush()
        else:
            print(f"Downloaded {total} minbase packages", flush=True)
        return downloaded

    downloaded = []
    with Progress(
        SpinnerColumn(),
        TextColumn(
            "[cyan]{task.fields[package]}",
            table_column=Column(width=32, no_wrap=True, overflow="ellipsis"),
        ),
        BarColumn(bar_width=28),
        TaskProgressColumn(),
        MofNCompleteColumn(),
        console=Console(file=sys.stdout),
        transient=False,
    ) as progress:
        task = progress.add_task("download", total=len(changes), package="")
        for package in changes:
            progress.update(task, package=package.name)
            downloaded.append(_fetch_binary(package, archives, progress_factory))
            progress.advance(task)
    return downloaded


def _bootstrap(cache, root, architecture, excluded_packages, progress, uses_https=False):
    seeds = _minbase_packages(architecture, excluded_packages, uses_https)
    _mark_packages(cache, seeds)
    _check_exclusions(cache, excluded_packages)
    changes = sorted(cache.get_changes(), key=lambda package: package.fullname)
    archives = root / "var/cache/apt/archives"
    archives.mkdir(parents=True, exist_ok=True)
    downloaded = _download_bootstrap_packages(changes, archives, progress)
    _prepare_usr(root)
    _status("Extracting bootstrap runtime before executing package scripts")
    for archive in downloaded:
        _extract_archive(archive, root)
    _prepare_bootstrap_links(root)

    environment = os.environ.copy()
    environment["PATH"] = "/usr/sbin:/usr/bin:/sbin:/bin"
    try:
        subprocess.run(
            ["chroot", str(root), "/bin/sh", "-ec", "command -v awk >/dev/null; dpkg --version >/dev/null; /bin/true"],
            env=environment,
            check=True,
        )
    except subprocess.CalledProcessError as error:
        raise RuntimeError(
            "The extracted bootstrap runtime cannot execute /bin/sh, awk, dpkg, or /bin/true; "
            "check the repository base packages, exclusions, target architecture, and chroot permissions"
        ) from error



def _commit(cache, apt_pkg, progress):
    _clear_hooks(apt_pkg.config)

    class InstallProgress(progress.base.InstallProgress):
        def error(self, package, message):
            try:
                super().error(package, message)
            except NotImplementedError:
                pass
            print(f"APT installation error for {package}: {message}", file=sys.stderr, flush=True)

    with InstallProgress() as install_progress:
        committed = cache.commit(progress.base.AcquireProgress(), install_progress)
    if not committed:
        raise RuntimeError("APT did not complete the requested package installation")


def install(request):
    root = Path(request["rootfs"]).resolve()
    architecture = request["architecture"]
    packages = request["packages"]
    excluded_packages = request["excluded_packages"]
    previous_config = os.environ.get("APT_CONFIG")
    preferences_path = root / "etc/apt/preferences.d/nbs-exclusions"
    cache = None
    with tempfile.TemporaryDirectory(prefix="nbs-apt-") as directory:
        config_dir = Path(directory)
        (config_dir / "empty").mkdir()
        config_path = config_dir / "apt.conf"
        empty_dir = str(config_dir / "empty").replace("\\", "\\\\").replace('"', '\\"')
        config_path.write_text(
            f'Dir::Etc::main "/dev/null";\nDir::Etc::parts "{empty_dir}";\n'
        )
        # APT_CONFIG is read before the normal apt.conf.d files. Set it before
        # importing apt so even its initialisation cannot load container hooks.
        os.environ["APT_CONFIG"] = str(config_path)
        try:
            import apt
            import apt_pkg
            import apt.progress.base

            _configure_apt(apt_pkg, root, architecture, config_dir)
            preferences_path.parent.mkdir(parents=True, exist_ok=True)
            if excluded_packages:
                preferences_path.write_text("\n".join(
                    line for name in excluded_packages
                    for line in (f"Package: {name}", "Pin: version *", "Pin-Priority: -1", "")
                ))

            # All paths were set explicitly above. Passing rootdir here would
            # reinitialise configuration and could reload apt.conf.d hooks.
            cache = apt.Cache()
            print("Refreshing APT indexes", flush=True)
            cache.update(fetch_progress=apt.progress.base.AcquireProgress())
            cache.open(None)
            sources = apt_pkg.SourceList()
            sources.read_main_list()
            uses_https = any(source.uri.startswith("https://") for source in sources.list)
            _bootstrap(
                cache, root, architecture, excluded_packages,
                apt.progress.base.AcquireProgress, uses_https=uses_https,
            )
            environment_keys = ("DEBIAN_FRONTEND", "DEBCONF_NONINTERACTIVE_SEEN", "PATH")
            previous_environment = {key: os.environ.get(key) for key in environment_keys}
            os.environ.update({
                "DEBIAN_FRONTEND": "noninteractive",
                "DEBCONF_NONINTERACTIVE_SEEN": "true",
                "PATH": "/usr/sbin:/usr/bin:/sbin:/bin",
            })
            try:
                _status("Installing and configuring the fixed minbase with APT")
                _commit(cache, apt_pkg, apt.progress)
                cache.open(None)
                _mark_packages(cache, packages)
                _check_exclusions(cache, excluded_packages)
                if cache.get_changes():
                    print(f"Adding {len(packages)} YAML packages with APT", flush=True)
                    _commit(cache, apt_pkg, apt.progress)
            finally:
                for key, value in previous_environment.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value
        finally:
            if cache is not None:
                cache.close()
            if preferences_path.exists():
                preferences_path.unlink()
            if previous_config is None:
                os.environ.pop("APT_CONFIG", None)
            else:
                os.environ["APT_CONFIG"] = previous_config


def main():
    install(json.load(sys.stdin))


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"Factory APT worker failed: {error}", file=sys.stderr)
        raise SystemExit(1) from error
