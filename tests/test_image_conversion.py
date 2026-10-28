"""Original in-memory images exercise checked conversion, never private books."""

import copy
import io
import json
import os
import random
import zipfile
from pathlib import Path

import pytest
from lxml.etree import XMLSyntaxError
from PIL import Image, ImageCms
from typer.testing import CliRunner

from emend.cli import app
from emend.image_conversion import (
    ImageConversionRejectedError,
    apply_image_conversion,
    discover,
    image_conversion_trial,
    load_image_conversion_trial,
    preserved,
    rewrite,
)
from emend.inspect import NS, inspect_epub, parse_xml, sha256_file
from emend.pipeline import get_report, ingest
from emend.review_guidance import reader_notes
from emend.storage import Store
from tests.epub_factory import book_files, write_epub


def webp(alpha=False, lossless=False, animated=False):
    mode = "RGBA" if alpha else "RGB"
    image = Image.frombytes(mode, (64, 96), random.Random(7).randbytes(64 * 96 * len(mode)))
    out = io.BytesIO()
    options = {
        "lossless": lossless,
        "icc_profile": ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes(),
    }
    if animated:
        options.update(
            save_all=True, append_images=[Image.new(mode, image.size, "red")], duration=100, loop=0
        )
    image.save(out, "WEBP", **options)
    return out.getvalue()


def files_with(version="3.0", *, rich=False, alpha=False, lossless=False):
    files = book_files(version)
    files.pop("EPUB/observatory.png")
    files["EPUB/images/scene one.webp"] = webp(alpha, lossless)
    files["EPUB/package.opf"] = files["EPUB/package.opf"].replace(
        b'href="observatory.png" media-type="image/png"',
        b'href="images/scene%20one.webp" media-type="image/webp"'
        + (b' properties="cover-image"' if version == "3.0" else b""),
    )
    files["EPUB/package.opf"] = files["EPUB/package.opf"].replace(
        b"</metadata>", b'<meta name="cover" content="image"/></metadata>'
    )
    files["EPUB/chapter.xhtml"] = files["EPUB/chapter.xhtml"].replace(
        b"observatory.png", b"images/scene%20one.webp"
    )
    files["EPUB/style.css"] += b" body { font-size:.8em }"
    if rich:
        files["EPUB/style.css"] += (
            b" /* keep formatting */\nfigure { background-image: url('images/scene%20one.webp'); color: red }"
        )
        files["EPUB/chapter.xhtml"] = files["EPUB/chapter.xhtml"].replace(
            b"<p><img", b'<p style="background:url(images/scene%20one.webp)"><img'
        )
        files["EPUB/chapter.xhtml"] = files["EPUB/chapter.xhtml"].replace(
            b"</body>",
            b'<svg xmlns="http://www.w3.org/2000/svg" width="64" height="96"><image href="images/scene%20one.webp#xywh=0,0,64,96" width="64" height="96"/></svg></body>',
        )
        files["EPUB/art/wrap.svg"] = (
            b'<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" width="64" height="96"><image xlink:href="../images/scene%20one.webp" width="64" height="96"/></svg>'
        )
        files["EPUB/package.opf"] = files["EPUB/package.opf"].replace(
            b"</manifest>",
            b'<item id="wrapper" href="art/wrap.svg" media-type="image/svg+xml" fallback="image"/></manifest>',
        )
    return files


@pytest.fixture
def capture(monkeypatch, fake_pages):
    import emend.visual

    monkeypatch.setattr("emend.image_conversion.render", emend.visual.render)


def book(tmp_path, config, **options):
    path = write_epub(tmp_path / "images.epub", files_with(**options))
    return path, ingest(path, config)


