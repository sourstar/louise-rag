from pathlib import Path

import pytest

from tests_campaign.client import BASE

HERE = Path(__file__).parent


def pytest_collection_modifyitems(config, items):
    """Without LOUISE_URL the campaign is skipped — its own items only: this
    hook sees every item of the session, the unit suite's too."""
    if BASE:
        return
    skip = pytest.mark.skip(reason="LOUISE_URL is not set: the campaign runs against a live node only")
    for item in items:
        if HERE in Path(item.path).parents:
            item.add_marker(skip)
