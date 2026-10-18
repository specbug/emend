import json
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from emend.cli import app
from emend.inspect import sha256_file
from emend.pagination import page_indices, paginate
from emend.pipeline import get_report, ingest
from emend.storage import Store
from emend.visual import VisualReview, load_visual_review, visual_review
from tests.epub_factory import book_files, write_epub


def long_book(tmp_path, config, version="3.0"):
    files = book_files(version)
    paragraphs = "".join(
        f'<p id="paragraph-{i}">Paragraph {i}. '
        + "Mira watched the constellations above the observatory. " * 12
        + "</p>"
        for i in range(40)
    )
    files["EPUB/chapter.xhtml"] = files["EPUB/chapter.xhtml"].replace(
        b"</body>", paragraphs.encode() + b"</body>"
    )
    return ingest(write_epub(tmp_path / "long.epub", files), config)


@pytest.mark.parametrize(
    ("count", "expected"), [(1, [0]), (2, [0, 1]), (5, [0, 2, 4]), (6, [0, 3, 5])]
)
def test_page_selection_includes_first_middle_last(count, expected):
    assert page_indices(count) == expected


def test_page_selection_rejects_excessive_or_empty_documents():
    for count in (0, 5001):
        with pytest.raises(ValueError, match="between"):
            page_indices(count)


@pytest.mark.parametrize("version", ["2.0", "3.0"])
def test_real_paginated_epub_preserves_original_and_reports_page_positions(
    tmp_path, browser_config, version
):
    config = browser_config
    before = long_book(tmp_path, config, version)
    store = Store(config.root)
    old_row = store.resolve(before.book_id)
    review = visual_review(
        before.book_id, config, profiles=["compact"], max_documents=1, layout="paginated"
    )
    assert review.status == "complete", review.model_dump()
    sample = review.samples[0]
    assert sample.page_count > 3
    assert [s.page_number for s in sample.screenshots] == [
        1,
        sample.page_count // 2 + 1,
        sample.page_count,
    ]
    assert all(
        s.scroll_x == (s.page_number - 1) * 600 and s.scroll_y == 0 for s in sample.screenshots
    )
    assert len({s.sha256 for s in sample.screenshots}) == 3
    assert not sample.observations
    assert store.resolve(before.book_id) == old_row and get_report(store, before.book_id) == before
    assert sha256_file(Path(old_row["artifact_path"])) == before.book_id
    assert sha256_file(config.root / "originals" / f"{before.book_id}.epub") == before.book_id
    assert load_visual_review(store, before.book_id, review.id) == review
    html = (
        Path(store.document(before.book_id, "visual-review", review.id)["path"])
        .with_suffix(".html")
        .read_text()
    )
    assert "Paginated layout" in html and f"Page {sample.page_count} of {sample.page_count}" in html
    assert review.page_margin == 40


@pytest.fixture
def page(browser_config):
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(
            executable_path=str(browser_config.tools.chromium)
            if browser_config.tools.chromium
            else None
        )
        context = browser.new_context(
            viewport={"width": 600, "height": 800}, java_script_enabled=False, offline=True
        )
        page = context.new_page()
        yield page
        browser.close()


def test_page_geometry_keeps_last_content_reachable_and_authored_typography(page):
    paragraphs = "".join(
        f'<p id="p{i}">{i}: ' + "A quiet night at the observatory. " * 20 + "</p>"
        for i in range(35)
    )
    page.set_content(
        "<style>p{font:18px/1.6 monospace;color:rgb(40,50,60)}</style><body>"
        + paragraphs
        + "</body>"
    )
    before = page.locator("#p0").evaluate(
        "e => ({text:document.body.textContent, font:getComputedStyle(e).font, color:getComputedStyle(e).color})"
    )
    result = paginate(page, 600, 800, 40)
    assert not result["observations"]
    page.evaluate("x => scrollTo(x, 0)", (result["page_count"] - 1) * 600)
    rects = page.locator("#p34").evaluate(
        "e=>[...e.getClientRects()].map(r=>({left:r.left,right:r.right,top:r.top,bottom:r.bottom}))"
    )
    assert any(
        r["left"] >= 39 and r["right"] <= 561 and r["bottom"] <= 761 and r["top"] >= 39
        for r in rects
    )
    after = page.locator("#p0").evaluate(
        "e => ({text:document.body.textContent, font:getComputedStyle(e).font, color:getComputedStyle(e).color})"
    )
    assert before == after


def test_page_breaks_are_respected_without_altering_source_text(page):
    page.set_content(
        '<body><h1>First page</h1><p id="second" style="break-before:page">Second page</p></body>'
    )
    result = paginate(page, 600, 800, 40)
    assert result["page_count"] == 2
    assert "Page-break rules adapted" in result["observations"][0]
    assert page.locator("#second").evaluate("e=>e.getBoundingClientRect().left") == 640


def test_oversized_content_is_reported(page):
    page.set_content('<body><pre style="width:1200px">Wide table-like content</pre></body>')
    result = paginate(page, 600, 800, 40)
    assert any("exceed the page content box" in item for item in result["observations"])