@pytest.mark.parametrize("version", ["2.0", "3.0"])
@pytest.mark.parametrize("kind", ["alpha", "lossless", "photo"])
def test_discover_rewrite_preserve_pixels_references_and_cover(tmp_path, config, version, kind):
    path, before = book(
        tmp_path,
        config,
        version=version,
        rich=True,
        alpha=kind == "alpha",
        lossless=kind == "lossless",
    )
    plan = discover(path, before.inspection)
    assert len(plan.images) == 1 and len(plan.skipped) == 1
    image = plan.images[0]
    assert image.manifest_id == "image" and image.width == 64 and image.height == 96
    assert image.new_href.endswith(".jpg" if kind == "photo" else ".png")
    assert {"css", "style", "attribute", "fallback"} <= {r.kind for r in plan.references}
    target = tmp_path / "converted.epub"
    rewrite(path, target, before.inspection, plan)
    assert preserved(path, target, before.inspection, plan)
    after = inspect_epub(target, config.limits)
    assert not after.blocking and after.spine == before.inspection.spine
    assert after.text_sha256 == before.inspection.text_sha256
    with zipfile.ZipFile(path) as old, zipfile.ZipFile(target) as new:
        assert new.namelist() == [
            image.new_path if n == image.old_path else n for n in old.namelist()
        ]
        assert (
            new.infolist()[0].filename == "mimetype"
            and new.infolist()[0].compress_type == zipfile.ZIP_STORED
        )
        package = parse_xml(new.read("EPUB/package.opf"))
        item = package.xpath("opf:manifest/opf:item[@id='image']", namespaces=NS)[0]
        assert item.get("href") == image.new_href
        assert package.xpath("opf:metadata/opf:meta[@name='cover']/@content", namespaces=NS) == [
            "image"
        ]
        if version == "3.0":
            assert item.get("properties") == "cover-image"
        for name in ("EPUB/notes.xhtml", "EPUB/nav.xhtml" if version == "3.0" else "EPUB/toc.ncx"):
            assert old.read(name) == new.read(name)
        converted = Image.open(io.BytesIO(new.read(image.new_path)))
        source = Image.open(io.BytesIO(old.read(image.old_path)))
        assert converted.info.get("icc_profile") == source.info.get("icc_profile")
        assert not converted.info.get("progressive")
        if kind != "photo":
            assert new.read(image.new_path)[28] == 0
        else:
            assert image.psnr is None or image.psnr >= 40
    assert not any("WebP" in note for note in reader_notes(target, after))


def test_srcset_is_resolved_and_preserved(tmp_path, config):
    files = files_with()
    files["EPUB/chapter.xhtml"] = files["EPUB/chapter.xhtml"].replace(
        b"<img src=", b'<img srcset="images/scene%20one.webp 1x, images/scene%20one.webp 2x" src='
    )
    path = write_epub(tmp_path / "srcset.epub", files)
    inspection = inspect_epub(path, config.limits)
    assert not inspection.blocking
    plan = discover(path, inspection)
    assert any(r.kind == "srcset" and ".jpg 2x" in r.after for r in plan.references)
    target = tmp_path / "out.epub"
    rewrite(path, target, inspection, plan)
    assert preserved(path, target, inspection, plan)


def test_animation_vector_and_corrupt_images_are_reported_not_converted(tmp_path, config):
    files = files_with(rich=True)
    files["EPUB/animated.webp"] = webp(animated=True)
    files["EPUB/corrupt.webp"] = b"not a raster image"
    files["EPUB/package.opf"] = files["EPUB/package.opf"].replace(
        b"</manifest>",
        b'<item id="anim" href="animated.webp" media-type="image/webp"/><item id="bad" href="corrupt.webp" media-type="image/webp"/></manifest>',
    )
    path = write_epub(tmp_path / "skipped.epub", files)
    before = ingest(path, config)
    plan = discover(path, before.inspection)
    assert len(plan.images) == 1 and len(plan.skipped) == 3
    assert any("Animated" in s.reason for s in plan.skipped)
    assert any("SVG" in s.reason for s in plan.skipped)
    with pytest.raises(ImageConversionRejectedError):
        image_conversion_trial(before.book_id, config, layout="scroll")
    assert get_report(Store(config.root), before.book_id) == before
    assert list((config.root / "image-conversion" / before.book_id).glob("*/failure.txt"))


def test_collision_uses_deterministic_unused_path(tmp_path, config):
    files = files_with()
    files["EPUB/images/scene one.jpg"] = b"untouched existing bytes"
    p = write_epub(tmp_path / "collision.epub", files)
    plan = discover(p, inspect_epub(p, config.limits))
    assert plan.images[0].new_path == "EPUB/images/scene one.emend-1.jpg"
    assert discover(p, inspect_epub(p, config.limits)) == plan


