import http.client
import threading
from pathlib import Path
from urllib.parse import urlencode, urlsplit

import pytest
from PIL import Image

from emend.inspect import sha256_file
from emend.pipeline import get_report, ingest
from emend.publish import publish
from emend.reading_review import reading_review
from emend.repairs import prepare_repairs
from emend.review_ui import ReviewServer
from emend.storage import Store
from emend.visual import PROFILES, Sample, Shot
from tests.epub_factory import book_files, write_epub
from tests.test_curation import setup_book
from tests.test_repairs import damaged_epub


@pytest.fixture
def workspace(config):
    with ReviewServer(config, 0) as server:
        thread = threading.Thread(
            target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
        )
        thread.start()
        yield server
        server.app.runner.wait(120)
        server.shutdown()
        thread.join(timeout=3)


@pytest.fixture
def fake_pages(monkeypatch):
    """Deterministic reader-page captures for the workspace preview step."""
    calls = []

    def capture(source, directory, selected, media, profiles, config, **options):
        calls.append(options)
        samples = []
        for profile in profiles:
            width, height = PROFILES[profile]
            for i, name in enumerate(selected):
                image = directory / f"{profile}-{i}.png"
                Image.new("RGB", (16, 16), "#887766").save(image)
                samples.append(
                    Sample(
                        document=name,
                        profile=profile,
                        width=width,
                        height=height,
                        page_count=1,
                        screenshots=[
                            Shot(
                                file=image.name,
                                sha256=sha256_file(image),
                                scroll_y=0,
                                page_number=1,
                            )
                        ],
                    )
                )
        return "fixture-browser", samples

    monkeypatch.setattr("emend.visual.render", capture)
    return calls


def request(server, path="/", method="GET", body=None, headers=None):
    connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=30)
    connection.request(method, path, body=body, headers=headers or {})
    response = connection.getresponse()
    data = response.read().decode(errors="replace")
    result = response.status, dict(response.getheaders()), data
    connection.close()
    return result


def login(server):
    url = urlsplit(server.entry_url)
    status, headers, _ = request(server, url.path + "?" + url.query)
    assert status == 303 and headers["Location"] == "/"
    assert "HttpOnly" in headers["Set-Cookie"] and "SameSite=Strict" in headers["Set-Cookie"]
    return headers["Set-Cookie"].split(";")[0]


def post(server, cookie, fields, **headers):
    result = request(
        server,
        "/action",
        "POST",
        urlencode(fields, doseq=True),
        {
            "Cookie": cookie,
            "Origin": f"http://127.0.0.1:{server.server_port}",
            "Content-Type": "application/x-www-form-urlencoded",
            **headers,
        },
    )
    # Long actions continue in a background run; tests observe their finished state.
    server.app.runner.wait(120)
    return result


def upload(server, cookie, path, name=None):
    boundary = "emend-upload-boundary"
    body = (
        (
            f'--{boundary}\r\nContent-Disposition: form-data; name="csrf"\r\n\r\n'
            f"{server.app.csrf}\r\n--{boundary}\r\n"
            'Content-Disposition: form-data; name="action"\r\n\r\nimport\r\n'
            f'--{boundary}\r\nContent-Disposition: form-data; name="epub"; '
            f'filename="{name or Path(path).name}"\r\nContent-Type: application/epub+zip\r\n\r\n'
        ).encode()
        + Path(path).read_bytes()
        + f"\r\n--{boundary}--\r\n".encode()
    )
    result = request(
        server,
        "/action",
        "POST",
        body,
        {
            "Cookie": cookie,
            "Origin": f"http://127.0.0.1:{server.server_port}",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        },
    )
    server.app.runner.wait(120)
    return result


def fields(server, report, action, **extra):
    return {
        "csrf": server.app.csrf,
        "book": report.book_id,
        "artifact": report.artifact_sha256 or report.book_id,
        "action": action,
        **extra,
    }