@pytest.mark.parametrize("style", ["direction:rtl", "writing-mode:vertical-rl"])
def test_unsupported_document_flow_is_explicit(page, style):
    page.set_content(f'<body style="{style}"><p>Reader-specific content</p></body>')
    with pytest.raises(ValueError, match="horizontal left-to-right"):
        paginate(page, 600, 800, 40)


@pytest.mark.parametrize("kind", ["fixed", "spine-fixed", "rtl"])
def test_unsupported_package_layouts_fail_before_browser_launch(tmp_path, config, kind):
    files = book_files()
    if kind == "fixed":
        files["EPUB/package.opf"] = files["EPUB/package.opf"].replace(
            b"</metadata>", b'<meta property="rendition:layout">pre-paginated</meta></metadata>'
        )
    elif kind == "spine-fixed":
        files["EPUB/package.opf"] = files["EPUB/package.opf"].replace(
            b'idref="chapter"', b'idref="chapter" properties="rendition:layout-pre-paginated"'
        )
    else:
        files["EPUB/package.opf"] = files["EPUB/package.opf"].replace(
            b"<spine>", b'<spine page-progression-direction="rtl">'
        )
    before = ingest(write_epub(tmp_path / "unsupported.epub", files), config)
    with pytest.raises(ValueError, match="reflowable|Right-to-left"):
        visual_review(before.book_id, config, layout="paginated")
    assert not (config.root / "visual").exists()


def test_custom_margins_and_cli_output(tmp_path, browser_config):
    report = long_book(tmp_path, browser_config)
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(browser_config.model_dump(mode="json")))
    result = CliRunner().invoke(
        app,
        [
            "visual-review",
            report.book_id,
            "--config",
            str(path),
            "--profile",
            "compact",
            "--layout",
            "paginated",
            "--page-margin",
            "80",
            "--max-documents",
            "1",
        ],
    )
    assert result.exit_code == 0, result.output
    review = load_visual_review(Store(browser_config.root), report.book_id)
    assert review.layout == "paginated" and review.page_margin == 80
    result = CliRunner().invoke(app, ["visuals", report.book_id, "--config", str(path), "--json"])
    assert json.loads(result.output)["review"]["layout"] == "paginated"
    for layout, margin in [("unknown", 40), ("paginated", 0), ("paginated", 121)]:
        with pytest.raises(ValueError):
            visual_review(report.book_id, browser_config, layout=layout, page_margin=margin)


def test_existing_scroll_review_remains_readable():
    review = VisualReview(
        id="test",
        book_id="a" * 64,
        artifact_sha256="a" * 64,
        engine_version="2",
        created_at="2026-10-17",
        browser_version="fixture",
        title="Old review",
        total_spine_documents=1,
        selected_documents=["chapter.xhtml"],
        profiles=["compact"],
        status="complete",
        samples=[
            {
                "document": "chapter.xhtml",
                "profile": "compact",
                "width": 600,
                "height": 800,
                "screenshots": [{"file": "a.png", "sha256": "a" * 64, "scroll_y": 0}],
            }
        ],
    )
    assert review.layout == "scroll" and review.page_margin is None
    assert (
        review.samples[0].page_count is None
        and review.samples[0].screenshots[0].page_number is None
    )


def test_unbreakable_text_warns_about_crossing_page_margins(page):
    page.set_content("<body><p>" + "W" * 100 + "</p></body>")
    result = paginate(page, 600, 800, 40)
    assert any("Text fragments cross" in n for n in result["observations"])


def test_column_spanners_do_not_silently_produce_reader_pages(page):
    page.set_content('<body><h1 style="column-span:all">Heading</h1><p>Text</p></body>')
    with pytest.raises(ValueError, match="Column-spanning"):
        paginate(page, 600, 800, 40)


def test_unsupported_document_flow_records_incomplete_review(tmp_path, browser_config):
    files = book_files()
    files["EPUB/style.css"] += b"body{writing-mode:vertical-rl}"
    before = ingest(write_epub(tmp_path / "vertical.epub", files), browser_config)
    review = visual_review(
        before.book_id, browser_config, layout="paginated", profiles=["compact"], max_documents=1
    )
    assert review.status == "incomplete"
    assert "horizontal left-to-right" in review.samples[0].error
    assert not review.samples[0].screenshots
    assert get_report(Store(browser_config.root), before.book_id) == before


def test_animation_is_frozen_before_page_geometry_is_measured(page):
    page.set_content("""<style>@keyframes enlarge {from{font-size:16px}to{font-size:100px}}
        p{animation:enlarge 60s linear infinite}</style><body><p>Animated typography</p></body>""")
    result = paginate(page, 600, 800, 40)
    assert any("Animations frozen" in n for n in result["observations"])
    assert page.locator("p").evaluate("e=>getComputedStyle(e).fontSize") == "16px"
    assert page.evaluate('document.getAnimations().every(a=>a.playState==="paused")')


def test_browser_page_limit_is_enforced(page, monkeypatch):
    import emend.pagination as module

    monkeypatch.setattr(module, "MAX_PAGES", 2)
    page.set_content("<body>" + "<p>A quiet night. " * 500 + "</p></body>")
    with pytest.raises(ValueError, match="2-page limit"):
        paginate(page, 600, 800, 40)