@pytest.mark.parametrize("case", ["script", "attribute", "malformed", "fixed", "css"])
def test_unrewritable_document_refuses_whole_plan(tmp_path, config, case):
    files = files_with()
    if case == "script":
        files["EPUB/chapter.xhtml"] = files["EPUB/chapter.xhtml"].replace(
            b"</body>", b"<script>ignored()</script></body>"
        )
    elif case == "attribute":
        files["EPUB/chapter.xhtml"] = files["EPUB/chapter.xhtml"].replace(
            b"<body>", b'<body data-image="images/scene%20one.webp">'
        )
    elif case == "malformed":
        files["EPUB/unused.xml"] = b"<broken"
    elif case == "fixed":
        files["EPUB/package.opf"] = files["EPUB/package.opf"].replace(
            b"</metadata>", b'<meta property="rendition:layout">pre-paginated</meta></metadata>'
        )
    else:
        files["EPUB/style.css"] += b'p { background:url("unterminated) }'
    p = write_epub(tmp_path / "unsafe.epub", files)
    with pytest.raises((ValueError, XMLSyntaxError)):
        discover(p, inspect_epub(p, config.limits))


@pytest.mark.parametrize("mutation", ["prose", "css", "opf", "pixels", "extra"])
def test_independent_check_refuses_unapproved_changes(tmp_path, config, mutation):
    p, before = book(tmp_path, config, rich=True, lossless=True)
    plan = discover(p, before.inspection)
    target = tmp_path / "candidate.epub"
    rewrite(p, target, before.inspection, plan)
    with zipfile.ZipFile(target) as archive:
        infos = archive.infolist()
        data = {i.filename: archive.read(i) for i in infos}
    if mutation == "prose":
        data["EPUB/chapter.xhtml"] = data["EPUB/chapter.xhtml"].replace(b"At dusk", b"At dawn")
    elif mutation == "css":
        data["EPUB/style.css"] = data["EPUB/style.css"].replace(b"color: red", b"color: tan")
    elif mutation == "opf":
        data["EPUB/package.opf"] = data["EPUB/package.opf"].replace(
            b"<dc:title>", b"<dc:title>Unapproved "
        )
    elif mutation == "pixels":
        image = plan.images[0]
        im = Image.open(io.BytesIO(data[image.new_path]))
        im.putpixel((0, 0), (255, 0, 255))
        out = io.BytesIO()
        im.save(out, "PNG")
        data[image.new_path] = out.getvalue()
        # A forged expected byte hash must not defeat the separate pixel comparison.
        import hashlib

        plan.images[0].new_sha256 = hashlib.sha256(out.getvalue()).hexdigest()
    with zipfile.ZipFile(target, "w") as archive:
        for i in infos:
            archive.writestr(copy.copy(i), data[i.filename])
        if mutation == "extra":
            archive.writestr("unplanned.bin", b"new")
    assert not preserved(p, target, before.inspection, plan)


@pytest.mark.parametrize("version", ["2.0", "3.0"])
def test_trial_apply_and_history_after_text_size(tmp_path, config, capture, monkeypatch, version):
    p, before = book(tmp_path, config, version=version)
    trial = image_conversion_trial(before.book_id, config, layout="scroll")
    assert trial.status == "complete" and get_report(Store(config.root), before.book_id) == before
    assert load_image_conversion_trial(Store(config.root), before.book_id, trial.id) == trial
    with pytest.raises(ValueError, match="approve"):
        apply_image_conversion(before.book_id, config, trial_id=trial.id, approve=False)
    after = apply_image_conversion(before.book_id, config, trial_id=trial.id, approve=True)
    assert after.state == "VALIDATED" and len(after.image_conversions) == 1
    assert apply_image_conversion(before.book_id, config, trial_id=trial.id, approve=True) == after
    import emend.visual
    from emend.text_size import apply_text_size, text_size_trial

    monkeypatch.setattr("emend.text_size.render", emend.visual.render)
    size = text_size_trial(before.book_id, config, layout="scroll")
    sized = apply_text_size(before.book_id, config, trial_id=size.id, approve=True)
    assert sized.image_conversions == after.image_conversions
    from emend.revalidation import revalidate

    assert revalidate(before.book_id, config).image_conversions == after.image_conversions
    assert sha256_file(p) == before.book_id


