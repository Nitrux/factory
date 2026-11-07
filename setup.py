#!/usr/bin/env python3
# SPDX-License-Identifier: BSD-3-Clause
# Copyright <2025> <Uri Herrera <uri_herrera@nxos.org>>

from setuptools import setup, find_packages
# <---
# --->
setup(
    name="nitrux-factory",
    version="0.0.1",
    packages=find_packages("src"),
    package_dir={"": "src"},
    include_package_data=True,
    package_data={"nx_bootstrap": ["data/**/*"]},
    install_requires=[
        "typer",
        "rich",
        "pyyaml",
        "requests",
    ],
    entry_points={
        "console_scripts": [
            "factory=nx_bootstrap.cli:main"
        ]
    },
    author="Uri Herrera",
    author_email="uri_herrera@nxos.org",
    description="Build Nitrux root filesystems and ISO images from YAML configurations.",
    url="https://github.com/Nitrux/nbs",
    classifiers=[
        "Programming Language :: Python :: 3",
        "License :: OSI Approved :: BSD 3 Clause License",
        "Operating System :: POSIX :: Linux"
    ],
    python_requires='>=3.10',
)
