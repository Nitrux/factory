import contextlib
import io
import os
import types
from pathlib import Path
import shutil
import subprocess
import tempfile
import tarfile
import unittest
from unittest.mock import patch

from nx_bootstrap.rootfs import apt_worker


class Candidate:
    architecture = "amd64"

    def __init__(self, archive, priority="optional"):
        self.archive = archive
        self.priority = priority

    def fetch_binary(self, **kwargs):
        if kwargs["allow_unauthenticated"]:
            raise AssertionError("Bootstrap downloads must require authentication")
        return str(self.archive)


class Package:
    def __init__(self, name, candidate, essential=False, dependencies=()):
        self.name = name
        self.fullname = name + ":amd64"
        self.candidate = candidate
        self.essential = essential
        self.dependencies = dependencies
        self.cache = None

    def mark_install(self):
        self.cache.marked.add(self.name)
        for dependency in self.dependencies:
            self.cache[dependency].mark_install()


class Cache:
    broken_count = 0

    def __init__(self, packages):
        self.packages = {package.name: package for package in packages}
        self.marked = set()
        for package in packages:
            package.cache = self

    def __iter__(self):
        return iter(self.packages.values())

    def __getitem__(self, name):
        return self.packages[name]

    def actiongroup(self):
        return contextlib.nullcontext()

    def get_changes(self):
        return [self.packages[name] for name in self.marked]


class Config(dict):
    def set(self, key, value):
        self[key] = value

    def clear(self, key):
        for entry in list(self):
            if entry == key or entry.startswith(key + "::"):
                del self[entry]


