from brain.platform.browser.service import _browser_harness_short_name, _normalize_url


def test_browser_url_normalization_requires_a_target_and_preserves_explicit_schemes():
    assert _normalize_url("example.com") == "https://example.com"
    assert _normalize_url(" http://localhost:5173 ") == "http://localhost:5173"
    assert _normalize_url("https://docs.example.test/path") == "https://docs.example.test/path"

    for value in ("", "   "):
        try:
            _normalize_url(value)
        except ValueError as exc:
            assert str(exc) == "URL is required"
        else:
            raise AssertionError("blank browser URLs must fail closed")


def test_browser_harness_short_names_are_stable_safe_and_collision_resistant():
    values = [
        "session/with/slashes",
        "session with spaces",
        "session:with:punctuation",
        "",
        None,
    ]

    results = [_browser_harness_short_name(value, fallback="session") for value in values]

    assert len(set(results)) == len(results)
    for result in results:
        prefix, digest = result.rsplit("-", 1)
        assert prefix
        assert len(digest) == 12
        assert all(ch.isalnum() or ch in {"-", "_"} for ch in result)
