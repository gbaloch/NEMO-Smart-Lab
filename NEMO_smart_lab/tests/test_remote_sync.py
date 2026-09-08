import unittest
from unittest.mock import MagicMock, patch

from NEMO_smart_lab.models import RemoteSyncEndpoint
from NEMO_smart_lab.remote_sync import RemoteSyncError, sync_tool_from_remote


def _endpoint(**overrides):
    fields = dict(
        name="Oak",
        host="dtn.oak.stanford.edu",
        port=22,
        username="jdoe",
        ssh_key_path=r"C:\keys\oak_id_ed25519",
        base_path="/oak/stanford/orgs/nano",
    )
    fields.update(overrides)
    return RemoteSyncEndpoint(**fields)


class ExtraSshOptionPairsTests(unittest.TestCase):
    def test_space_separated_line(self):
        endpoint = _endpoint(extra_ssh_options="ControlPath ~/.ssh/%r@%h:%p")
        self.assertEqual(endpoint.extra_ssh_option_pairs(), [("ControlPath", "~/.ssh/%r@%h:%p")])

    def test_equals_separated_line(self):
        endpoint = _endpoint(extra_ssh_options="ControlPersist=yes")
        self.assertEqual(endpoint.extra_ssh_option_pairs(), [("ControlPersist", "yes")])

    def test_multiple_lines_and_blank_lines_ignored(self):
        endpoint = _endpoint(extra_ssh_options="ControlPath ~/.ssh/%r@%h:%p\n\nControlPersist yes\n")
        self.assertEqual(
            endpoint.extra_ssh_option_pairs(),
            [("ControlPath", "~/.ssh/%r@%h:%p"), ("ControlPersist", "yes")],
        )

    def test_empty_is_no_pairs(self):
        self.assertEqual(_endpoint().extra_ssh_option_pairs(), [])


class SyncTests(unittest.TestCase):
    def test_raises_without_username_or_key(self):
        endpoint = _endpoint(username="", ssh_key_path="")
        with self.assertRaises(RemoteSyncError):
            sync_tool_from_remote(r"C:\data\fiji1", endpoint, "fiji1")

    @patch("NEMO_smart_lab.remote_sync.os.makedirs")
    @patch("NEMO_smart_lab.remote_sync.shutil.which")
    @patch("NEMO_smart_lab.remote_sync.subprocess.run")
    def test_prefers_rsync_when_available(self, mock_run, mock_which, mock_makedirs):
        mock_which.side_effect = lambda name: f"/usr/bin/{name}" if name == "rsync" else None
        mock_run.return_value = MagicMock(returncode=0, stdout="sent 1 file", stderr="")

        sync_tool_from_remote(r"C:\data\fiji1", _endpoint(), "fiji-actual-name")

        cmd = mock_run.call_args[0][0]
        self.assertEqual(cmd[0], "rsync")
        self.assertIn("jdoe@dtn.oak.stanford.edu:/oak/stanford/orgs/nano/fiji-actual-name/", cmd)
        self.assertTrue(any("oak_id_ed25519" in part for part in cmd))

    @patch("NEMO_smart_lab.remote_sync.os.makedirs")
    @patch("NEMO_smart_lab.remote_sync.shutil.which")
    @patch("NEMO_smart_lab.remote_sync.subprocess.run")
    def test_falls_back_to_scp_without_rsync(self, mock_run, mock_which, mock_makedirs):
        mock_which.side_effect = lambda name: "/usr/bin/scp" if name == "scp" else None
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

        sync_tool_from_remote(r"C:\data\fiji1", _endpoint(), "fiji1")

        cmd = mock_run.call_args[0][0]
        self.assertEqual(cmd[0], "scp")
        self.assertIn("jdoe@dtn.oak.stanford.edu:/oak/stanford/orgs/nano/fiji1/*", cmd)

    @patch("NEMO_smart_lab.remote_sync.os.makedirs")
    @patch("NEMO_smart_lab.remote_sync.shutil.which")
    def test_raises_when_no_transfer_tool_is_available(self, mock_which, mock_makedirs):
        mock_which.return_value = None
        with self.assertRaises(RemoteSyncError):
            sync_tool_from_remote(r"C:\data\fiji1", _endpoint(), "fiji1")

    @patch("NEMO_smart_lab.remote_sync.os.makedirs")
    @patch("NEMO_smart_lab.remote_sync.shutil.which")
    @patch("NEMO_smart_lab.remote_sync.subprocess.run")
    def test_nonzero_exit_raises_with_stderr(self, mock_run, mock_which, mock_makedirs):
        mock_which.side_effect = lambda name: "/usr/bin/rsync" if name == "rsync" else None
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="Permission denied (publickey).")

        with self.assertRaises(RemoteSyncError) as ctx:
            sync_tool_from_remote(r"C:\data\fiji1", _endpoint(), "fiji1")
        self.assertIn("Permission denied", str(ctx.exception))

    @patch("NEMO_smart_lab.remote_sync.os.makedirs")
    @patch("NEMO_smart_lab.remote_sync.shutil.which")
    @patch("NEMO_smart_lab.remote_sync.subprocess.run")
    def test_extra_ssh_opts_reach_the_rsync_dash_e_string(self, mock_run, mock_which, mock_makedirs):
        mock_which.side_effect = lambda name: "/usr/bin/rsync" if name == "rsync" else None
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

        endpoint = _endpoint(extra_ssh_options="ControlPath ~/.ssh/%r@%h:%p")
        sync_tool_from_remote(r"C:\data\fiji1", endpoint, "fiji1")

        cmd = mock_run.call_args[0][0]
        ssh_arg = cmd[cmd.index("-e") + 1]
        self.assertIn("ControlPath=~/.ssh/%r@%h:%p", ssh_arg)

    @patch("NEMO_smart_lab.remote_sync.os.makedirs")
    @patch("NEMO_smart_lab.remote_sync.shutil.which")
    @patch("NEMO_smart_lab.remote_sync.subprocess.run")
    def test_different_endpoints_use_their_own_host_and_base_path(self, mock_run, mock_which, mock_makedirs):
        mock_which.side_effect = lambda name: "/usr/bin/rsync" if name == "rsync" else None
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

        endpoint = _endpoint(
            name="Building 34 fileserver",
            host="filer34.example.org",
            username="labops",
            base_path="/srv/tool-logs",
        )
        sync_tool_from_remote(r"C:\data\hdpcvd", endpoint, "hdpcvd")

        cmd = mock_run.call_args[0][0]
        self.assertIn("labops@filer34.example.org:/srv/tool-logs/hdpcvd/", cmd)


if __name__ == "__main__":
    unittest.main()
