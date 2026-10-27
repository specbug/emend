"""Regression cases for actionable failures and gated browser downloads."""

import hashlib
import http.client
import json
import sys
from pathlib import Path

import pytest

from emend.pipeline import get_report, ingest
from emend.revalidation import revalidate
from emend.review_guidance import epubcheck_messages
from emend.storage import Store
from tests.epub_factory import write_epub
from tests.test_repairs import damaged_epub
from tests.test_review_ui import fields, login, post, request


def blocked(tmp_path, config):
    tool = tmp_path / "failed-epubcheck"
    messages = [
        {
            "severity": "ERROR",
            "message": "Element <bad> is not allowed here.",
            "locations": [{"path": "EPUB/chapter.xhtml", "line": 7}],
        }
    ]
    tool.write_text(
        f"#!{sys.executable}\nimport json,sys\nfrom pathlib import Path\nPath(sys.argv[sys.argv.index('--json')+1]).write_text(json.dumps({{'messages': {messages!r}}}))\nsys.exit(1)\n"
    )
    tool.chmod(0o755)
    config.tools.epubcheck = tool
    return ingest(write_epub(tmp_path / "blocked.epub"), config)


def raw_download(workspace, book, cookie=None):
    connection = http.client.HTTPConnection("127.0.0.1", workspace.server_port, timeout=30)
    connection.request(
        "GET", f"/books/{book}/download", headers={"Cookie": cookie} if cookie else {}
    )
    response = connection.getresponse()
    result = response.status, dict(response.getheaders()), response.read()
    connection.close()
    return result


def test_failed_book_shows_real_errors_and_sending_is_disabled(workspace, tmp_path, config):
    before = blocked(tmp_path, config)
    cookie = login(workspace)
    status, _, body = request(workspace, "/books/" + before.book_id, headers={"Cookie": cookie})
    assert status == 200 and "Needs attention before sending" in body
    assert "Element &lt;bad&gt; is not allowed here." in body
    assert "EPUB/chapter.xhtml:7" in body
    assert "Find available fixes" in body
    assert "<button disabled>Download EPUB</button>" in body
    assert f'href="/books/{before.book_id}/download"' not in body
    assert "Saved review evidence" not in body and "Current artifact" not in body
    assert get_report(Store(config.root), before.book_id) == before


def test_unsupported_fix_explains_manual_next_step(workspace, tmp_path, config):
    before = blocked(tmp_path, config)
    cookie = login(workspace)
    assert post(workspace, cookie, fields(workspace, before, "repair-plan"))[0] == 303
    body = request(workspace, "/books/" + before.book_id, headers={"Cookie": cookie})[2]
    assert "No automatic fix for these problems" in body
    assert "Edit book → Tools → Check book" in body
    assert "Element &lt;bad&gt; is not allowed here." in body
    assert "Fix EPUB packaging" not in body and "Fix contents links" not in body
    assert get_report(Store(config.root), before.book_id) == before


def test_publish_and_download_refusals_stay_on_the_book(
    workspace, tmp_path, config, fake_publishers
):
    before = blocked(tmp_path, config)
    cookie = login(workspace)
    status, _, body = post(
        workspace, cookie, fields(workspace, before, "publish", target="calibre")
    )
    assert status == 400 and "The Quiet Observatory" in body
    assert "That step could not finish" in body and "Element &lt;bad&gt;" in body
    assert "See checks and next steps" in body and "Review request" not in body
    assert not (config.publish.calibre_library / "fake-published.json").exists()
    status, headers, data = raw_download(workspace, before.book_id, cookie)
    assert status == 400 and "Content-Disposition" not in headers
    assert "The Quiet Observatory" in data.decode() and "Element &lt;bad&gt;" in data.decode()
    assert get_report(Store(config.root), before.book_id) == before