def test_authentication_and_no_get_mutation(workspace, tmp_path, config):
    before = ingest(write_epub(tmp_path / "book.epub"), config)
    row = Store(config.root).resolve(before.book_id)
    assert request(workspace)[0] == 401
    assert request(workspace, headers={"Host": "evil.invalid"})[0] == 403
    cookie = login(workspace)
    for step in ["", "?step=import", "?step=check", "?step=repair", "?step=preview"]:
        status, headers, body = request(
            workspace, "/books/" + before.book_id + step, headers={"Cookie": cookie}
        )
        assert status == 200 and "The Quiet Observatory" in body
    for step in ["?step=refine", "?step=send", "?step=unknown"]:
        assert (
            request(workspace, "/books/" + before.book_id + step, headers={"Cookie": cookie})[0]
            == 200
        )
    assert "default-src 'none'" in headers["Content-Security-Policy"]
    assert "script-src 'self'" in headers["Content-Security-Policy"]
    assert "unsafe-inline" not in headers["Content-Security-Policy"]
    assert headers["Referrer-Policy"] == "same-origin"
    assert request(workspace, "/status")[0] == 401
    assert get_report(Store(config.root), before.book_id) == before
    assert Store(config.root).resolve(before.book_id) == row
    assert not workspace.app.runner.runs


def test_static_assets_are_package_files_only(workspace):
    status, headers, body = request(workspace, "/assets/workspace.css")
    assert status == 200 and headers["Content-Type"].startswith("text/css")
    assert "@font-face" in body and "oklch" in body
    assert request(workspace, "/assets/workspace.js")[1]["Content-Type"].startswith(
        "text/javascript"
    )
    assert request(workspace, "/assets/DMSans.ttf")[0] == 200
    assert request(workspace, "/assets/favicon.svg")[1]["Content-Type"] == "image/svg+xml"
    assert request(workspace, "/assets/report.css")[0] == 404
    assert request(workspace, "/assets/..%2Fconfig.py")[0] == 404
    assert request(workspace, "/assets/workspace.css", headers={"Host": "evil.invalid"})[0] == 403


@pytest.mark.parametrize("origin", ["http://evil.invalid", "null", "http://localhost:1234", ""])
def test_cross_origin_actions_are_rejected(workspace, tmp_path, config, origin):
    before = ingest(write_epub(tmp_path / "book.epub"), config)
    status, _, _ = post(
        workspace, login(workspace), fields(workspace, before, "revalidate"), Origin=origin
    )
    assert status == 403
    assert get_report(Store(config.root), before.book_id) == before


def test_form_token_stale_hash_and_unknown_actions_are_rejected(workspace, tmp_path, config):
    before = ingest(write_epub(tmp_path / "book.epub"), config)
    cookie = login(workspace)
    assert post(workspace, cookie, fields(workspace, before, "revalidate", csrf="wrong"))[0] == 403
    assert (
        post(workspace, cookie, fields(workspace, before, "revalidate", artifact="0" * 64))[0]
        == 400
    )
    assert post(workspace, cookie, fields(workspace, before, "arbitrary-shell"))[0] == 400
    assert post(workspace, cookie, fields(workspace, before, "apply-repairs"))[0] == 400
    assert get_report(Store(config.root), before.book_id) == before
    assert not workspace.app.runner.runs


def test_unicode_and_duplicate_entry_tokens_fail_cleanly(workspace):
    assert request(workspace, "/?token=%C3%A9")[0] == 401
    assert request(workspace, "/?token=a&token=b")[0] == 400


def test_run_again_refreshes_quality_evidence(workspace, tmp_path, config, fake_pages):
    before = ingest(write_epub(tmp_path / "book.epub"), config)
    status, headers, _ = post(workspace, login(workspace), fields(workspace, before, "revalidate"))
    assert status == 303 and headers["Location"] == "/books/" + before.book_id
    after = get_report(Store(config.root), before.book_id)
    assert after.state == "VALIDATED" and after.revalidation
    assert after.artifact_sha256 == before.artifact_sha256
    run = workspace.app.runner.get(before.book_id)
    assert [s.state for s in run.steps] == ["done", "done", "done", "done"]
    assert Store(config.root).document(before.book_id, "visual-review")