@pytest.mark.parametrize(
    "failure", ["validator", "database", "candidate", "screenshot", "html", "policy"]
)
def test_apply_failure_leaves_current_artifact_unchanged(
    tmp_path, config, capture, monkeypatch, failure
):
    _, before = book(tmp_path, config)
    trial = image_conversion_trial(before.book_id, config, layout="scroll")
    if failure == "validator":
        config.tools.epubcheck.write_text(
            config.tools.epubcheck.read_text().replace(
                "'messages': []", "'messages': [{'severity':'ERROR'}]"
            )
        )
    elif failure == "database":
        monkeypatch.setattr(
            Store, "transition", lambda *a, **k: (_ for _ in ()).throw(OSError("database failed"))
        )
    elif failure == "candidate":
        monkeypatch.setattr(
            "emend.image_conversion.rewrite", lambda s, t, *a: t.write_bytes(b"broken")
        )
    elif failure in {"screenshot", "html"}:
        path = Path(
            Store(config.root).document(before.book_id, "image-conversion-trial", trial.id)["path"]
        )
        target = (
            path.with_suffix(".html")
            if failure == "html"
            else path.parent / "before" / trial.before[0].screenshots[0].file
        )
        target.write_bytes(b"tampered")
    else:
        config.quality.require_accessibility_audit = True
    with pytest.raises((ValueError, OSError, zipfile.BadZipFile)):
        apply_image_conversion(before.book_id, config, trial_id=trial.id, approve=True)
    assert get_report(Store(config.root), before.book_id) == before
    assert sha256_file(config.root / "curated" / f"{before.book_id}.epub") == before.artifact_sha256


def test_workspace_post_is_explicit_hash_verified_and_no_get_mutation(
    workspace, tmp_path, config, capture, fake_pages
):
    from tests.test_review_ui import fields, login, post, request

    _, before = book(tmp_path, config)
    cookie = login(workspace)
    page = f"/books/{before.book_id}?step=refine"
    body = request(workspace, page, headers={"Cookie": cookie})[2]
    assert "Compare image conversion" in body
    assert get_report(Store(config.root), before.book_id) == before
    with Store(config.root).db() as db:
        assert (
            db.execute(
                "SELECT COUNT(*) FROM review_documents WHERE book_id=? AND kind='image-conversion-trial'",
                (before.book_id,),
            ).fetchone()[0]
            == 0
        )
    f = fields(workspace, before, "image-conversion-trial", layout="scroll")
    f["csrf"] = "bad"
    assert post(workspace, cookie, f)[0] == 403
    assert (
        post(
            workspace, cookie, fields(workspace, before, "image-conversion-trial", layout="scroll")
        )[0]
        == 303
    )
    body = request(workspace, page, headers={"Cookie": cookie})[2]
    assert "Apply image conversion" in body and "/pages/image-conversion-trial/" in body
    trial = Store(config.root).document(before.book_id, "image-conversion-trial")["id"]
    assert (
        post(workspace, cookie, fields(workspace, before, "apply-image-conversion", trial=trial))[0]
        == 303
    )
    after = get_report(Store(config.root), before.book_id)
    assert after.image_conversions and after.state == "VALIDATED"
    body = request(workspace, page, headers={"Cookie": cookie})[2]
    assert "Return to the baseline" in body
    preview = request(
        workspace, f"/books/{before.book_id}?step=preview", headers={"Cookie": cookie}
    )[2]
    assert "1 WebP" not in preview and "On your X4 Pro" not in preview
    row = Store(config.root).resolve(before.book_id)
    assert (
        post(
            workspace,
            cookie,
            fields(
                workspace,
                after,
                "restore-baseline",
                revision=before.artifact_sha256,
                expected_report=row["report_path"],
            ),
        )[0]
        == 303
    )
    restored = get_report(Store(config.root), before.book_id)
    assert restored.image_conversions == after.image_conversions
    assert restored.artifact_sha256 == before.artifact_sha256
    body = request(workspace, page, headers={"Cookie": cookie})[2]
    assert "Apply image conversion" in body and "Images converted" not in body