def test_available_fix_has_named_button_and_stale_report_requires_new_scan(
    workspace, tmp_path, config
):
    before = ingest(damaged_epub(tmp_path / "broken.epub"), config)
    cookie = login(workspace)
    assert post(workspace, cookie, fields(workspace, before, "repair-plan"))[0] == 303
    body = request(workspace, "/books/" + before.book_id, headers={"Cookie": cookie})[2]
    assert "Fix EPUB packaging" in body and "Available fix" in body
    revalidate(before.book_id, config)
    body = request(workspace, "/books/" + before.book_id, headers={"Cookie": cookie})[2]
    assert "Fix EPUB packaging" not in body and "Find available fixes" in body


def test_missing_checker_has_setup_and_refresh_action(workspace, tmp_path, config):
    good = config.tools.epubcheck
    config.tools.epubcheck = tmp_path / "missing"
    before = ingest(write_epub(tmp_path / "book.epub"), config)
    cookie = login(workspace)
    body = request(workspace, "/books/" + before.book_id, headers={"Cookie": cookie})[2]
    assert "brew install epubcheck" in body and "Run checks again" in body
    assert "Download EPUB</a>" not in body
    config.tools.epubcheck = good
    assert post(workspace, cookie, fields(workspace, before, "revalidate"))[0] == 303
    body = request(workspace, "/books/" + before.book_id, headers={"Cookie": cookie})[2]
    assert "Checks passed · ready to send" in body and "Download EPUB</a>" in body


def test_download_uses_approved_bytes_without_publishing_or_changing_state(
    workspace, tmp_path, config, monkeypatch
):
    import emend.publish as publishing

    before = ingest(write_epub(tmp_path / "book.epub"), config)
    row = Store(config.root).resolve(before.book_id)
    monkeypatch.setattr(
        publishing, "publish_calibre", lambda *a: pytest.fail("Download must not invoke Calibre")
    )
    assert raw_download(workspace, before.book_id)[0] == 401
    status, headers, data = raw_download(workspace, before.book_id, login(workspace))
    assert status == 200 and headers["Content-Type"] == "application/epub+zip"
    assert headers["Cache-Control"] == "no-store"
    assert (
        "attachment;" in headers["Content-Disposition"]
        and "filename*=UTF-8''" in headers["Content-Disposition"]
    )
    assert int(headers["Content-Length"]) == len(data)
    assert hashlib.sha256(data).hexdigest() == before.artifact_sha256
    assert Store(config.root).resolve(before.book_id) == row
    assert get_report(Store(config.root), before.book_id) == before
    assert not list((config.root / ".staging").iterdir())


@pytest.mark.parametrize("change", ["policy", "engine", "artifact", "original", "snapshot"])
def test_download_rechecks_all_core_gates(workspace, tmp_path, config, monkeypatch, change):
    import emend.publish as publishing
    import emend.revalidation as validation

    before = ingest(write_epub(tmp_path / "book.epub"), config)
    if change == "policy":
        config.quality.require_accessibility_audit = True
    elif change == "engine":
        monkeypatch.setattr(publishing, "ENGINE_VERSION", "next")
    elif change in {"artifact", "original"}:
        path = (
            config.root
            / ("curated" if change == "artifact" else "originals")
            / f"{before.book_id}.epub"
        )
        path.chmod(0o600)
        path.write_bytes(b"changed")
    else:

        def changed_snapshot(source, target, limit):
            target.write_bytes(b"changed")

        monkeypatch.setattr(validation, "snapshot", changed_snapshot)
    status, headers, _ = raw_download(workspace, before.book_id, login(workspace))
    assert status == 400 and "Content-Disposition" not in headers
    assert not list((config.root / ".staging").iterdir())


