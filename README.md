# Factory | [![License](https://img.shields.io/badge/License-BSD_3--Clause-blue.svg)](https://opensource.org/licenses/BSD-3-Clause)

<p align="center">
  <img width="128" height="128" src="https://raw.githubusercontent.com/Nitrux/luv-icon-theme/master/Luv/apps/64/live-installer.svg">
</p>

# Introduction

Factory is system for Nitrux using YAML configurations to create rootfs archives and ISO images.

It uses [Python APT](https://apt-team.pages.debian.net/python-apt/library/index.html) to configure repositories, resolve dependencies, and install packages, then runs the selected ISO stages.

> [!WARNING]
> Factory primarily targets Nitrux OS, and using this utility in other distributions may or may not work. Compatibility with other distributions is incidental, not intentional.

For more in-depth information about Factory, please see the [Wiki](https://github.com/Nitrux/factory/wiki).

## Requirements

- Python 3.10 and newer. 
- Root privileges for rootfs and ISO builds.

### Runtime Requirements

```
python3-apt
mount
tar
```

# Installation

To install NX AppHub CLI we recommend using `pipx`.

## Single-user

```
pipx install git+https://github.com/Nitrux/factory.git
```

> [!WARNING]
> pipx will install `factory` to `$HOME/.local/bin`, run `pipx ensurepath` to add this directory to `$PATH`.


## System-wide

```
sudo pipx install --global git+https://github.com/Nitrux/factory.git
```

# Uninstallation

To uninstall NX AppHub CLI, do the following.

## Single-user

```
pipx uninstall factory
```


## System-wide

```
sudo pipx uninstall --global factory
```

# Usage

Use the commands below to build artifacts or download package snapshots.

- `build` → Builds a rootfs archive or an ISO. Specify exactly one build target.
  - `--rfs <file>` → Reads a rootfs YAML file, installs the fixed minbase plus the listed packages from its configured APT repositories, and creates a compressed rootfs archive. Run as `factory build --rfs <file>`.
  - `--iso <file>` → Reads an ISO YAML file, uses its rootfs archive and external ISO assets, runs the selected stages, and creates an ISO image. Run as `factory build --iso <file>`.
- `snapshot <file>` → Reads a snapshot YAML file, downloads the selected Debian packages from the configured APT repository, and verifies their SHA-256 checksums. Run as `factory snapshot <file>`.

# Licensing

The license for this repository and its contents is **BSD-3-Clause**.

# Issues

If you find problems with the contents of this repository, please create an issue and use the **🐞 Bug report** template.

## Submitting a bug report

Before submitting a bug, you should look at the [existing bug reports]([url](https://github.com/Nitrux/nx-apphub/issues)) to verify that no one has reported the bug already.

©2026 Nitrux Latinoamericana S.C.
