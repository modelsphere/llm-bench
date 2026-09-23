"""The optional `source_url` on a submission: what it accepts and what it refuses.

`source_url` is a link back to the system that produced a submission — an LLM
AutoTune run page, a CI job, whatever the submitter runs. A leaderboard row is
otherwise three ids and a score, with no way back to the engine configuration
behind it.

One property matters and is asserted here: the value is **opaque but not
arbitrary**. We never parse or rewrite it, so it stays useful to any submitter
— but it is rendered as an href, so a non-http(s) scheme and an over-long
string are refused at submit rather than stored and sanitised later at every
render site.

Pure/offline: the Pydantic schema alone, no DB.
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.schemas.benchmarks import SOURCE_URL_MAX, SubmissionCreate


def _create(**kwargs) -> SubmissionCreate:
    return SubmissionCreate(
        endpoint_url="https://api.example.com",
        model="m",
        api_key="k",
        **kwargs,
    )


# --- validation -------------------------------------------------------------


def test_source_url_defaults_to_none():
    assert _create().source_url is None


def test_absent_and_empty_both_mean_no_link():
    # Neither is an error — a submitting system that always sends the field can
    # send "" for "no link" without special-casing it.
    assert _create(source_url=None).source_url is None
    assert _create(source_url="").source_url is None
    assert _create(source_url="   ").source_url is None


@pytest.mark.parametrize(
    "url",
    [
        "https://autotune.example.com/baselines/57",
        "https://hub.example.com/entry/57?tab=sweep#gate",
        "HTTPS://EXAMPLE.COM/x",  # scheme is case-insensitive
    ],
)
def test_http_and_https_accepted(url):
    assert _create(source_url=url).source_url == url.strip()


def test_value_is_stored_verbatim():
    # Opaque: no normalisation, no trailing-slash tidying, no re-encoding — the
    # submitting platform generated this URL for itself and owns its shape.
    url = "https://hub.example.com/a//b?q=%20&x=1#f"
    assert _create(source_url=url).source_url == url


def test_surrounding_whitespace_is_trimmed():
    s = _create(source_url="  https://hub.example.com/57  ")
    assert s.source_url == "https://hub.example.com/57"


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        "  javascript:alert(1)  ",   # trimmed first, then still refused
        "data:text/html;base64,PHNjcmlwdD4=",
        "file:///etc/passwd",
        "//example.com/x",           # protocol-relative
        "/baseline-hub/57",          # bare path
        "hub.example.com/57",        # no scheme at all
        "ftp://example.com/x",
    ],
)
def test_non_http_schemes_rejected(url):
    with pytest.raises(ValidationError, match="source_url"):
        _create(source_url=url)


def test_url_at_cap_accepted():
    url = "https://h.example.com/" + "x" * (SOURCE_URL_MAX - len("https://h.example.com/"))
    assert len(url) == SOURCE_URL_MAX
    assert _create(source_url=url).source_url == url


def test_over_long_url_rejected():
    with pytest.raises(ValidationError, match="source_url"):
        _create(source_url="https://h.example.com/" + "x" * SOURCE_URL_MAX)


def test_cap_applies_after_trim():
    # Padding around an at-cap URL must not trip the cap.
    url = "https://h.example.com/" + "x" * (SOURCE_URL_MAX - len("https://h.example.com/"))
    assert _create(source_url=f"  {url}  ").source_url == url