def test_validator_details_are_bounded_and_confined_to_book_reports(tmp_path, config):
    report = blocked(tmp_path, config)
    root = config.root / "reports" / report.book_id
    gate = next(g for g in report.gates if g.name == "epubcheck")
    path = Path(gate.evidence)
    path.write_text(json.dumps({"messages": [{"severity": "ERROR", "message": "problem"}] * 31}))
    messages, note = epubcheck_messages(report, root)
    assert len(messages) == 30 and "31" in note
    secret = tmp_path / "private.json"
    secret.write_text(
        json.dumps({"messages": [{"severity": "ERROR", "message": "private content"}]})
    )
    gate.evidence = str(secret)
    assert epubcheck_messages(report, root)[0] == []
    gate.evidence = str(path)
    path.write_bytes(b"x" * (2 * 1024**2 + 1))
    assert epubcheck_messages(report, root)[0] == []
    path.write_text("not JSON")
    assert epubcheck_messages(report, root)[0] == []


def test_unreadable_validator_evidence_does_not_turn_into_a_pass(workspace, tmp_path, config):
    report = blocked(tmp_path, config)
    gate = next(g for g in report.gates if g.name == "epubcheck")
    Path(gate.evidence).unlink()
    cookie = login(workspace)
    body = request(workspace, "/books/" + report.book_id, headers={"Cookie": cookie})[2]
    assert "detailed validator report could not be read" in body
    assert "<button disabled>Download EPUB</button>" in body


def test_spacing_preset_has_separate_preview_and_apply(workspace, tmp_path, config, fake_capture):
    from emend.typography import load_trial

    before = ingest(write_epub(tmp_path / "book.epub"), config)
    cookie = login(workspace)
    status, headers, _ = post(
        workspace,
        cookie,
        fields(
            workspace, before, "typography-trial", spacing_preset="comfortable", layout="scroll"
        ),
    )
    assert status == 303 and "panel=spacing" in headers["Location"]
    trial = load_trial(Store(config.root), before.book_id)
    assert trial.settings.line_height == 1.55 and trial.settings.paragraph_gap == 0.55
    assert get_report(Store(config.root), before.book_id) == before
    body = request(
        workspace, "/books/" + before.book_id + "?panel=spacing", headers={"Cookie": cookie}
    )[2]
    assert 'id="spacing" open' in body and "Apply this spacing" in body
    assert (
        post(workspace, cookie, fields(workspace, before, "apply-typography", trial=trial.id))[0]
        == 303
    )
    body = request(
        workspace, "/books/" + before.book_id + "?panel=spacing", headers={"Cookie": cookie}
    )[2]
    assert "Applied; kept for reference" in body
    assert "Apply this spacing" not in body


def test_browser_can_find_fix_and_download_without_a_dead_end(workspace, tmp_path, browser_config):
    from playwright.sync_api import sync_playwright

    before = ingest(damaged_epub(tmp_path / "repairable.epub"), browser_config)
    with sync_playwright() as p:
        browser = p.chromium.launch(
            executable_path=str(browser_config.tools.chromium)
            if browser_config.tools.chromium
            else None
        )
        page = browser.new_page(viewport={"width": 1280, "height": 1000})
        page.goto(workspace.entry_url)
        page.goto(f"http://127.0.0.1:{workspace.server_port}/books/{before.book_id}")
        assert page.get_by_role("button", name="Download EPUB", exact=True).is_disabled()
        for width in (1280, 390):
            page.set_viewport_size({"width": width, "height": 1000})
            page.evaluate("document.fonts.ready")
            assert page.evaluate("document.documentElement.scrollWidth") == width
            page.screenshot(path=str(tmp_path / f"blocked-{width}.png"), full_page=True)
        page.get_by_role("button", name="Find available fixes", exact=True).click()
        assert page.get_by_role("button", name="Fix EPUB packaging", exact=True).is_visible()
        for width in (1280, 390):
            page.set_viewport_size({"width": width, "height": 1000})
            page.screenshot(path=str(tmp_path / f"fix-available-{width}.png"), full_page=True)
        page.get_by_role("button", name="Fix EPUB packaging", exact=True).click()
        after = get_report(Store(browser_config.root), before.book_id)
        assert after.state == "VALIDATED" and after.repair_changes
        assert page.get_by_text("Checks passed · ready to send", exact=True).is_visible()
        assert not page.locator("#spacing").get_attribute("open")
        with page.expect_download() as download_event:
            page.get_by_role("link", name="Download EPUB", exact=True).click()
        downloaded = download_event.value
        target = tmp_path / downloaded.suggested_filename
        downloaded.save_as(target)
        assert hashlib.sha256(target.read_bytes()).hexdigest() == after.artifact_sha256
        for width in (1280, 390):
            page.set_viewport_size({"width": width, "height": 1000})
            assert page.evaluate("document.documentElement.scrollWidth") == width
            page.screenshot(path=str(tmp_path / f"ready-{width}.png"), full_page=True)
        browser.close()


