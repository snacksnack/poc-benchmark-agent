"""The published site (RC1-635, ADR 0027): every page builds, every link
points somewhere real, and the writeup is the front page."""

import re

import pytest

pytest.importorskip("markdown_it")

from publish.render import REPO, build, pages, resolve, routes  # noqa: E402

TABLE = routes(pages())


def test_the_engagement_writeup_is_the_index():
    assert [p.source for p in pages() if p.out == "index.html"] == ["docs/engagement.md"]


def test_every_document_the_writeup_links_is_published_or_on_github(tmp_path):
    build(tmp_path)
    index = (tmp_path / "index.html").read_text()
    for href in re.findall(r'href="([^"]+)"', index):
        if href.startswith(("http://", "https://")):
            assert href.startswith((REPO, "https://")), href
            continue
        assert (tmp_path / href.split("#")[0]).exists(), href


def test_every_published_page_exists_and_has_navigation(tmp_path):
    written = build(tmp_path)
    assert len(written) == len(pages())
    for path in written:
        html = path.read_text()
        assert 'aria-current="page"' in html, path.name
        assert "<title>" in html


def test_a_link_from_an_adr_back_to_a_doc_is_relative():
    assert resolve("../risks.md", source="docs/adr/0001-neon-sizing-and-cost-not-guc-tuning.md",
                   out="adr/0001-neon-sizing-and-cost-not-guc-tuning.html",
                   table=TABLE) == "../risks.html"


def test_a_link_to_code_goes_to_github():
    assert resolve("../config/goal.toml", source="docs/engagement.md", out="index.html",
                   table=TABLE) == f"{REPO}/blob/main/config/goal.toml"


def test_a_link_to_nothing_fails_the_build():
    with pytest.raises(ValueError, match="resolves to nothing"):
        resolve("missing.md", source="docs/engagement.md", out="index.html", table=TABLE)


def test_the_results_document_and_loop_log_are_published():
    outs = {p.source: p.out for p in pages()}
    for doc in ("docs/results.md", "docs/loop-log.md", "docs/criteria.md", "docs/risks.md"):
        assert doc in outs, doc
