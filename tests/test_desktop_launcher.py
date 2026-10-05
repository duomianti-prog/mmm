from pathlib import Path
import os
import socket
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
import zipfile
from contextlib import closing

from desktop.launcher import prepare_home, user_directory, snapshot, bind_local_port, child_command, InstanceLock


class DesktopTests(unittest.TestCase):
    def test_custom_home_is_explicit_and_separate(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"CREATORHUB_DESKTOP_HOME": tmp}):
            self.assertEqual(user_directory(), Path(tmp).resolve())

    def test_legacy_creatorhub_directory_is_migrated_to_mmm(self):
        # 品牌改名：首次启动时把旧 CreatorHub 用户数据整体搬到 mmm
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            legacy = base / "CreatorHub" / "user-data"
            legacy.mkdir(parents=True)
            (legacy / "license.dat").write_bytes(b"keep-license")
            (legacy / "config.yaml").write_text("platform: douyin\n", encoding="utf-8")
            env = {"LOCALAPPDATA": tmp}
            for key in ("MMM_DESKTOP_HOME", "CREATORHUB_DESKTOP_HOME"):
                env[key] = ""
            with patch.dict(os.environ, env, clear=False):
                home = user_directory()
            self.assertEqual(home, (base / "mmm" / "user-data").resolve())
            self.assertEqual((home / "license.dat").read_bytes(), b"keep-license")
            self.assertEqual((home / "config.yaml").read_text(encoding="utf-8"),
                             "platform: douyin\n")

    def test_existing_mmm_directory_is_kept_untouched(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            legacy = base / "CreatorHub" / "user-data"
            legacy.mkdir(parents=True)
            (legacy / "old-only.txt").write_text("legacy", encoding="utf-8")
            new = base / "mmm" / "user-data"
            new.mkdir(parents=True)
            (new / "current.txt").write_text("current", encoding="utf-8")
            env = {"LOCALAPPDATA": tmp}
            for key in ("MMM_DESKTOP_HOME", "CREATORHUB_DESKTOP_HOME"):
                env[key] = ""
            with patch.dict(os.environ, env, clear=False):
                home = user_directory()
            self.assertEqual(home, new.resolve())
            self.assertTrue((home / "current.txt").exists())
            self.assertFalse((home / "old-only.txt").exists())
            self.assertTrue((legacy / "old-only.txt").exists())

    def test_prepare_preserves_config_and_existing_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            prepare_home(home)
            (home / "config.yaml").write_text("custom: true", encoding="utf-8")
            (home / "private.db").write_bytes(b"existing data")
            prepare_home(home)
            self.assertEqual((home / "config.yaml").read_text(), "custom: true")
            self.assertEqual((home / "private.db").read_bytes(), b"existing data")

    def test_snapshot_contains_consistent_database_and_no_logs(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            prepare_home(home)
            (home / "config.yaml").write_text("storage:\n  db_path: custom.db\n", encoding="utf-8")
            with closing(sqlite3.connect(home / "custom.db")) as db:
                db.execute("create table example (value text)")
                db.execute("insert into example values ('keep me')")
                db.commit()
            (home / "logs" / "private.txt").write_text("SECRET")
            with zipfile.ZipFile(snapshot(home)) as archive:
                self.assertEqual(set(archive.namelist()), {"config.yaml", "database.db", "README.txt"})
                archive.extract("database.db", home / "restore")
            with closing(sqlite3.connect(home / "restore" / "database.db")) as db:
                self.assertEqual(db.execute("select value from example").fetchone()[0], "keep me")

    def test_port_falls_back_without_reusing_another_server(self):
        with socket.socket() as occupied:
            occupied.bind(("127.0.0.1", 0))
            occupied.listen()
            with bind_local_port(occupied.getsockname()[1]) as chosen:
                self.assertNotEqual(chosen.getsockname()[1], occupied.getsockname()[1])
                self.assertEqual(chosen.getsockname()[0], "127.0.0.1")

    def test_frozen_command_is_not_python_module_invocation(self):
        with patch("sys.frozen", True, create=True), patch("sys.executable", "mmm.exe"):
            self.assertEqual(child_command("--serve"), ["mmm.exe", "--serve"])

    @unittest.skipUnless(os.name == "nt", "Windows locking")
    def test_single_instance_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            prepare_home(home)
            first = InstanceLock(home)
            try:
                with self.assertRaises(RuntimeError):
                    InstanceLock(home)
            finally:
                first.close()
            second = InstanceLock(home)
            second.close()


if __name__ == "__main__":
    unittest.main()