def test_cli_plan_apply(tmp_path, config, capture):
    import yaml

    _, before = book(tmp_path, config)
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(config.model_dump(mode="json")))
    c = ["-c", str(path)]
    runner = CliRunner()
    r = runner.invoke(
        app, ["image-conversion-trial", before.book_id, "--layout", "scroll", "--json", *c]
    )
    assert r.exit_code == 0, r.output
    trial = json.loads(r.output)
    r = runner.invoke(
        app, ["apply-image-conversion", before.book_id, "--trial", trial["id"], "--approve", *c]
    )
    assert r.exit_code == 0, r.output


@pytest.mark.parametrize("version", ["2.0", "3.0"])
def test_epubcheck_accepts_converted_book(tmp_path, config, capture, version):
    jar = os.environ.get("EMEND_TEST_EPUBCHECK_JAR")
    if not jar:
        pytest.skip("Set EMEND_TEST_EPUBCHECK_JAR for official validation")
    config.tools.epubcheck_jar = Path(jar)
    _, before = book(tmp_path, config, version=version)
    trial = image_conversion_trial(before.book_id, config, layout="scroll")
    after = apply_image_conversion(before.book_id, config, trial_id=trial.id, approve=True)
    assert after.state == "VALIDATED"
    assert next(g for g in after.gates if g.name == "epubcheck").status == "passed"


def test_manifest_href_spelling_is_preserved_except_extension(tmp_path, config):
    files = files_with(lossless=True)
    files["EPUB/package.opf"] = files["EPUB/package.opf"].replace(
        b'href="images/scene%20one.webp"', b'href="./images/scene%20one.webp"'
    )
    p = write_epub(tmp_path / "encoded.epub", files)
    plan = discover(p, inspect_epub(p, config.limits))
    assert plan.images[0].new_href == "./images/scene%20one.png"


@pytest.mark.parametrize("limit", ["MAX_BYTES", "MAX_PIXELS", "MAX_IMAGES", "MAX_REFS"])
def test_limits_leave_original_intact(tmp_path, config, monkeypatch, limit):
    p, before = book(tmp_path, config, rich=True)
    monkeypatch.setattr("emend.image_conversion." + limit, 1)
    if limit in {"MAX_IMAGES", "MAX_REFS"}:
        with pytest.raises(ValueError):
            discover(p, before.inspection)
    else:
        plan = discover(p, before.inspection)
        assert not plan.images and plan.skipped
    assert sha256_file(p) == before.book_id


def test_metadata_curation_preserves_conversion_history(tmp_path, config, capture):
    from emend.curation import apply_proposals
    from emend.research.service import research
    from tests.test_research import ISBN, evidence_client

    files = files_with()
    files["EPUB/package.opf"] = files["EPUB/package.opf"].replace(
        b"</metadata>", f"<dc:identifier>urn:isbn:{ISBN}</dc:identifier></metadata>".encode()
    )
    before = ingest(write_epub(tmp_path / "metadata.epub", files), config)
    trial = image_conversion_trial(before.book_id, config, layout="scroll")
    after = apply_image_conversion(before.book_id, config, trial_id=trial.id, approve=True)
    research(before.book_id, config, client=evidence_client(tmp_path))
    edited = apply_proposals(before.book_id, config, safe=True)
    assert edited.image_conversions == after.image_conversions


def test_reader_notes_keeps_skipped_images_and_size_warnings(tmp_path, config):
    files = files_with()
    large = io.BytesIO()
    Image.new("RGB", (2100, 10), "green").save(large, "PNG")
    files["EPUB/oversize.png"] = large.getvalue()
    files["EPUB/animated.webp"] = webp(animated=True)
    files["EPUB/package.opf"] = files["EPUB/package.opf"].replace(
        b"</manifest>",
        b'<item id="large" href="oversize.png" media-type="image/png"/><item id="anim" href="animated.webp" media-type="image/webp"/></manifest>',
    )
    p = write_epub(tmp_path / "advisory.epub", files)
    before = inspect_epub(p, config.limits)
    plan = discover(p, before)
    out = tmp_path / "converted.epub"
    rewrite(p, out, before, plan)
    notes = reader_notes(out, inspect_epub(out, config.limits))
    assert any("(1 WebP)" in n for n in notes)
    assert any("source limits" in n for n in notes)


