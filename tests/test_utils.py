from app.utils import classify_url, normalize_domain, normalize_url


def test_normalize_domain():
    assert normalize_domain("https://www.Example.COM/a") == "example.com"


def test_normalize_url_strips_tracking_and_fragment():
    assert normalize_url("HTTPS://Example.COM/a/?utm_source=x&b=2&a=1#frag") == "https://example.com/a?a=1&b=2"


def test_classify_url():
    assert classify_url("https://example.com/a.pdf") == "pdf"
    assert classify_url("https://example.com/a.zip") == "archive"
    assert classify_url("https://example.com/a") == "page"