def test_import_checks_repairs_and_previews_without_further_clicks(
    workspace, tmp_path, config, fake_pages
):
    source = damaged_epub(tmp_path / "broken.epub")
    cookie = login(workspace)
    status, headers, _ = upload(workspace, cookie, source)
    book = sha256_file(source)
    assert status == 303 and headers["Location"] == "/books/" + book
    report = get_report(Store(config.root), book)
    # The supported packaging repair passed its own gates; the original stays unchanged.
    assert report.state == "VALIDATED" and report.repair_changes
    assert sha256_file(config.root / "originals" / f"{book}.epub") == book
    assert fake_pages and fake_pages[0]["layout"] == "paginated"
    body = request(workspace, "/books/" + book, headers={"Cookie": cookie})[2]
    assert "Ready. The checks passed" in body
    assert f'href="/books/{book}/download"' in body
    assert 'aria-current="step"' in body and "Preview" in body
    assert (
        "Fix EPUB packaging"
        in request(workspace, f"/books/{book}?step=repair", headers={"Cookie": cookie})[2]
    )
    # Importing the same bytes again opens the book without resetting its edits.
    assert upload(workspace, cookie, source)[0] == 303
    assert get_report(Store(config.root), book) == report
    assert (
        "already on your shelf"
        in request(workspace, "/books/" + book, headers={"Cookie": cookie})[2]
    )


