import http.client
import threading
from pathlib import Path
from urllib.parse import urlencode, urlsplit

import pytest

from emend.inspect import sha256_file
from emend.pipeline import get_report, ingest
from emend.publish import publish
from emend.reading_review import reading_review
from emend.repairs import prepare_repairs
from emend.review_ui import ReviewServer
from emend.storage import Store
from tests.epub_factory import book_files, write_epub
from tests.test_curation import setup_book


@pytest.fixture
def workspace(config):
    with ReviewServer(config, 0) as server:
        thread = threading.Thread(
            target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
        )
        thread.start()
        yield server
        server.shutdown()
        thread.join(timeout=3)


def request(server, path="/", method="GET", body=None, headers=None):
    connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=30)
    connection.request(method, path, body=body, headers=headers or {})
    response = connection.getresponse()
    data = response.read().decode()
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
    return request(
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
    status, headers, body = request(
        workspace, "/books/" + before.book_id, headers={"Cookie": cookie}
    )
    assert status == 200 and "The Quiet Observatory" in body
    assert "default-src 'none'" in headers["Content-Security-Policy"]
    assert headers["Referrer-Policy"] == "same-origin"
    assert get_report(Store(config.root), before.book_id) == before
    assert Store(config.root).resolve(before.book_id) == row


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
    assert get_report(Store(config.root), before.book_id) == before


def test_unicode_and_duplicate_entry_tokens_fail_cleanly(workspace):
    assert request(workspace, "/?token=%C3%A9")[0] == 401
    assert request(workspace, "/?token=a&token=b")[0] == 400


def test_post_revalidation_refreshes_quality_evidence(workspace, tmp_path, config):
    before = ingest(write_epub(tmp_path / "book.epub"), config)
    status, headers, _ = post(workspace, login(workspace), fields(workspace, before, "revalidate"))
    assert status == 303 and headers["Location"] == "/books/" + before.book_id
    after = get_report(Store(config.root), before.book_id)
    assert after.state == "VALIDATED" and after.revalidation
    assert after.artifact_sha256 == before.artifact_sha256


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
    assert "Reading review" not in headers.get("Location", "")
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
    for url in ["/", "/books/" + before.book_id]:
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


def test_publication_rechecks_expected_artifact_under_operation_lock(
    tmp_path, config, fake_publishers
):
    before = ingest(write_epub(tmp_path / "book.epub"), config)
    with pytest.raises(ValueError, match="stale"):
        publish(before.book_id, ["calibre"], config, expected_artifact_sha256="0" * 64)
    assert not (config.publish.calibre_library / "fake-published.json").exists()


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
        page.goto(workspace.entry_url)
        assert page.get_by_role("heading", name="Your library", exact=True).is_visible()
        page.get_by_label("Choose an EPUB").set_input_files(source)
        page.get_by_role("button", name="Import EPUB", exact=True).click()
        assert page.get_by_role("heading", name="The Quiet Observatory", exact=True).is_visible(), (
            events,
            page.content(),
        )
        book = Store(config.root).books()[0]
        assert book["id"] == sha256_file(source)
        page.locator("summary").filter(has_text="Fonts and footnotes").click()
        page.get_by_role("button", name="Check fonts and footnotes", exact=True).click()
        page.get_by_text("View font and footnote report", exact=True).click()
        frame = page.frame_locator("iframe")
        assert frame.get_by_role("heading", name="The Quiet Observatory", exact=True).is_visible()
        for width in (1280, 390):
            page.set_viewport_size({"width": width, "height": 1000})
            assert page.evaluate("document.documentElement.scrollWidth") == width
        browser.close()


def test_typography_actions_use_complete_current_evidence(
    workspace, tmp_path, config, fake_capture
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
    assert result[0] == 303
    trial = Store(config.root).document(before.book_id, "typography-trial")
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
        page.goto(f"http://127.0.0.1:{workspace.server_port}/books/{before.book_id}")
        page.locator("summary").filter(has_text="Book details and cover").click()
        page.get_by_label("JPEG or PNG cover, up to 20 MiB").set_input_files(image)
        page.get_by_label("Source/provenance note").fill(
            "Original synthetic fixture, explicitly supplied for this edition"
        )
        page.get_by_role("button", name="Compare local cover", exact=True).click()
        review = load_cover_review(Store(browser_config.root), before.book_id)
        assert review.candidates
        assert get_report(Store(browser_config.root), before.book_id) == before
        page.locator('input[name="candidate"]').first.check()
        page.get_by_role("button", name="Apply reviewed cover", exact=True).click()
        assert get_report(Store(browser_config.root), before.book_id).cover_changes
        browser.close()


@pytest.mark.parametrize("archive_limit, displayed", [(256, 128), (1, 1)])
def test_upload_limit_display_and_request_bound(workspace, config, archive_limit, displayed):
    config.limits.max_archive_mb = archive_limit
    cookie = login(workspace)
    assert f"Up to {displayed} MiB here." in request(workspace, headers={"Cookie": cookie})[2]
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
def test_epub_upload_file_boundary(workspace, config, tmp_path, over_limit):
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
    data = source.read_bytes() + (b"x" if over_limit else b"")
    boundary = "emend-upload-boundary"
    body = (
        (
            f'--{boundary}\r\nContent-Disposition: form-data; name="csrf"\r\n\r\n'
            f"{workspace.app.csrf}\r\n--{boundary}\r\n"
            'Content-Disposition: form-data; name="action"\r\n\r\nimport\r\n'
            f'--{boundary}\r\nContent-Disposition: form-data; name="epub"; '
            'filename="boundary.epub"\r\nContent-Type: application/epub+zip\r\n\r\n'
        ).encode()
        + data
        + f"\r\n--{boundary}--\r\n".encode()
    )
    status, _, response = request(
        workspace,
        "/action",
        "POST",
        body,
        {
            "Cookie": login(workspace),
            "Origin": f"http://127.0.0.1:{workspace.server_port}",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        },
    )
    if over_limit:
        assert status == 400 and "EPUB exceeds the upload size limit" in response
        assert not Store(config.root).books()
    else:
        assert status == 303, response
        original = config.root / "originals" / f"{sha256_file(source)}.epub"
        assert original.read_bytes() == source.read_bytes()
