"""Smoke tests for the package skeleton."""

import leads_discovery


def test_package_imports() -> None:
    """The package skeleton must be importable before later layers are added."""
    assert leads_discovery.__doc__