def test_browser_unfixable_book_explains_blockers_and_keeps_user_in_context(
    workspace, tmp_path, browser_config
):
    from playwright.sync_api import sync_playwright

    before = blocked(tmp_path, browser_config)
    with sync_playwright() as p:
        browser = p.chromium.launch(
            executable_path=str(browser_config.tools.chromium)
            if browser_config.tools.chromium
            else None
        )
        page = browser.new_page(viewport={"width": 390, "height": 1000})
        page.goto(workspace.entry_url)
        page.goto(f"http://127.0.0.1:{workspace.server_port}/books/{before.book_id}")
        assert page.get_by_text(
            "Element <bad> is not allowed here.", exact=False
        ).first.is_visible()
        page.get_by_role("button", name="Find available fixes", exact=True).click()
        assert page.get_by_role(
            "heading", name="No automatic fix for these problems", exact=True
        ).is_visible()
        page.goto(f"http://127.0.0.1:{workspace.server_port}/books/{before.book_id}/download")
        assert page.get_by_role("heading", name="The Quiet Observatory", exact=True).is_visible()
        assert page.get_by_role("alert").is_visible()
        assert page.get_by_role("button", name="Download EPUB", exact=True).is_disabled()
        assert page.get_by_role("link", name="See checks and next steps", exact=True).is_visible()
        assert page.evaluate("document.documentElement.scrollWidth") == 390
        page.screenshot(path=str(tmp_path / "manual-390.png"), full_page=True)
        browser.close()


def test_cover_check_distinguishes_library_image_from_epub_declaration(tmp_path, config):
    from emend.review_guidance import cover_observation
    from tests.test_covers import cover_book

    undeclared = ingest(write_epub(tmp_path / "undeclared.epub"), config)
    source = config.root / "curated" / f"{undeclared.book_id}.epub"
    info = cover_observation(source, undeclared.inspection)
    assert info["title"] == "No cover image declared in the EPUB" and info["can_add"]
    assert "Calibre may show a separate library cover" in info["detail"]
    declared = cover_book(tmp_path, config)
    source = config.root / "curated" / f"{declared.book_id}.epub"
    info = cover_observation(source, declared.inspection)
    assert info["title"] == "Cover found in EPUB"
    assert "PNG" in info["detail"] and "160 × 240" in info["detail"]
    assert not info["can_add"] and not info["warnings"]