def test_browser_conversion_with_javascript_disabled(workspace, tmp_path, browser_config):
    from playwright.sync_api import sync_playwright

    p, before = book(tmp_path, browser_config)
    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            executable_path=str(browser_config.tools.chromium)
            if browser_config.tools.chromium
            else None
        )
        page = browser.new_page(java_script_enabled=False, viewport={"width": 1280, "height": 1000})
        page.goto(workspace.entry_url)
        url = f"http://127.0.0.1:{workspace.server_port}/books/{before.book_id}?step=refine"
        page.goto(url)
        page.get_by_role("button", name="Compare image conversion", exact=True).click()
        workspace.app.runner.wait(120)
        page.goto(url)
        assert page.get_by_role("button", name="Apply image conversion", exact=True).is_visible()
        for width in (1280, 390):
            page.set_viewport_size({"width": width, "height": 1000})
            page.evaluate("document.fonts.ready")
            assert page.evaluate("document.documentElement.scrollWidth") == width
            page.screenshot(path=str(tmp_path / f"image-comparison-{width}.png"), full_page=True)
        assert page.locator("img").count() > 0
        for img in page.locator("img").all():
            img.scroll_into_view_if_needed()
            assert img.evaluate("(e)=>e.complete && e.naturalWidth>0")
        page.get_by_role("button", name="Apply image conversion", exact=True).click()
        workspace.app.runner.wait(120)
        page.goto(url)
        assert page.get_by_text("Images converted", exact=True).first.is_visible()
        after = get_report(Store(browser_config.root), before.book_id)
        assert after.image_conversions
        with page.expect_download() as event:
            page.get_by_role("link", name="Download EPUB", exact=True).first.click()
        dest = tmp_path / "download.epub"
        event.value.save_as(dest)
        assert sha256_file(dest) == after.artifact_sha256
        assert sha256_file(p) == before.book_id
        browser.close()


def test_text_attributes_are_not_mistaken_for_image_references(tmp_path, config):
    files = files_with()
    files["EPUB/chapter.xhtml"] = files["EPUB/chapter.xhtml"].replace(
        b'alt="A simple green illustration of the observatory"', b'alt="images/scene%20one.webp"'
    )
    path = write_epub(tmp_path / "alt.epub", files)
    plan = discover(path, inspect_epub(path, config.limits))
    assert plan.images and plan.images[0].reference_indices
    target = tmp_path / "alt-converted.epub"
    rewrite(path, target, inspect_epub(path, config.limits), plan)
    assert preserved(path, target, inspect_epub(path, config.limits), plan)
    with zipfile.ZipFile(target) as z:
        assert b'alt="images/scene%20one.webp"' in z.read("EPUB/chapter.xhtml")


def test_unrelated_css_url_bytes_are_preserved():
    from emend.image_references import css_preserved, css_rewrite

    def resolve(value):
        return "converted.png" if value == "source.webp" else value

    before = "a {background:url(source.webp)} b {background:url('unrelated.png')}"
    after, _ = css_rewrite(before, resolve)
    assert css_preserved(before, after, resolve)
    assert not css_preserved(
        before, after.replace("url('unrelated.png')", 'url("unrelated.png")'), resolve
    )
    with pytest.raises(ValueError, match="outside url"):
        css_rewrite('a{background:image-set("source.webp" 1x)}', resolve)


def test_many_images_remain_one_complete_checked_plan(tmp_path, config, capture):
    files = files_with(lossless=True)
    asset = files["EPUB/images/scene one.webp"]
    manifest = []
    body = []
    for n in range(1, 327):
        name = f"images/illustration-{n}.webp"
        files["EPUB/" + name] = asset
        manifest.append(f'<item id="img-{n}" href="{name}" media-type="image/webp"/>')
        body.append(f'<p><img src="{name}" alt="Generated illustration {n}"/></p>')
    files["EPUB/package.opf"] = files["EPUB/package.opf"].replace(
        b"</manifest>", "".join(manifest).encode() + b"</manifest>"
    )
    files["EPUB/chapter.xhtml"] = files["EPUB/chapter.xhtml"].replace(
        b"</body>", "".join(body).encode() + b"</body>"
    )
    before = ingest(write_epub(tmp_path / "generated-many.epub", files), config)
    trial = image_conversion_trial(before.book_id, config, layout="scroll")
    assert len(trial.plan.images) == 327
    assert all(i.reference_indices for i in trial.plan.images)
    after = apply_image_conversion(before.book_id, config, trial_id=trial.id, approve=True)
    assert len(after.image_conversions[-1].images) == 327
    assert after.inspection.text_sha256 == before.inspection.text_sha256


