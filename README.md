# Factory | [![License](https://img.shields.io/badge/License-BSD_3--Clause-blue.svg)](https://opensource.org/licenses/BSD-3-Clause)

Nitrux rootfs and ISO build tooling.

`Factory` builds root filesystems and ISO images from YAML configurations. It uses python-apt to configure repositories, resolve dependencies, and install packages, then runs the selected ISO stages.

See the [Factory wiki](https://github.com/Nitrux/nbs/wiki) for usage and configuration details.

## Requirements

- Python 3 with `python3-apt` available to the system interpreter used by APT.
- Root privileges for rootfs and ISO builds.

## Runtime Requirements

```
APT and python3-apt
mount and umount
GNU tar with xz support
```

# Licensing

This repository and its contents are licensed under **BSD-3-Clause**.

# Issues

If you find problems with the contents of this repository, please create an issue.

## Submitting a bug report

Before submitting a bug, you should look at the [existing bug reports](https://github.com/Nitrux/nbs/issues) to verify that no one has reported the bug already.

©2026 Nitrux Latinoamericana S.C.
