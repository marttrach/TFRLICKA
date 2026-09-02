from importlib.metadata import requires


def test_playwright_is_only_in_browser_extra() -> None:
    dependencies = requires("tra-sniper") or []
    playwright = [dependency for dependency in dependencies if dependency.startswith("playwright")]
    assert len(playwright) == 1
    assert "extra == 'browser'" in playwright[0]
