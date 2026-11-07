import gzip
import hashlib
import lzma
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from nx_bootstrap import snapshot


class Response:
    status_code = 200

    def __init__(self, content):
        self.content = content

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def close(self):
        pass

    def raise_for_status(self):
        pass

    def iter_content(self, chunk_size):
        yield self.content


class SnapshotTests(unittest.TestCase):
    def test_package_index_returns_filename_and_sha256(self):
        package = b"package contents"
        index = gzip.compress(
            (
                "Package: sample\nArchitecture: amd64\n"
                "Filename: pool/main/s/sample/sample_1_amd64.deb\n"
                f"SHA256: {hashlib.sha256(package).hexdigest()}\n"
            ).encode()
        )
        with patch.object(snapshot.requests, "get", return_value=Response(index)):
            files = snapshot.package_filenames(
                "https://mirror.invalid/debian", "testing", "main", "amd64", {"sample"}
            )
        self.assertEqual(files["sample"], (
            "pool/main/s/sample/sample_1_amd64.deb", hashlib.sha256(package).hexdigest()
        ))

    def test_package_index_falls_back_to_xz(self):
        package = b"xz indexed package"
        index = lzma.compress(
            (
                "Package: sample\nArchitecture: amd64\n"
                "Filename: pool/main/s/sample/sample_1_amd64.deb\n"
                f"SHA256: {hashlib.sha256(package).hexdigest()}\n"
            ).encode()
        )

        class MissingIndex(Response):
            status_code = 404

        with patch.object(
            snapshot.requests, "get", side_effect=(MissingIndex(b""), Response(index))
        ) as get:
            files = snapshot.package_filenames(
                "https://mirror.invalid/debian", "testing", "main", "amd64", {"sample"}
            )
        self.assertEqual(get.call_count, 2)
        self.assertEqual(files["sample"][1], hashlib.sha256(package).hexdigest())

    def test_package_index_rejects_missing_sha256(self):
        index = gzip.compress(
            b"Package: sample\nArchitecture: amd64\nFilename: pool/sample.deb\n"
        )
        with patch.object(snapshot.requests, "get", return_value=Response(index)):
            with self.assertRaisesRegex(ValueError, "valid SHA256"):
                snapshot.package_filenames(
                    "https://mirror.invalid/debian", "testing", "main", "amd64", {"sample"}
                )

    def test_package_index_rejects_unsafe_filename(self):
        index = gzip.compress(
            (
                "Package: sample\nArchitecture: amd64\n"
                "Filename: https://example.invalid/sample.deb\n"
                f"SHA256: {'a' * 64}\n"
            ).encode()
        )
        with patch.object(snapshot.requests, "get", return_value=Response(index)):
            with self.assertRaisesRegex(ValueError, "Unsafe package filename"):
                snapshot.package_filenames(
                    "https://mirror.invalid/debian", "testing", "main", "amd64", {"sample"}
                )

    def test_download_verifies_hash_and_replaces_destination(self):
        package = b"verified package"
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "sample.deb"
            with patch.object(snapshot.requests, "get", return_value=Response(package)):
                snapshot.download_file(
                    "https://mirror.invalid/sample.deb",
                    destination,
                    hashlib.sha256(package).hexdigest(),
                )
            self.assertEqual(destination.read_bytes(), package)
            self.assertEqual(destination.stat().st_mode & 0o777, 0o644)

    def test_download_hash_failure_preserves_existing_file(self):
        package = b"tampered package"
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "sample.deb"
            destination.write_bytes(b"existing package")
            with patch.object(snapshot.requests, "get", return_value=Response(package)):
                with self.assertRaisesRegex(ValueError, "SHA256 mismatch"):
                    snapshot.download_file(
                        "https://mirror.invalid/sample.deb", destination, "0" * 64
                    )
            self.assertEqual(destination.read_bytes(), b"existing package")
            self.assertEqual(list(Path(directory).glob("*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
