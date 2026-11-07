import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from nitrux_factory import iso


class ISOOrchestrationTests(unittest.TestCase):
    def test_builds_from_local_archive_and_writes_outputs(self):
        real_run = subprocess.run
        real_popen = subprocess.Popen
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            rootfs = work / "rootfs"
            files = (
                "etc/placeholder",
                "boot/vmlinuz-1",
                "boot/initrd.img-1",
                "usr/lib/shim/shimx64.efi.signed",
                "usr/lib/shim/mmx64.efi.signed",
                "usr/lib/grub/x86_64-efi-signed/gcdx64.efi.signed",
            )
            for name in files:
                path = rootfs / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("fixture")

            rootfs_archive = work / "rootfs.tar.xz"
            with tarfile.open(rootfs_archive, "w:xz") as archive:
                archive.add(rootfs, arcname=".")

            data_dir = work / "iso-data"
            (data_dir / "configs/scripts/stages").mkdir(parents=True)
            (data_dir / "configs/scripts/others").mkdir(parents=True)
            (data_dir / "configs/files/grub_files/live").mkdir(parents=True)
            grub_theme = data_dir / "configs/files/grub_files/theme/custom"
            grub_theme.mkdir(parents=True)
            (data_dir / "tooling").mkdir(parents=True)
            for asset in (
                "bootstrap",
                "configs/scripts/stages/01-add-build-sources",
                "configs/files/grub_files/live/grub.cfg",
                "configs/files/grub_files/live/loopback.cfg",
                "tooling/runch",
                "tooling/mkiso",
            ):
                (data_dir / asset).write_text("fixture")
            config_path = work / "iso.yml"
            config = {"iso": {
                "data_dir": str(data_dir),
                "grub_theme": "configs/files/grub_files/theme/custom",
                "rootfs": {"path": str(rootfs_archive)},
                "output_dir": str(work / "output"),
                "image": "smoke.iso",
                "prepare_host": False,
                "stages": ["01-add-build-sources"],
                "squashfs": {"compression": "zstd", "level": 18},
            }}
            config_path.write_text(yaml.safe_dump(config))
            second_config = {"iso": {
                **config["iso"],
                "output_dir": str(work / "second-output"),
                "image": "second-smoke.iso",
            }}
            second_config_path = work / "second.yml"
            second_config_path.write_text(yaml.safe_dump(second_config))
            observed = {}

            def run(command, **kwargs):
                if command[0] == "tar":
                    observed["extract"] = command
                    with patch.object(subprocess, "Popen", real_popen):
                        return real_run(command, **kwargs)
                if command[0] == "sbverify":
                    return subprocess.CompletedProcess(command, 0)
                if command[0] == "mksquashfs":
                    observed["squashfs"] = command
                    Path(command[2]).write_text("squashfs")
                    return subprocess.CompletedProcess(command, 0)
                if command[0] == "mkiso":
                    Path(command[-1]).write_text("iso")
                    return subprocess.CompletedProcess(command, 0)
                with patch.object(subprocess, "Popen", real_popen):
                    return real_run(command, **kwargs)

            class FakeStageRunner:
                stdout = ()

                def wait(self):
                    stage_root = Path(observed["stage_root"])
                    (stage_root / "installed_pkgs_start.txt").write_text("start")
                    (stage_root / "installed_pkgs_end.txt").write_text("end")
                    return 0

            def popen(command, **kwargs):
                observed["stage_root"] = kwargs["cwd"]
                observed["stage_command"] = command
                return FakeStageRunner()

            with patch.object(iso.os, "geteuid", return_value=0), \
                 patch.object(iso.shutil, "which", return_value="/bin/true"), \
                 patch.object(iso.subprocess, "run", side_effect=run), \
                 patch.object(iso.subprocess, "Popen", side_effect=popen):
                result = iso.build_iso(config_path)
                second_result = iso.build_iso(second_config_path)

            self.assertTrue(result["image"].is_file())
            self.assertTrue(second_result["image"].is_file())
            self.assertTrue(result["checksum"].is_file())
            self.assertEqual(len(result["package_lists"]), 2)
            self.assertIn("01-add-build-sources", observed["stage_command"])
            self.assertIn("--numeric-owner", observed["extract"])
            self.assertIn("--xattrs", observed["extract"])
            self.assertIn("--xattrs-include=*", observed["extract"])
            self.assertIn("--acls", observed["extract"])
            squashfs = observed["squashfs"]
            self.assertEqual(squashfs[squashfs.index("-comp") + 1], "zstd")
            self.assertEqual(squashfs[squashfs.index("-Xcompression-level") + 1], "18")


if __name__ == "__main__":
    unittest.main()