class BootstrapTests(unittest.TestCase):
    def test_config_targets_rootfs_and_removes_inherited_hooks(self):
        config = Config({hook + "::0": "touch /etc/localtime" for hook in apt_worker.APT_HOOKS})
        module = type("AptPkg", (), {"config": config, "init_config": staticmethod(lambda: None)})
        apt_worker._configure_apt(module, Path("/build/rootfs"), "amd64", Path("/build/config"))
        self.assertEqual(config["Dir::State::status"], "/build/rootfs/var/lib/dpkg/status")
        self.assertEqual(config["Dir::Etc::parts"], "/build/config/empty")
        self.assertEqual(config["DPkg::Options::"], "--root=/build/rootfs")
        self.assertEqual(config["APT::Install-Recommends"], "false")
        self.assertEqual(config["APT::Install-Suggests"], "false")
        self.assertFalse(any(key.startswith(hook) for key in config for hook in apt_worker.APT_HOOKS))

    def test_minbase_baseline_is_fixed_and_https_only_adds_certificates(self):
        baseline = set(apt_worker._minbase_packages("amd64", []))
        self.assertEqual(baseline, set(apt_worker.MINBASE_PACKAGES))
        self.assertTrue({"apt", "bash", "coreutils", "dpkg", "mawk"}.issubset(baseline))
        self.assertNotIn("nitrux-live-user", baseline)
        self.assertEqual(
            set(apt_worker._minbase_packages("amd64", [], uses_https=True)) - baseline,
            {"ca-certificates"},
        )

    def test_yaml_exclusions_apply_to_the_fixed_baseline_and_https_support(self):
        excluded = ["apt:amd64", "bash", "ca-certificates:all"]
        baseline = apt_worker._minbase_packages("amd64", excluded, uses_https=True)
        self.assertNotIn("apt", baseline)
        self.assertNotIn("bash", baseline)
        self.assertNotIn("ca-certificates", baseline)

    def test_missing_named_baseline_package_fails_before_download(self):
        cache = Cache([
            Package(name, Candidate("must-not-be-downloaded"))
            for name in apt_worker.MINBASE_PACKAGES if name != "dpkg"
        ])
        with self.assertRaisesRegex(RuntimeError, "not available.*dpkg"):
            apt_worker._bootstrap(cache, Path("/unused"), "amd64", [], lambda: None)

    def test_excluded_transitive_bootstrap_dependency_is_rejected(self):
        cache = Cache([
            Package(name, Candidate("unused")) for name in apt_worker.MINBASE_PACKAGES
        ] + [Package("runtime", Candidate("unused"))])
        cache["bash"].dependencies = ("runtime",)
        with self.assertRaisesRegex(RuntimeError, "excluded package: runtime"):
            apt_worker._bootstrap(cache, Path("/unused"), "amd64", ["runtime"], lambda: None)

    @unittest.skipUnless(shutil.which("tar") and shutil.which("cat"), "needs tar and cat")
    def test_payloads_seed_shell_and_dependencies_before_any_chroot_script(self):
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            root = work / "root"
            root.mkdir()

            def package(name, files):
                source = work / name
                for filename, contents in files.items():
                    path = source / filename
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(contents)
                    path.chmod(0o755)
                archive = work / (name + ".deb")
                # Supply the data stream dpkg-deb would produce. GNU tar still
                # performs the actual extraction and merged-/usr handling.
                with tarfile.open(archive, "w") as payload:
                    payload.add(source, arcname=".")
                return archive

            shell = package("shell", {"bin/dash": "#!/bin/sh\n", "bin/true": "true\n"})
            runtime = package("runtime", {"lib/runtime": "loader fixture\n", "usr/bin/dpkg": "dpkg\n", "usr/bin/mawk": "awk\n"})
            apt = package("apt", {"usr/bin/apt": "apt\n"})
            cache = Cache([
                Package(name, Candidate(runtime)) for name in apt_worker.MINBASE_PACKAGES
            ] + [
                Package("runtime", Candidate(runtime)),
                Package("editor", Candidate("must-not-be-downloaded")),
                Package("nitrux-live-user", Candidate("must-not-be-downloaded", "required"), essential=True),
            ])
            cache["bash"].candidate = Candidate(shell)
            cache["bash"].dependencies = ("runtime",)
            cache["apt"].candidate = Candidate(apt)
            run = subprocess.run
            popen = subprocess.Popen
            checked = []

            def package_payload(command, **kwargs):
                if command[0] == "dpkg-deb":
                    self.assertEqual(command[1], "--fsys-tarfile")
                    return popen(["cat", command[2]], **kwargs)
                return popen(command, **kwargs)

            def check_run(command, **kwargs):
                if command[0] == "chroot":
                    self.assertTrue((root / "bin").is_symlink())
                    self.assertEqual((root / "usr/bin/sh").read_text(), "#!/bin/sh\n")
                    self.assertEqual((root / "lib/runtime").read_text(), "loader fixture\n")
                    self.assertTrue((root / "usr/bin/dpkg").is_file())
                    self.assertTrue((root / "usr/bin/sh").is_symlink())
                    self.assertEqual((root / "usr/bin/awk").read_text(), "awk\n")
                    checked.append(command)
                    return subprocess.CompletedProcess(command, 0)
                return run(command, **kwargs)

            with patch.object(apt_worker.subprocess, "run", side_effect=check_run):
                with patch.object(apt_worker.subprocess, "Popen", side_effect=package_payload):
                    apt_worker._bootstrap(cache, root, "amd64", [], lambda: None)
            self.assertEqual(len(checked), 1)
            self.assertEqual(cache.marked, set(apt_worker.MINBASE_PACKAGES) | {"runtime"})
            self.assertNotIn("nitrux-live-user", cache.marked)
            apt_worker._mark_packages(cache, ["nitrux-live-user"])
            self.assertIn("nitrux-live-user", cache.marked)
            self.assertFalse((root / "var/lib/dpkg/status").exists())


