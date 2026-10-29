import base64
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import gswap


class GSwapTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        gacct_dir = self.home / ".config" / "google-accounts"
        self.env_patch = patch.dict(
            os.environ,
            {
                "GSWAP_DIR": str(self.home / ".local" / "share" / "gswap"),
                "AGY_SWAP_DIR": "",
                "GACCT_DIR": str(gacct_dir),
                "GACCT_SETTINGS": str(gacct_dir / "settings.json"),
                "GACCT_REGISTRY": str(gacct_dir / "accounts.json"),
                "AGYSWAP_CLIENT_ID": gswap.DEFAULT_CLIENT_ID,
                "AGYSWAP_CLIENT_SECRET": "GOCSPX-TestDummySecret1234567890123",
                "AGYSWAP_SKIP_KEYRING": "1",
                "GSWAP_SIMPLE_MENU": "1",
                "GSWAP_LANG": "",
                "LC_ALL": "C",
                "LC_MESSAGES": "C",
                "LANGUAGE": "",
                "LANG": "C",
            },
        )
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)
        gswap.set_lang(None)
        gswap._RAW_KEY_PENDING.clear()
        self.addCleanup(lambda: gswap.set_lang(None))

    def sample_token(self, refresh: str, access: str = "ya29.test", email: str | None = None):
        doc = {
            "token": access,
            "refresh_token": refresh,
            "token_uri": gswap.TOKEN_URI,
            "client_id": gswap.DEFAULT_CLIENT_ID,
            "client_secret": "GOCSPX-TestDummySecret1234567890123",
            "scopes": list(gswap.DEFAULT_SCOPES),
            "expiry": "2099-01-01T00:00:00Z",
        }
        if email:
            doc["email"] = email
        return doc

    def test_parse_json_and_base64_and_antigravity_tools_nested(self):
        raw = self.sample_token("1//refresh-a", email="alice@example.com")
        parsed1 = gswap.parse_token_blob(json.dumps(raw))
        self.assertEqual(parsed1["refresh_token"], "1//refresh-a")
        self.assertEqual(parsed1["email"], "alice@example.com")

        b64 = base64.b64encode(json.dumps(raw).encode("utf-8")).decode("ascii")
        parsed2 = gswap.parse_token_blob(b64)
        self.assertEqual(parsed2["refresh_token"], "1//refresh-a")

        nested = {
            "email": "bob@example.com",
            "token": {
                "access_token": "ya29.nested",
                "refresh_token": "1//refresh-b",
            },
        }
        parsed3 = gswap.parse_token_blob(nested)
        self.assertEqual(parsed3["email"], "bob@example.com")
        self.assertEqual(parsed3["token"], "ya29.nested")
        self.assertEqual(parsed3["refresh_token"], "1//refresh-b")

    def test_upsert_and_switch_rotates_and_updates_markers(self):
        t1 = self.sample_token("1//r1", "ya29.a1", "alice@example.com")
        t2 = self.sample_token("1//r2", "ya29.a2", "bob@example.com")

        n1, e1, new1 = gswap.upsert_account(t1, home=self.home, make_active=True)
        n2, e2, new2 = gswap.upsert_account(t2, home=self.home, make_active=False)
        self.assertEqual((n1, e1, new1), (1, "alice@example.com", True))
        self.assertEqual((n2, e2, new2), (2, "bob@example.com", True))

        gswap.sync_live_stores(t1, home=self.home)
        self.assertTrue(gswap.cli_token_path(self.home).is_file())
        self.assertEqual(gswap.cli_token_path(self.home).stat().st_mode & 0o777, 0o400)
        self.assertTrue(gswap.standalone_token_path(self.home).is_file())
        self.assertEqual(gswap.standalone_token_path(self.home).stat().st_mode & 0o777, 0o400)
        self.assertTrue(gswap.cli_marker_path(self.home).is_file())
        self.assertTrue(gswap.standalone_marker_path(self.home).is_file())

        # Rotate with no target -> switches from 1 to 2
        res = gswap.switch_to(None, home=self.home)
        self.assertEqual(res["slot"], 2)
        self.assertEqual(res["email"], "bob@example.com")
        live = json.loads(gswap.cli_token_path(self.home).read_text(encoding="utf-8"))
        self.assertEqual(live["token"]["refresh_token"], "1//r2")
        self.assertEqual(live["token"]["token_type"], "Bearer")
        self.assertEqual(live["auth_method"], "consumer")
        self.assertEqual(live["id_token_email"], "bob@example.com")

        # Rotate again -> back to 1
        res2 = gswap.switch_to(None, home=self.home)
        self.assertEqual(res2["slot"], 1)
        self.assertEqual(res2["email"], "alice@example.com")

        # Switch by alias / email prefix
        res3 = gswap.switch_to("bob", home=self.home)
        self.assertEqual(res3["slot"], 2)

    def test_live_refreshed_token_saved_back_before_switch(self):
        t1 = self.sample_token("1//r1", "ya29.old", "alice@example.com")
        t2 = self.sample_token("1//r2", "ya29.two", "bob@example.com")
        gswap.upsert_account(t1, home=self.home, make_active=True)
        gswap.upsert_account(t2, home=self.home, make_active=False)
        gswap.sync_live_stores(t1, home=self.home)

        # Simulate agy CLI refreshing the live token file in-place
        refreshed = dict(t1)
        refreshed["token"] = "ya29.new-from-cli"
        gswap.atomic_write_json(gswap.cli_token_path(self.home), gswap.clean_cli_token_payload(refreshed, self.home))

        gswap.switch_to("2", home=self.home)
        saved_slot_1 = gswap.load_slot_token(1, "alice@example.com", home=self.home)
        self.assertEqual(saved_slot_1["token"], "ya29.new-from-cli")

    def test_remove_slot_updates_sequence(self):
        t1 = self.sample_token("1//r1", "ya29.a1", "alice@example.com")
        t2 = self.sample_token("1//r2", "ya29.a2", "bob@example.com")
        gswap.upsert_account(t1, home=self.home, make_active=True)
        gswap.upsert_account(t2, home=self.home, make_active=False)

        removed = gswap.remove_slot("alice", home=self.home)
        self.assertEqual(removed["removed_slot"], 1)
        seq = gswap.load_sequence(self.home)
        self.assertEqual(seq["sequence"], [2])
        self.assertEqual(seq["activeAccountNumber"], 2)

    def test_unified_gacct_registry_and_gcloud_switch(self):
        reg_path = gswap.gacct_registry_path(self.home)
        gcloud_dir = self.home / ".config" / "gcloud-test"
        train_pool_file = self.home / ".config" / "train-pool" / "colab_accounts.json"
        gswap.atomic_write_json(
            gswap.gacct_settings_path(self.home),
            {"train_pool_registry": str(train_pool_file)},
        )
        gswap.atomic_write_json(
            reg_path,
            [
                {
                    "id": "default",
                    "email": "primary@example.com",
                    "colab": {
                        "adc": str(self.home / "adc-default.json"),
                        "sessions": str(self.home / "sessions-default.json"),
                        "max_per_gpu": 3,
                        "train_pool": True,
                    },
                    "drive": {"rclone_config": str(self.home / "rclone.conf"), "remote": "gd-main"},
                    "gcloud": {
                        "config_dir": str(gcloud_dir),
                        "configuration": "main-cfg",
                        "project": "demo-project-1",
                    },
                },
                {
                    "id": "second",
                    "email": "alice@example.com",
                    "colab": {
                        "adc": str(self.home / "adc-second.json"),
                        "sessions": str(self.home / "sessions-second.json"),
                        "max_per_gpu": 2,
                        "train_pool": True,
                    },
                    "drive": {"rclone_config": str(self.home / "rclone.conf"), "remote": "gd-second"},
                    "gcloud": {
                        "config_dir": str(gcloud_dir),
                        "configuration": "second-cfg",
                        "project": None,
                    },
                },
            ],
        )

        t1 = self.sample_token("1//r1", "ya29.a1", "alice@example.com")
        t2 = self.sample_token("1//r2", "ya29.a2", "bob@example.com")
        gswap.upsert_account(t1, home=self.home, make_active=False)
        gswap.upsert_account(t2, home=self.home, make_active=True)

        merged = gswap.load_gacct_registry(home=self.home, sync_back=True)
        self.assertEqual(len(merged), 3)
        self.assertIsNone(merged[0].get("agy"))
        self.assertEqual(merged[1]["agy"]["slot"], 1)
        self.assertEqual(merged[2]["id"], "bob")
        self.assertEqual(merged[2]["agy"]["slot"], 2)

        # Switch both agy and gcloud to "second" (alice@example.com)
        rc = gswap.use_unified_account("second", "all", home=self.home)
        self.assertEqual(rc, 0)
        self.assertEqual(gswap.detect_active_gcloud_config(merged, home=self.home), "second-cfg")
        self.assertEqual(gswap.detect_active_slot(gswap.load_sequence(self.home), home=self.home), "1")

        # Sync train pool
        gswap.sync_trainpool(home=self.home)
        pool_rows = json.loads(train_pool_file.read_text(encoding="utf-8"))
        self.assertEqual([r["id"] for r in pool_rows], ["default", "second"])
        self.assertEqual(pool_rows[1]["max_per_gpu"], 2)

    def test_interactive_menu_rotates_and_exits(self):
        t1 = self.sample_token("1//r1", "ya29.a1", "alice@example.com")
        t2 = self.sample_token("1//r2", "ya29.a2", "bob@example.com")
        gswap.upsert_account(t1, home=self.home, make_active=True)
        gswap.upsert_account(t2, home=self.home, make_active=False)
        gswap.sync_live_stores(t1, home=self.home)

        # Choose "1" (rotate next AGY account -> bob), then "q" (quit)
        with patch("builtins.input", side_effect=["1", "q"]):
            rc = gswap.interactive_menu(home=self.home)
        self.assertEqual(rc, 0)
        self.assertEqual(gswap.detect_active_slot(gswap.load_sequence(self.home), home=self.home), "2")

    @unittest.skipUnless(hasattr(os, "openpty"), "POSIX pty required")
    def test_pty_arrow_keys_do_not_trigger_esc(self):
        import pty

        master_fd, slave_fd = pty.openpty()
        try:
            # 1. Standard CSI Down arrow in one packet
            os.write(master_fd, b"\x1b[B")
            self.assertEqual(gswap._read_raw_key(slave_fd), "DOWN")

            # 2. Standard CSI Up arrow split across two packets
            os.write(master_fd, b"\x1b")
            os.write(master_fd, b"[A")
            self.assertEqual(gswap._read_raw_key(slave_fd), "UP")

            # 3. Application-cursor-mode Down arrow (SS3)
            os.write(master_fd, b"\x1bOB")
            self.assertEqual(gswap._read_raw_key(slave_fd), "DOWN")

            # 4. Enter key
            os.write(master_fd, b"\r")
            self.assertEqual(gswap._read_raw_key(slave_fd), "ENTER")

            # 5. Esc followed immediately by 'q' preserves 'q' for the next read
            os.write(master_fd, b"\x1bq")
            self.assertEqual(gswap._read_raw_key(slave_fd), "ESC")
            self.assertEqual(gswap._read_raw_key(slave_fd), "q")

            # 6. Full select_from_menu with Down arrow + Down arrow + Enter -> selects 3rd option ("3")
            with patch.dict(os.environ, {"GSWAP_SIMPLE_MENU": "0"}), patch(
                "sys.stdin.isatty", return_value=True
            ), patch("sys.stdout.isatty", return_value=True), patch(
                "sys.stdin.fileno", return_value=slave_fd
            ):
                os.write(master_fd, b"\x1b[B\x1b[B\r")
                chosen = gswap.select_from_menu(
                    "Test Menu",
                    [("1", "First"), ("2", "Second"), ("3", "Third"), ("q", "Quit")],
                    home=self.home,
                )
                self.assertEqual(chosen, "3")
        finally:
            os.close(master_fd)
            os.close(slave_fd)

    def test_i18n_zh_and_en_and_menu_lang_switch(self):
        import io
        from contextlib import redirect_stdout

        t1 = self.sample_token("1//r1", "ya29.a1", "alice@example.com")
        gswap.upsert_account(t1, home=self.home, make_active=True)

        # Save language as 'zh' and verify Chinese output
        gswap.save_lang_setting("zh", home=self.home)
        self.assertEqual(gswap.get_lang(self.home), "zh")
        buf_zh = io.StringIO()
        with redirect_stdout(buf_zh):
            gswap.print_unified_status(include_quota=False, home=self.home)
        self.assertIn("[AGY激活]", buf_zh.getvalue())

        # Switch language to 'en' via interactive menu ('l' -> 'q')
        gswap.set_lang(None)
        with patch("builtins.input", side_effect=["l", "q"]):
            rc = gswap.interactive_menu(home=self.home)
        self.assertEqual(rc, 0)
        self.assertEqual(gswap.get_lang(self.home), "en")

        buf_en = io.StringIO()
        with redirect_stdout(buf_en):
            gswap.print_unified_status(include_quota=False, home=self.home)
        self.assertIn("[AGY Active]", buf_en.getvalue())

    def test_codex_review_edge_cases(self):
        # 1. _strip_lang_flags preserves downstream -L for rclone/drive and rejects invalid --lang
        rest = gswap._strip_lang_flags(["-L", "en", "second", "drive", "copy", "-L", ":src", ":dst"])
        self.assertEqual(gswap.get_lang(self.home), "en")
        self.assertEqual(rest, ["second", "drive", "copy", "-L", ":src", ":dst"])
        with self.assertRaises(gswap.AgySwapError):
            gswap._strip_lang_flags(["--lang", "fr"])

        # 2. save_lang_setting refuses to overwrite malformed settings.json
        spath = gswap.gacct_settings_path(self.home)
        spath.parent.mkdir(parents=True, exist_ok=True)
        spath.write_text("{corrupt-json", encoding="utf-8")
        with self.assertRaises(gswap.AgySwapError):
            gswap.save_lang_setting("zh", home=self.home)
        self.assertEqual(spath.read_text(encoding="utf-8"), "{corrupt-json")
        spath.unlink()

        # 3. remove_slot syncs new active account to live store; unmanaged live token returns None
        t1 = self.sample_token("1//r1", "ya29.a1", "alice@example.com")
        t2 = self.sample_token("1//r2", "ya29.a2", "bob@example.com")
        gswap.upsert_account(t1, home=self.home, make_active=True)
        gswap.upsert_account(t2, home=self.home, make_active=False)
        gswap.sync_live_stores(t1, home=self.home)
        gswap.remove_slot("1", home=self.home)
        live = gswap.read_live_cli_token(self.home)
        self.assertEqual(live["email"], "bob@example.com")

        # Write an unmanaged live token on disk -> detect_active_slot returns None
        t_other = self.sample_token("1//r3", "ya29.a3", "stranger@example.com")
        gswap.sync_live_stores(t_other, home=self.home)
        self.assertIsNone(gswap.detect_active_slot(gswap.load_sequence(self.home), home=self.home))

        # 4. use_unified_account rejects invalid service or gcloud switch for AGY-only account
        with self.assertRaises(gswap.AgySwapError):
            gswap.use_unified_account("bob", "invalid-svc", home=self.home)
        with self.assertRaises(gswap.AgySwapError):
            gswap.use_unified_account(None, "gcloud", home=self.home)


if __name__ == "__main__":
    unittest.main()
