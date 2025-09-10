import os
import pytest


def pytest_collection_modifyitems(config, items):
    if os.getenv("CI"):
        skip_ci = pytest.mark.skip(reason="Skipped in CI environment")
        for item in items:
            if "skip_ci" in item.keywords:
                item.add_marker(skip_ci)