def test_run_progress_is_visible_and_polled_read_only(workspace, tmp_path, config, monkeypatch):
    import emend.workspace_runs as runs

    before = ingest(write_epub(tmp_path / "book.epub"), config)
    release = threading.Event()
    entered = threading.Event()

    def slow_preview(run, config, book):
        run.begin("preview")
        entered.set()
        release.wait(10)
        run.end("preview", "failed", "Chromium could not launch for this fixture.")

    monkeypatch.setattr(runs, "preview", slow_preview)
    cookie = login(workspace)
    request(
        workspace,
        "/action",
        "POST",
        urlencode(fields(workspace, before, "revalidate")),
        {
            "Cookie": cookie,
            "Origin": f"http://127.0.0.1:{workspace.server_port}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    assert entered.wait(10)
    status, headers, data = request(workspace, "/status", headers={"Cookie": cookie})
    assert status == 200 and headers["Content-Type"] == "application/json"
    assert '"active": true' in data
    body = request(workspace, "/books/" + before.book_id, headers={"Cookie": cookie})[2]
    assert "Working on it" in body and 'data-status="/status"' in body
    assert "Render reader pages" in body and "<noscript>" in body
    busy = post(workspace, cookie, fields(workspace, before, "reading"))
    assert busy[0] == 409 and "still working on The Quiet Observatory" in busy[2]
    release.set()
    workspace.app.runner.wait(30)
    assert '"active": false' in request(workspace, "/status", headers={"Cookie": cookie})[2]
    body = request(
        workspace, "/books/" + before.book_id + "?step=preview", headers={"Cookie": cookie}
    )[2]
    assert "The last run stopped here" in body
    assert "Chromium could not launch for this fixture." in body


def test_page_images_are_served_only_when_hashes_match(workspace, tmp_path, config, fake_pages):
    from emend.visual import load_visual_review

    before = ingest(write_epub(tmp_path / "book.epub"), config)
    cookie = login(workspace)
    assert post(workspace, cookie, fields(workspace, before, "visual", layout="scroll"))[0] == 303
    review = load_visual_review(Store(config.root), before.book_id)
    shot = review.samples[0].screenshots[0]
    url = f"/books/{before.book_id}/pages/visual-review/{review.id}/pages/{shot.file}"
    body = request(workspace, f"/books/{before.book_id}?step=preview", headers={"Cookie": cookie})[
        2
    ]
    assert url in body
    status, headers, _ = request(workspace, url, headers={"Cookie": cookie})
    assert status == 200 and headers["Content-Type"] == "image/png"
    assert request(workspace, url)[0] == 401
    unknown = url.rsplit("/", 1)[0] + "/review.json"
    assert request(workspace, unknown, headers={"Cookie": cookie})[0] == 400
    image = Path(Store(config.root).document(before.book_id, "visual-review")["path"]).parent
    (image / shot.file).write_bytes(b"changed")
    assert request(workspace, url, headers={"Cookie": cookie})[0] == 400


def test_explicit_metadata_selection_uses_core_preservation(workspace, tmp_path, config):
    before, plan = setup_book(tmp_path, config)
    status, _, _ = post(
        workspace,
        login(workspace),
        fields(
            workspace,
            before,
            "apply-metadata",
            plan=plan.id,
            proposal=[p.id for p in plan.proposals],
        ),
    )
    assert status == 303
    after = get_report(Store(config.root), before.book_id)
    assert after.inspection.metadata["publisher"] == ["Observatory Press"]
    assert after.inspection.spine_text_sha256 == before.inspection.spine_text_sha256


def test_saved_evidence_is_sandboxed_and_tampering_is_reported(workspace, tmp_path, config):
    before = ingest(write_epub(tmp_path / "book.epub"), config)
    review = reading_review(before.book_id, config)
    cookie = login(workspace)
    url = f"/evidence/{before.book_id}/reading-review/{review.id}"
    status, headers, body = request(workspace, url, headers={"Cookie": cookie})
    assert status == 200 and "sandbox;" in headers["Content-Security-Policy"]
    assert "form-action 'self'" in headers["Content-Security-Policy"]
    assert headers["Referrer-Policy"] == "no-referrer"
    page = request(workspace, f"/books/{before.book_id}?step=preview", headers={"Cookie": cookie})[
        2
    ]
    assert url in page and "Open the font and footnote report" in page
    path = Path(Store(config.root).document(before.book_id, "reading-review", review.id)["path"])
    path.write_text(path.read_text() + "\n")
    assert request(workspace, url, headers={"Cookie": cookie})[0] == 400


def test_repair_review_uses_registered_html_filename(workspace, tmp_path, config):
    before = ingest(write_epub(tmp_path / "book.epub"), config)
    plan, _ = prepare_repairs(before.book_id, config)
    status, _, body = request(
        workspace,
        f"/evidence/{before.book_id}/repairs/{plan.id}",
        headers={"Cookie": login(workspace)},
    )
    assert status == 200 and "No supported repair" in body


def test_untrusted_book_metadata_cannot_inject_markup(workspace, tmp_path, config):
    files = book_files()
    files["EPUB/package.opf"] = files["EPUB/package.opf"].replace(
        b"The Quiet Observatory", b"&lt;script&gt;alert(1)&lt;/script&gt;"
    )
    before = ingest(write_epub(tmp_path / "book.epub", files), config)
    cookie = login(workspace)
    for url in ["/", "/books/" + before.book_id, "/books/" + before.book_id + "?step=import"]:
        status, _, body = request(workspace, url, headers={"Cookie": cookie})
        assert status == 200 and "<script>" not in body and "&lt;script&gt;" in body


def test_bounded_forms_and_concurrent_operation_return_actionable_errors(
    workspace, tmp_path, config
):
    before = ingest(write_epub(tmp_path / "book.epub"), config)
    cookie = login(workspace)
    assert (
        request(
            workspace,
            "/action",
            "POST",
            "x" * 65537,
            {
                "Cookie": cookie,
                "Origin": f"http://127.0.0.1:{workspace.server_port}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )[0]
        == 400
    )
    workspace.app.operation.acquire()
    try:
        assert post(workspace, cookie, fields(workspace, before, "revalidate"))[0] == 409
    finally:
        workspace.app.operation.release()
    assert not workspace.app.runner.runs


def test_publication_rechecks_expected_artifact_under_operation_lock(
    tmp_path, config, fake_publishers
):
    before = ingest(write_epub(tmp_path / "book.epub"), config)
    with pytest.raises(ValueError, match="stale"):
        publish(before.book_id, ["calibre"], config, expected_artifact_sha256="0" * 64)
    assert not (config.publish.calibre_library / "fake-published.json").exists()


def test_typography_actions_use_complete_current_evidence(
    workspace, tmp_path, config, fake_capture, fake_pages
):
    before = ingest(write_epub(tmp_path / "book.epub"), config)
    cookie = login(workspace)
    result = post(
        workspace,
        cookie,
        fields(
            workspace,
            before,
            "typography-trial",
            line_height="1.7",
            paragraph_gap=".6",
            layout="scroll",
        ),
    )
    assert result[0] == 303 and result[1]["Location"].endswith("?step=refine")
    trial = Store(config.root).document(before.book_id, "typography-trial")
    assert get_report(Store(config.root), before.book_id) == before
    assert (
        post(workspace, cookie, fields(workspace, before, "apply-typography", trial=trial["id"]))[0]
        == 303
    )
    after = get_report(Store(config.root), before.book_id)
    assert after.artifact_sha256 != before.artifact_sha256 and after.typography_changes
    assert (
        post(workspace, cookie, fields(workspace, before, "apply-typography", trial=trial["id"]))[0]
        == 400
    )


def test_spacing_can_be_compared_applied_and_returned_to_baseline(
    workspace, tmp_path, config, fake_capture, fake_pages
):
    before = ingest(write_epub(tmp_path / "book.epub"), config)
    cookie = login(workspace)
    page = f"/books/{before.book_id}?step=refine"
    assert "Compare with the baseline" in request(workspace, page, headers={"Cookie": cookie})[2]
    assert (
        post(
            workspace,
            cookie,
            fields(workspace, before, "typography-trial", spacing_preset="roomy"),
        )[0]
        == 303
    )
    body = request(workspace, page, headers={"Cookie": cookie})[2]
    assert "Not applied yet" in body and "Line height 1.75" in body
    assert "Side by side" in body and "/pages/typography-trial/" in body
    assert 'name="all_spine" value="yes" checked' in body
    trial = Store(config.root).document(before.book_id, "typography-trial")["id"]
    assert (
        post(
            workspace,
            cookie,
            fields(workspace, before, "apply-typography", trial=trial, all_spine="yes"),
        )[0]
        == 303
    )
    adjusted = get_report(Store(config.root), before.book_id)
    assert adjusted.typography_changes[0].all_spine
    body = request(workspace, page, headers={"Cookie": cookie})[2]
    assert "Applied to the book" in body and "Return to the baseline" in body
    row = Store(config.root).resolve(before.book_id)
    status, _, _ = post(
        workspace,
        cookie,
        fields(
            workspace,
            adjusted,
            "restore-baseline",
            revision=before.artifact_sha256,
            expected_report=row["report_path"],
        ),
    )
    assert status == 303
    restored = get_report(Store(config.root), before.book_id)
    assert restored.artifact_sha256 == before.artifact_sha256 and restored.recovery_changes
    assert restored.state == "VALIDATED"


def test_real_browser_import_review_and_evidence_are_usable(workspace, tmp_path, browser_config):
    from playwright.sync_api import sync_playwright

    config = browser_config
    # The server and browser fixture share the same configuration object.
    source = write_epub(tmp_path / "new-book.epub")
    with sync_playwright() as p:
        browser = p.chromium.launch(
            executable_path=str(config.tools.chromium) if config.tools.chromium else None
        )
        page = browser.new_page(viewport={"width": 1280, "height": 1000})
        events = []
        page.on("response", lambda r: events.append((urlsplit(r.url).path, r.status)))
        page.on("requestfailed", lambda r: events.append((urlsplit(r.url).path, r.failure)))
        console = []
        page.on("console", lambda m: console.append(m.text) if m.type == "error" else None)
        page.goto(workspace.entry_url)
        assert page.get_by_role("heading", name="Bring a book in.", exact=True).is_visible()
        # Choosing the file starts the import; the page follows the run until it is done.
        page.get_by_label("Choose an EPUB").set_input_files(source)
        page.get_by_role("link", name="Download EPUB", exact=True).wait_for(timeout=120000)
        assert page.get_by_role("heading", name="The Quiet Observatory", exact=True).is_visible(), (
            events,
            page.content(),
        )
        book = Store(config.root).books()[0]
        assert book["id"] == sha256_file(source)
        assert get_report(Store(config.root), book["id"]).state == "VALIDATED"
        assert not console, console
        assert all(status != 404 for _, status in events if isinstance(status, int))
        page.locator("summary").filter(has_text="Fonts and footnotes").click()
        with page.expect_popup() as popup:
            page.get_by_role("link", name="Open the font and footnote report").click()
        report = popup.value
        assert report.get_by_role("heading", name="The Quiet Observatory", exact=True).is_visible()
        for width in (1280, 390):
            page.set_viewport_size({"width": width, "height": 1000})
            assert page.evaluate("document.documentElement.scrollWidth") == width
        browser.close()


def test_real_browser_local_cover_can_be_reviewed_before_application(
    workspace, tmp_path, browser_config
):
    from playwright.sync_api import sync_playwright

    from emend.covers import load_cover_review
    from tests.test_covers import candidate, cover_book

    before = cover_book(tmp_path, browser_config)
    image = candidate(tmp_path)
    with sync_playwright() as p:
        browser = p.chromium.launch(
            executable_path=str(browser_config.tools.chromium)
            if browser_config.tools.chromium
            else None
        )
        page = browser.new_page(viewport={"width": 1280, "height": 1000})
        page.goto(workspace.entry_url)
        page.goto(f"http://127.0.0.1:{workspace.server_port}/books/{before.book_id}?step=refine")
        page.locator("summary").filter(has_text="Book details and cover").click()
        page.get_by_label("JPEG or PNG cover, up to 20 MiB").set_input_files(image)
        page.get_by_label("Source/provenance note").fill(
            "Original synthetic fixture, explicitly supplied for this edition"
        )
        page.get_by_role("button", name="Compare local cover", exact=True).click()
        page.locator('input[name="candidate"]').first.wait_for(state="attached")
        review = load_cover_review(Store(browser_config.root), before.book_id)
        assert review.candidates
        assert get_report(Store(browser_config.root), before.book_id) == before
        page.locator('input[name="candidate"]').first.check()
        page.get_by_role("button", name="Apply reviewed cover", exact=True).click()
        page.get_by_text("Cover updated in the EPUB").wait_for()
        assert get_report(Store(browser_config.root), before.book_id).cover_changes
        browser.close()


@pytest.mark.parametrize("archive_limit, displayed", [(256, 128), (1, 1)])
def test_upload_limit_display_and_request_bound(workspace, config, archive_limit, displayed):
    config.limits.max_archive_mb = archive_limit
    cookie = login(workspace)
    assert f"Up to {displayed} MiB here" in request(workspace, headers={"Cookie": cookie})[2]
    status, _, body = request(
        workspace,
        "/action",
        "POST",
        b"",
        {
            "Cookie": cookie,
            "Origin": f"http://127.0.0.1:{workspace.server_port}",
            "Content-Type": "multipart/form-data; boundary=emend-test",
            "Content-Length": str(displayed * 1024**2 + 65536 + 1),
        },
    )
    assert status == 400 and "Request exceeds" in body
    assert not Store(config.root).books()


@pytest.mark.parametrize("over_limit", [False, True])
def test_epub_upload_file_boundary(workspace, config, tmp_path, over_limit, fake_pages):
    import zipfile

    config.limits.max_archive_mb = 1
    source = write_epub(tmp_path / "boundary.epub")
    # Add a stored resource to reach the exact file boundary without a large
    # compressed expansion. ZIP local/central records add 76 + twice the name.
    name = "EPUB/padding.bin"
    padding = 1024**2 - source.stat().st_size - 76 - 2 * len(name)
    with zipfile.ZipFile(source, "a") as archive:
        archive.writestr(name, b"\0" * padding, compress_type=zipfile.ZIP_STORED)
    assert source.stat().st_size == 1024**2
    if over_limit:
        source.write_bytes(source.read_bytes() + b"x")
    status, _, response = upload(workspace, login(workspace), source, "boundary.epub")
    if over_limit:
        assert status == 400 and "EPUB exceeds the upload size limit" in response
        assert not Store(config.root).books()
    else:
        assert status == 303, response
        original = config.root / "originals" / f"{sha256_file(source)}.epub"
        assert original.read_bytes() == source.read_bytes()
        assert not list((config.root / ".staging").iterdir())


def test_failed_import_explains_what_went_wrong(workspace, tmp_path, config):
    source = tmp_path / "not-a-book.epub"
    source.write_bytes(b"this is not a zip archive")
    cookie = login(workspace)
    status, headers, _ = upload(workspace, cookie, source)
    assert status == 303
    body = request(workspace, headers["Location"], headers={"Cookie": cookie})[2]
    assert "What went wrong" in body or "Needs you" in body
    assert "Traceback" not in body


def test_import_while_busy_keeps_the_shelf_and_explains(workspace, tmp_path, config):
    source = write_epub(tmp_path / "book.epub")
    cookie = login(workspace)
    workspace.app.operation.acquire()
    try:
        status, _, body = upload(workspace, cookie, source)
    finally:
        workspace.app.operation.release()
    assert status == 409 and "Bring a book in." in body and 'role="alert"' in body
    assert "Another review action is running" in body
    assert not Store(config.root).books()


def test_partial_font_and_note_inventory_is_reported_not_stopped(
    workspace, tmp_path, config, fake_pages, monkeypatch
):
    monkeypatch.setattr("emend.reading_review.MAX_DOCUMENTS", 1)
    before = ingest(write_epub(tmp_path / "book.epub"), config)
    cookie = login(workspace)
    assert post(workspace, cookie, fields(workspace, before, "revalidate"))[0] == 303
    step = workspace.app.runner.get(before.book_id).step("notes")
    assert step.state == "done" and "exceeded the review limits" in step.note
    body = request(workspace, f"/books/{before.book_id}?step=preview", headers={"Cookie": cookie})[
        2
    ]
    assert "The last run stopped here" not in body