@pytest.mark.parametrize("mode", ["L", "P"])
def test_existing_grayscale_or_palette_sources_keep_decoded_pixels(tmp_path, config, mode):
    source = Image.new(mode, (32, 32), 1)
    if mode == "P":
        source.putpalette([v for n in range(256) for v in (n, 255 - n, n // 2)])
    out = io.BytesIO()
    source.save(out, "PNG")
    files = files_with()
    files["EPUB/images/scene one.webp"] = out.getvalue()
    p = write_epub(tmp_path / "colour.epub", files)
    inspection = inspect_epub(p, config.limits)
    plan = discover(p, inspection)
    candidate = tmp_path / "out.epub"
    rewrite(p, candidate, inspection, plan)
    assert plan.images[0].new_media_type == "image/png"
    assert preserved(p, candidate, inspection, plan)
    if mode == "L":
        with zipfile.ZipFile(candidate) as archive:
            assert Image.open(io.BytesIO(archive.read(plan.images[0].new_path))).mode == "L"


def test_cover_id_can_equal_its_filename_without_being_rewritten(tmp_path, config):
    files = files_with()
    files["EPUB/cover.webp"] = files.pop("EPUB/images/scene one.webp")
    files["EPUB/package.opf"] = (
        files["EPUB/package.opf"]
        .replace(b"images/scene%20one.webp", b"cover.webp")
        .replace(b'id="image"', b'id="cover.webp"')
        .replace(b'content="image"', b'content="cover.webp"')
    )
    files["EPUB/chapter.xhtml"] = files["EPUB/chapter.xhtml"].replace(
        b"images/scene%20one.webp", b"cover.webp"
    )
    p = write_epub(tmp_path / "cover.epub", files)
    before = inspect_epub(p, config.limits)
    plan = discover(p, before)
    out = tmp_path / "out.epub"
    rewrite(p, out, before, plan)
    assert plan.images[0].manifest_id == "cover.webp" and preserved(p, out, before, plan)
    with zipfile.ZipFile(out) as archive:
        package = parse_xml(archive.read("EPUB/package.opf"))
        assert package.xpath("opf:metadata/opf:meta[@name='cover']/@content", namespaces=NS) == [
            "cover.webp"
        ]


def test_rtl_comparison_uses_scrolling_without_changing_reading_order(tmp_path, config, capture):
    files = files_with()
    files["EPUB/package.opf"] = files["EPUB/package.opf"].replace(
        b"<spine>", b'<spine page-progression-direction="rtl">'
    )
    before = ingest(write_epub(tmp_path / "rtl.epub", files), config)
    with pytest.raises(ValueError, match="layout scroll"):
        image_conversion_trial(before.book_id, config)
    trial = image_conversion_trial(before.book_id, config, layout="scroll")
    after = apply_image_conversion(before.book_id, config, trial_id=trial.id, approve=True)
    assert after.inspection.spine == before.inspection.spine


@pytest.mark.parametrize(
    "fmt,extension,media,warns",
    [("PNG", ".png", "image/png", True), ("JPEG", ".jpg", "image/jpeg", False)],
)
def test_sleep_pixel_advisory_is_specific_to_png(tmp_path, config, fmt, extension, media, warns):
    image = Image.new("RGB", (1700, 1900), "#456789")
    out = io.BytesIO()
    image.save(out, fmt)
    files = book_files()
    files.pop("EPUB/observatory.png")
    files["EPUB/cover" + extension] = out.getvalue()
    files["EPUB/package.opf"] = files["EPUB/package.opf"].replace(
        b'href="observatory.png" media-type="image/png"',
        f'href="cover{extension}" media-type="{media}" properties="cover-image"'.encode(),
    )
    files["EPUB/chapter.xhtml"] = files["EPUB/chapter.xhtml"].replace(
        b"observatory.png", f"cover{extension}".encode()
    )
    path = write_epub(tmp_path / "limit.epub", files)
    notes = reader_notes(path, inspect_epub(path, config.limits))
    assert any("source limits" in n for n in notes) == warns
