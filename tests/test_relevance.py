from app.services.relevance import score_relevance, tokenize


def test_tokenize_lowercases_and_splits_on_url_separators():
    assert tokenize("BGP-Routing_Guide.pdf?ver=2&lang=ru") == {"bgp", "routing", "guide", "pdf", "ver", "lang", "ru"}


def test_tokenize_is_unicode_aware_for_cyrillic():
    assert tokenize("Маршрутизация BGP") == {"маршрутизация", "bgp"}


def test_tokenize_drops_single_char_tokens():
    assert "a" not in tokenize("a bgp guide")
    assert "bgp" in tokenize("a bgp guide")


def test_relevance_high_when_all_terms_in_url():
    r = score_relevance("bgp routing", "https://example.com/bgp-routing-guide.pdf")
    assert r.tier == "high"
    assert r.score == 1.0


def test_relevance_possible_when_only_snippet_matches():
    r = score_relevance(
        "bgp routing",
        "https://example.com/networking/article-42",
        title="Networking basics",
        snippet="An introduction to bgp concepts",
    )
    assert r.tier == "possible"
    assert 0 < r.score < 0.6


def test_relevance_discovered_when_nothing_matches():
    r = score_relevance("bgp routing", "https://example.com/about-us", title="About the team", snippet="Who we are")
    assert r.tier == "discovered"
    assert r.score == 0.0


def test_relevance_url_match_outweighs_snippet_match():
    url_hit = score_relevance("bgp", "https://example.com/bgp", title=None, snippet=None)
    snippet_hit = score_relevance("bgp", "https://example.com/other", title=None, snippet="about bgp")
    assert url_hit.score > snippet_hit.score


def test_relevance_empty_query_is_discovered_not_crash():
    r = score_relevance("", "https://example.com/")
    assert r.tier == "discovered"
    assert r.score == 0.0


def test_relevance_never_errors_on_missing_title_snippet():
    r = score_relevance("bgp", "https://example.com/bgp")
    assert r.tier == "high"
