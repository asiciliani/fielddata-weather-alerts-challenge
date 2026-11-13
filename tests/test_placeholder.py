"""Placeholder so the toolchain runs before any application code exists (step 0)."""

import app


def test_package_imports() -> None:
    assert app.__doc__