@pytest.mark.parametrize("problem", ["extension", "mimetype", "svg", "guide", "fixed"])
def test_cover_check_reports_reader_hints_without_changing_approval(tmp_path, config, problem):
    from emend.review_guidance import cover_observation
    from tests.epub_factory import book_files

    files = book_files()
    files["EPUB/package.opf"] = files["EPUB/package.opf"].replace(
        b'id="image"', b'id="image" properties="cover-image"'
    )
    if problem == "extension":
        files["EPUB/package.opf"] = files["EPUB/package.opf"].replace(
            b"observatory.png", b"observatory.bin"
        )
        files["EPUB/chapter.xhtml"] = files["EPUB/chapter.xhtml"].replace(
            b"observatory.png", b"observatory.bin"
        )
        files["EPUB/observatory.bin"] = files.pop("EPUB/observatory.png")
    elif problem == "mimetype":
        files["EPUB/package.opf"] = files["EPUB/package.opf"].replace(b"image/png", b"image/jpeg")
    elif problem == "svg":
        files["EPUB/package.opf"] = (
            files["EPUB/package.opf"]
            .replace(b"image/png", b"image/svg+xml")
            .replace(b"observatory.png", b"observatory.svg")
        )
        files["EPUB/chapter.xhtml"] = files["EPUB/chapter.xhtml"].replace(
            b"observatory.png", b"observatory.svg"
        )
        files.pop("EPUB/observatory.png")
        files["EPUB/observatory.svg"] = (
            b'<svg xmlns="http://www.w3.org/2000/svg" width="160" height="240"><rect width="160" height="240" fill="#345a4a"/></svg>'
        )
    else:
        files["EPUB/package.opf"] = files["EPUB/package.opf"].replace(
            b' properties="cover-image"', b""
        )
        if problem == "guide":
            files["EPUB/package.opf"] = files["EPUB/package.opf"].replace(
                b"</package>",
                b'<guide><reference type="cover" href="chapter.xhtml" title="Cover"/></guide></package>',
            )
        else:
            files["EPUB/package.opf"] = files["EPUB/package.opf"].replace(
                b"</metadata>", b'<meta property="rendition:layout">pre-paginated</meta></metadata>'
            )
    before = ingest(write_epub(tmp_path / "cover.epub", files), config)
    source = config.root / "curated" / f"{before.book_id}.epub"
    info = cover_observation(source, before.inspection)
    assert not info["can_add"]
    if problem == "extension":
        assert "filename extension" in info["warnings"][0]
    elif problem == "mimetype":
        assert "declared image type" in info["warnings"][0]
    elif problem == "svg":
        assert info["title"] == "Cover needs review"
    assert get_report(Store(config.root), before.book_id) == before


def test_book_page_explains_crosspoint_bmp_generation(workspace, tmp_path, config):
    from tests.test_covers import cover_book

    before = cover_book(tmp_path, config)
    body = request(workspace, "/books/" + before.book_id, headers={"Cookie": login(workspace)})[2]
    assert "Cover found in EPUB" in body
    assert "makes its own cover and thumbnail BMP caches" in body
    assert "Settings → Display → Sleep Screen → Cover" in body
    assert get_report(Store(config.root), before.book_id) == before


def test_calibre_lock_error_offers_download_without_blocking_ready_book(
    workspace, tmp_path, config, fake_publishers, monkeypatch
):
    import emend.publish as publishing

    before = ingest(write_epub(tmp_path / "book.epub"), config)

    def busy(*args):
        raise ValueError(
            "Another calibre program such as calibre-server or the main calibre program is running."
        )

    monkeypatch.setattr(publishing, "publish_calibre", busy)
    status, _, body = post(
        workspace, login(workspace), fields(workspace, before, "publish", target="calibre")
    )
    assert status == 400 and "Calibre is open" in body
    assert "Calibre’s Add books" in body
    assert f'href="/books/{before.book_id}/download"' in body
    assert "Checks passed · ready to send" in body
    assert get_report(Store(config.root), before.book_id) == before


def test_metadata_save_is_disabled_when_required_checks_fail(workspace, tmp_path, config):
    from emend.reports import write_reports
    from tests.test_curation import setup_book

    before, _ = setup_book(tmp_path, config)
    row = Store(config.root).resolve(before.book_id)
    failed = before.model_copy(deep=True, update={"state": "FAILED"})
    next(g for g in failed.gates if g.name == "epubcheck").status = "failed"
    path = write_reports(failed, Path(row["report_path"]).parent)
    Store(config.root).transition(
        before.book_id,
        row["source_name"],
        "FAILED",
        "Injected required failure",
        path,
        Path(row["artifact_path"]),
    )
    body = request(
        workspace,
        "/books/" + before.book_id + "?panel=details",
        headers={"Cookie": login(workspace)},
    )[2]
    assert '<button type="submit" disabled>Save selected book details</button>' in body
    assert "Resolve the required check failures before saving book details" in body
