#!/usr/bin/env python3
from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import send_simplex_report as simplex_sender


class PortableBinaryFallbackTests(unittest.TestCase):
    def test_portable_binary_is_used_when_host_simplex_depends_on_missing_openssl(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir_path = Path(tmpdir)
            report_path = tmpdir_path / "report.txt"
            report_path.write_text("simplex report body", encoding="utf-8")

            host_binary = tmpdir_path / "simplex-chat"
            host_binary.write_text("stub", encoding="utf-8")
            host_binary.chmod(0o755)

            portable_binary = tmpdir_path / "portable-simplex-chat"
            completed = subprocess.CompletedProcess(
                args=[str(portable_binary)],
                returncode=0,
                stdout="sent",
                stderr="",
            )

            with (
                mock.patch.object(simplex_sender, "_resolve_binary", return_value=str(host_binary)),
                mock.patch.object(
                    simplex_sender,
                    "_get_openssl_dependency_paths",
                    return_value=["/usr/local/opt/openssl@3.0/lib/libcrypto.3.dylib"],
                ),
                mock.patch.object(simplex_sender, "_is_x86_64_compatible_dylib", return_value=False),
                mock.patch.object(
                    simplex_sender,
                    "_ensure_portable_binary",
                    return_value=(True, str(portable_binary), f"using portable SimpleX binary at {portable_binary}"),
                ),
                mock.patch.object(simplex_sender, "_prepare_db_prefix", return_value=(True, "")),
                mock.patch.object(simplex_sender, "_seed_db_prefix", return_value=(True, "seeded")),
                mock.patch.object(simplex_sender.subprocess, "run", return_value=completed) as run_mock,
            ):
                outcome = simplex_sender.send_report(
                    report_path,
                    db_prefix="/tmp/nonexistent-simplex-prefix",
                    contact="Gaslad",
                    binary=str(host_binary),
                    retry_count=1,
                )

        self.assertTrue(outcome.ok)
        self.assertIn("portable SimpleX binary", outcome.message)
        self.assertEqual(run_mock.call_args_list[0].args[0][0], str(portable_binary))


if __name__ == "__main__":
    unittest.main()