class InstallLifecycleTests(unittest.TestCase):
    def test_commit_reports_install_errors_when_base_callback_is_abstract(self):
        class BaseInstallProgress:
            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

            def error(self, package, message):
                raise NotImplementedError

        progress = types.SimpleNamespace(base=types.SimpleNamespace(
            InstallProgress=BaseInstallProgress,
            AcquireProgress=lambda: object(),
        ))
        apt_pkg = types.SimpleNamespace(config=Config())

        class CommitCache:
            def commit(self, _, install_progress):
                install_progress.error("broken-package", "unpack failed")
                return True

        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            apt_worker._commit(CommitCache(), apt_pkg, progress)
        self.assertIn("APT installation error for broken-package: unpack failed", stderr.getvalue())

    def run_install(self, root, fail_bootstrap=False):
        events = []
        config = Config()
        apt_pkg = types.ModuleType("apt_pkg")
        apt_pkg.config = config
        apt_pkg.init_config = lambda: None
        apt_pkg.SourceList = lambda: types.SimpleNamespace(
            read_main_list=lambda: None,
            list=[types.SimpleNamespace(uri="https://example.invalid/repository")],
        )
        apt = types.ModuleType("apt")
        progress = types.ModuleType("apt.progress")
        base = types.ModuleType("apt.progress.base")
        base.AcquireProgress = lambda: object()

        class InstallProgress:
            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

            def error(self, package, message):
                pass

        base.InstallProgress = InstallProgress
        apt.progress = progress
        progress.base = base
        cache = Cache([
            Package("base-files", Candidate("unused")),
            Package("requested", Candidate("unused")),
            Package("nitrux-live-user", Candidate("unused", "required")),
        ])

        def make_cache():
            config_file = Path(os.environ["APT_CONFIG"])
            self.assertIn('Dir::Etc::main "/dev/null"', config_file.read_text())
            self.assertTrue(Path(config["Dir::Etc::parts"]).is_dir())
            return cache

        def bootstrap(*args, **kwargs):
            self.assertTrue(kwargs["uses_https"])
            events.append("bootstrap")
            if fail_bootstrap:
                raise RuntimeError("bootstrap failed")
            cache["base-files"].mark_install()
            self.assertNotIn("requested", cache.marked)
            config["DPkg::Post-Invoke::0"] = "must not execute"

        def commit(*args):
            self.assertIn("bootstrap", events)
            self.assertFalse(any(key.startswith("DPkg::Post-Invoke") for key in config))
            self.assertEqual(os.environ["DEBIAN_FRONTEND"], "noninteractive")
            self.assertNotIn("nitrux-live-user", cache.marked)
            if "commit-minbase" not in events:
                self.assertEqual(cache.marked, {"base-files"})
                events.append("commit-minbase")
            else:
                self.assertEqual(cache.marked, {"requested"})
                events.append("commit-yaml")
            return True

        def reopen(*args):
            if "commit-minbase" in events:
                cache.marked.clear()

        cache.update = lambda **kwargs: events.append("update")
        cache.open = reopen
        cache.commit = commit
        cache.close = lambda: events.append("close")
        apt.Cache = make_cache
        modules = {
            "apt": apt, "apt_pkg": apt_pkg, "apt.progress": progress,
            "apt.progress.base": base,
        }
        request = {
            "rootfs": str(root), "architecture": "amd64",
            "packages": ["requested"], "excluded_packages": ["excluded"],
        }
        with patch.dict("sys.modules", modules), patch.dict(os.environ, {"APT_CONFIG": "original"}):
            with patch.object(apt_worker, "_bootstrap", side_effect=bootstrap):
                if fail_bootstrap:
                    with self.assertRaisesRegex(RuntimeError, "bootstrap failed"):
                        apt_worker.install(request)
                else:
                    apt_worker.install(request)
            self.assertEqual(os.environ["APT_CONFIG"], "original")
        self.assertFalse((root / "etc/apt/preferences.d/nbs-exclusions").exists())
        return events

    def test_minbase_is_configured_before_yaml_packages_with_host_hooks_removed(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(self.run_install(Path(directory)), ["update", "bootstrap", "commit-minbase", "commit-yaml", "close"])

    def test_failed_bootstrap_does_not_run_dpkg_commit_and_cleans_preferences(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(self.run_install(Path(directory), True), ["update", "bootstrap", "close"])


if __name__ == "__main__":
    unittest.main()
