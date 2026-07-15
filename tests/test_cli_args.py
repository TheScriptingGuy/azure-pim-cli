from __future__ import annotations

from azure_pim_cli.cli import _iso8601_hours, build_parser


class TestBuildParser:
    def setup_method(self) -> None:
        self.p = build_parser()

    def test_defaults(self) -> None:
        args = self.p.parse_args([])
        assert args.refresh is False
        assert args.list_only is False
        assert args.group is None
        assert args.justification is None
        assert args.hours == 8
        assert args.ticket is None
        assert args.approvals_only is False
        assert args.eligibilities_only is False
        assert args.headless is False
        assert args.keep_open is False
        assert args.token is None
        assert args.parallel == 8
        assert args.fetch_workers == 32
        assert args.cdp_endpoint is None
        assert args.auto_cdp is True
        assert args.auto_cdp_port == 9222
        assert args.refresh_chrome_profile is False

    def test_refresh_flag(self) -> None:
        args = self.p.parse_args(["--refresh"])
        assert args.refresh is True

    def test_list_only_flag(self) -> None:
        args = self.p.parse_args(["--list-only"])
        assert args.list_only is True

    def test_group_arg(self) -> None:
        args = self.p.parse_args(["--group", "^sg-"])
        assert args.group == "^sg-"

    def test_token_arg(self) -> None:
        args = self.p.parse_args(["--token", "eyJtest"])
        assert args.token == "eyJtest"

    def test_hours_override(self) -> None:
        args = self.p.parse_args(["--hours", "4"])
        assert args.hours == 4

    def test_parallel_override(self) -> None:
        args = self.p.parse_args(["--parallel", "1"])
        assert args.parallel == 1

    def test_headless_flag(self) -> None:
        args = self.p.parse_args(["--headless"])
        assert args.headless is True

    def test_no_auto_cdp(self) -> None:
        args = self.p.parse_args(["--no-auto-cdp"])
        assert args.auto_cdp is False

    def test_auto_cdp_port(self) -> None:
        args = self.p.parse_args(["--auto-cdp-port", "9333"])
        assert args.auto_cdp_port == 9333

    def test_cdp_endpoint(self) -> None:
        args = self.p.parse_args(["--cdp-endpoint", "http://localhost:9222"])
        assert args.cdp_endpoint == "http://localhost:9222"

    def test_approvals_only(self) -> None:
        args = self.p.parse_args(["--approvals-only"])
        assert args.approvals_only is True

    def test_eligibilities_only(self) -> None:
        args = self.p.parse_args(["--eligibilities-only"])
        assert args.eligibilities_only is True

    def test_combined_flags(self) -> None:
        args = self.p.parse_args(
            [
                "--group",
                "db-",
                "--justification",
                "test",
                "--hours",
                "2",
                "--parallel",
                "4",
                "--headless",
                "--no-auto-cdp",
            ]
        )
        assert args.group == "db-"
        assert args.justification == "test"
        assert args.hours == 2
        assert args.parallel == 4
        assert args.headless is True
        assert args.auto_cdp is False


class TestIso8601Hours:
    def test_hours(self) -> None:
        assert _iso8601_hours("PT8H") == 8

    def test_minutes_rounds_up(self) -> None:
        assert _iso8601_hours("PT120M") == 2

    def test_minutes_small_rounds_to_one(self) -> None:
        assert _iso8601_hours("PT30M") == 1

    def test_invalid_returns_none(self) -> None:
        assert _iso8601_hours("P1D") is None
        assert _iso8601_hours("invalid") is None
        assert _iso8601_hours("") is None

    def test_zero_minutes_rounds_to_one(self) -> None:
        assert _iso8601_hours("PT0M") == 1
