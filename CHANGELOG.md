# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.0] - 2026-07-10

### Added
- Packaged as `azure-pim-cli` on PyPI; install with `pip install azure-pim-cli`.
- Console script `pim-activate` (replaces `python activate_pim.py`).
- `python -m azure_pim_cli` entry point.
- GitHub Actions CI: lint (ruff), type-check (mypy), unit tests (pytest), build artifact.
- GitHub Actions release workflow: publish to PyPI via Trusted Publisher (OIDC) on `v*` tags.
- Apache-2.0 license.
- Unit tests for `cache`, `cli` argument parsing, `graph_client`, and `chrome_launcher`.

### Removed
- `azrbac_client.py` — unused by the main CLI; removed to reduce surface area.
- `sniff_portal.py` — dev/debug tool, not part of the public package.
- `requirements.txt` — superseded by `pyproject.toml` dependencies.

### Known gaps
- `token_grabber.py` and `acrs_primer.py` lack unit tests (require Playwright fixtures; deferred).
