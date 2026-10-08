import pytest
from test_showcase import OUT


@pytest.hookimpl(tryfirst=True)
def pytest_configure(config):
    """Write `junit.xml` beside the pictures unless `--junitxml` says otherwise,
    or nothing is drawn: a `--collect-only` would leave the pictures with no verdicts."""
    if not config.option.collectonly:
        config.option.xmlpath = config.option.xmlpath or str(OUT / "junit.xml")
