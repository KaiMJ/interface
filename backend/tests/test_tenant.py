"""Which institution's install a recorded URL means.

The hazard these pin is quiet rather than loud: an allowlist is a pattern that
spans tenants *by design* (that is what makes one artifact reusable), so a
recorded absolute URL from institution A passes institution B's allowlist and the
run succeeds — against A. Nothing fails; the answer is simply about the wrong
credit union's member.
"""

from __future__ import annotations

from cua.replay.tenant import rebase


def test_a_recorded_origin_is_replaced_by_this_deployments() -> None:
    url, note = rebase(
        "https://coreview.riverside.example/members/12345",
        "https://coreview.lakeside.example",
    )
    assert url == "https://coreview.lakeside.example/members/12345"
    assert note is not None and "riverside" in note and "lakeside" in note


def test_the_query_and_fragment_survive() -> None:
    """They are part of the path the capability recorded — a search that keeps its
    filters is not the same request as one that loses them."""
    url, _ = rebase(
        "https://a.example/members?q=smith&page=2#row-3", "https://b.example"
    )
    assert url == "https://b.example/members?q=smith&page=2#row-3"


def test_an_entry_url_with_a_path_prefix_keeps_it() -> None:
    """One tenant mounts the vendor product at the root, another under /corebank.
    Dropping the prefix would produce a 404 that reads as UI drift."""
    url, _ = rebase("https://a.example/members/1", "https://b.example/corebank/")
    assert url == "https://b.example/corebank/members/1"


def test_a_url_already_at_this_deployment_is_untouched_and_unremarked() -> None:
    """The ordinary case. A note on every step would train reviewers to skip them."""
    url, note = rebase("http://targetapp:8080/members", "http://targetapp:8080")
    assert url == "http://targetapp:8080/members"
    assert note is None


def test_a_relative_url_has_no_origin_to_replace() -> None:
    url, note = rebase("/members/12345", "https://b.example")
    assert url == "/members/12345"
    assert note is None


def test_no_entry_url_means_no_opinion() -> None:
    """Offline replay against recorded frames has no deployment, so it must not
    invent one."""
    url, note = rebase("https://a.example/x", "")
    assert url == "https://a.example/x"
    assert note is None
