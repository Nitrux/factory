import io
import json
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from nx_bootstrap.rootfs import orchestrator


class RootfsOrchestrationTests(unittest.TestCase):
    def test_device_setup_falls_back_when_nodes_and_binds_are_blocked(self):
        with tempfile.TemporaryDirectory() as directory:
            rootfs = Path(directory)
            (rootfs / "dev").mkdir()
            mounts = []

            def blocked_operation(command, **kwargs):
                if command[0] == "mknod":
                    raise orchestrator.subprocess.CalledProcessError(1, command)
                if command[0] == "mount":
                    return orchestrator.subprocess.CompletedProcess(
                        command, 32, stdout="", stderr="mount blocked"
                    )
                raise AssertionError(f"Unexpected command: {command}")

            with patch.object(orchestrator.subprocess, "run", side_effect=blocked_operation):
                cleanup_paths = orchestrator._prepare_dev(rootfs, mounts)

            self.assertEqual(mounts, [])
            self.assertTrue(cleanup_paths)
            for name in ("null", "zero", "full", "random", "urandom", "tty", "console", "ptmx"):
                device = rootfs / "dev" / name
                self.assertTrue(device.exists())
                self.assertEqual(device.stat().st_mode & 0o777, 0o666)
            self.assertEqual((rootfs / "dev/shm").stat().st_mode & 0o7777, 0o1777)
            self.assertTrue((rootfs / "dev/fd").is_symlink())

    def test_device_setup_falls_back_when_mount_command_is_missing(self):
        with tempfile.TemporaryDirectory() as directory:
            rootfs = Path(directory)
            (rootfs / "dev").mkdir()
            mounts = []

            def missing_mount(command, **kwargs):
                if command[0] == "mknod":
                    raise orchestrator.subprocess.CalledProcessError(1, command)
                if command[0] == "mount":
                    raise FileNotFoundError("mount")
                raise AssertionError(f"Unexpected command: {command}")

            with patch.object(orchestrator.subprocess, "run", side_effect=missing_mount):
                cleanup_paths = orchestrator._prepare_dev(rootfs, mounts)

            self.assertEqual(mounts, [])
            self.assertTrue(cleanup_paths)
            self.assertEqual((rootfs / "dev/null").stat().st_mode & 0o777, 0o666)
            self.assertEqual((rootfs / "dev/shm").stat().st_mode & 0o7777, 0o1777)

    def test_downloads_repository_key_and_installs_armored_keyring(self):
        with tempfile.TemporaryDirectory() as directory:
            key_dir = Path(directory) / "etc/apt/keyrings"
            key_dir.mkdir(parents=True)
            key_data = b"-----BEGIN PGP PUBLIC KEY BLOCK-----\nkey\n"
            repository = {
                "name": "nitrux-rootfs",
                "key_url": "https://packages.invalid/repository.gpg",
                "keyring": None,
            }
            with patch.object(
                orchestrator.urllib.request, "urlopen", return_value=io.BytesIO(key_data)
            ) as urlopen:
                orchestrator._prepare_keyring(repository, 1, key_dir)

            urlopen.assert_called_once_with(repository["key_url"], timeout=30)
            self.assertEqual(repository["signed_by"], "/etc/apt/keyrings/1-nitrux-rootfs.asc")
            self.assertEqual(repository["host_key"], str(key_dir / "1-nitrux-rootfs.asc"))
            key_path = key_dir / "1-nitrux-rootfs.asc"
            self.assertEqual(key_path.read_bytes(), key_data)
            self.assertEqual(key_path.stat().st_mode & 0o777, 0o644)

    def test_apt_worker_falls_back_to_system_python_for_pipx_runtime(self):
        rootfs = Path("/tmp/factory-test-root")
        packages = ["apt", "curl"]
        excluded = ["systemd-sysv"]

        with patch.dict("sys.modules", {"apt": None, "apt_pkg": None}), \
             patch.object(orchestrator.sys, "executable", "/opt/pipx/venvs/factory-cli/bin/python"), \
             patch.object(orchestrator.subprocess, "run") as run:
            orchestrator._install_packages(rootfs, "amd64", packages, excluded)

        command = run.call_args.args[0]
        request = json.loads(run.call_args.kwargs["input"])
        self.assertEqual(command[0], "/usr/bin/python3")
        self.assertEqual(command[1], str(Path(orchestrator.__file__).with_name("apt_worker.py")))
        self.assertEqual(request, {
            "rootfs": str(rootfs),
            "architecture": "amd64",
            "packages": packages,
            "excluded_packages": excluded,
        })

    def test_creates_signed_sources_and_readable_archive(self):
        real_run = orchestrator.subprocess.run
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            key = work / "repo.gpg"
            key.write_bytes(b"test repository key")
            rootfs = work / "build/rootfs"
            archive = work / "build/rootfs.tar.xz"
            repositories = [{
                "name": "test",
                "url": "https://example.invalid/debian",
                "release": "stable",
                "arch": "amd64",
                "components": ["main"],
                "keyring": str(key),
            }]

            def install_packages(target, architecture, packages, excluded):
                self.assertEqual(architecture, "amd64")
                self.assertEqual(packages, ["apt", "curl"])
                self.assertEqual(excluded, ["systemd-sysv"])
                payload = target / "usr/share/rootfs-orchestrator-test"
                payload.parent.mkdir(parents=True, exist_ok=True)
                payload.write_text("installed")

            observed = {}

            def run(command, **kwargs):
                if command[0] == "tar":
                    observed["archive"] = command
                return real_run(command, **kwargs)

            with patch.object(orchestrator.os, "geteuid", return_value=0), \
                 patch.object(orchestrator, "_prepare_dev", return_value=[]), \
                 patch.object(orchestrator, "_install_packages", side_effect=install_packages), \
                 patch.object(orchestrator.subprocess, "run", side_effect=run):
                result = orchestrator.create_base_system(
                    ["apt", "curl"], repositories,
                    rootfs_path=rootfs,
                    archive_path=archive,
                    config_dir=work,
                    excluded_packages=["systemd-sysv"],
                    compression="xz",
                    compression_level=1,
                )

            self.assertEqual(result["archive"], archive)
            self.assertEqual(archive.stat().st_mode & 0o777, 0o644)
            self.assertTrue((rootfs / ".nbs-rootfs").is_file())
            self.assertFalse((rootfs / "usr/sbin/policy-rc.d").exists())
            self.assertTrue((rootfs / "etc/apt/keyrings/1-test.gpg").is_file())
            sources = (rootfs / "etc/apt/sources.list.d/nbs.list").read_text()
            self.assertIn("signed-by=/etc/apt/keyrings/1-test.gpg", sources)
            self.assertFalse((rootfs / "etc/apt/sources.list").exists())
            self.assertIn("--numeric-owner", observed["archive"])
            self.assertIn("--xattrs", observed["archive"])
            self.assertIn("--xattrs-include=*", observed["archive"])
            self.assertIn("--acls", observed["archive"])

            with tarfile.open(archive, "r:xz") as rootfs_archive:
                members = rootfs_archive.getnames()
            self.assertIn("./usr/share/rootfs-orchestrator-test", members)
            self.assertFalse(any(name.endswith(".nbs-rootfs") for name in members))


if __name__ == "__main__":
    unittest.main()
