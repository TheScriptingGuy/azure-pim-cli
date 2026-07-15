from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import azure_pim_cli.chrome_launcher as launcher


class TestPortAlive:
    def test_returns_true_on_200(self) -> None:
        mock_resp = MagicMock()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_resp.status = 200
        with patch("urllib.request.urlopen", return_value=mock_resp):
            assert launcher._port_alive(9222) is True

    def test_returns_false_on_connection_error(self) -> None:
        import urllib.error

        with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("refused")):
            assert launcher._port_alive(9222) is False


class TestKillChrome:
    def test_calls_taskkill(self) -> None:
        with patch("subprocess.run") as mock_run:
            launcher._kill_chrome()
            mock_run.assert_called_once_with(
                ["taskkill", "/F", "/IM", "chrome.exe"], capture_output=True
            )


class TestChromeExe:
    def test_finds_first_existing_candidate(self, tmp_path: Path) -> None:
        fake_exe = tmp_path / "chrome.exe"
        fake_exe.touch()
        with patch("os.path.isfile", side_effect=lambda p: str(p) == str(fake_exe)):
            with patch.object(launcher, "_chrome_exe", wraps=launcher._chrome_exe):
                # Patch candidates directly
                with patch(
                    "azure_pim_cli.chrome_launcher._chrome_exe",
                    return_value=str(fake_exe),
                ):
                    assert launcher._chrome_exe() == str(fake_exe)

    def test_raises_when_not_found(self) -> None:
        with patch("os.path.isfile", return_value=False):
            with pytest.raises(RuntimeError, match="chrome.exe not found"):
                launcher._chrome_exe()


class TestLaunchDebugChrome:
    def test_fast_path_when_port_alive(self) -> None:
        with patch.object(launcher, "_port_alive", return_value=True):
            endpoint = launcher.launch_debug_chrome(port=9222)
        assert endpoint == "http://localhost:9222"

    def test_launches_chrome_and_waits(self, tmp_path: Path) -> None:
        fake_exe = tmp_path / "chrome.exe"
        fake_exe.touch()
        copy_profile = tmp_path / "profile_copy"
        source_profile = tmp_path / "source"
        source_profile.mkdir()

        port_responses = [False, True]  # not alive → launch → alive

        with (
            patch.object(launcher, "_port_alive", side_effect=port_responses),
            patch.object(launcher, "_kill_chrome"),
            patch.object(launcher, "_copy_profile"),
            patch.object(launcher, "_chrome_exe", return_value=str(fake_exe)),
            patch.object(launcher, "_wait_ready", return_value=True),
            patch("subprocess.Popen") as mock_popen,
        ):
            endpoint = launcher.launch_debug_chrome(
                port=9222,
                copy_profile=copy_profile,
                source_profile=source_profile,
            )

        assert endpoint == "http://localhost:9222"
        mock_popen.assert_called_once()
        popen_args = mock_popen.call_args[0][0]
        assert str(fake_exe) in popen_args
        assert "--remote-debugging-port=9222" in popen_args

    def test_detached_process_flag_on_nt(self, tmp_path: Path) -> None:
        fake_exe = tmp_path / "chrome.exe"
        fake_exe.touch()
        source = tmp_path / "src"
        source.mkdir()
        dst = tmp_path / "dst"

        with (
            patch.object(launcher, "_port_alive", side_effect=[False, True]),
            patch.object(launcher, "_kill_chrome"),
            patch.object(launcher, "_copy_profile"),
            patch.object(launcher, "_chrome_exe", return_value=str(fake_exe)),
            patch.object(launcher, "_wait_ready", return_value=True),
            patch("subprocess.Popen") as mock_popen,
            patch("os.name", "nt"),
        ):
            launcher.launch_debug_chrome(port=9222, copy_profile=dst, source_profile=source)

        kwargs = mock_popen.call_args[1]
        assert kwargs.get("creationflags") == 0x00000008

    def test_raises_on_port_not_ready(self, tmp_path: Path) -> None:
        fake_exe = tmp_path / "chrome.exe"
        fake_exe.touch()
        source = tmp_path / "src"
        source.mkdir()
        dst = tmp_path / "dst"

        with (
            patch.object(launcher, "_port_alive", return_value=False),
            patch.object(launcher, "_kill_chrome"),
            patch.object(launcher, "_copy_profile"),
            patch.object(launcher, "_chrome_exe", return_value=str(fake_exe)),
            patch.object(launcher, "_wait_ready", return_value=False),
            patch("subprocess.Popen"),
            pytest.raises(RuntimeError, match="not responding"),
        ):
            launcher.launch_debug_chrome(port=9222, copy_profile=dst, source_profile=source)
