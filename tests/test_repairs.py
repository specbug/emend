import copy
import json
import os
import zipfile
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from emend.cli import app
from emend.curation import apply_proposals
from emend.inspect import inspect_epub, sha256_file
from emend.pipeline import get_report, ingest
from emend.repairs import ACTION, apply_repairs, prepare_repairs
from emend.reports import write_reports
from emend.research.service import research
from emend.revalidation import revalidate
from emend.storage import Store
from tests.epub_factory import book_files
from tests.test_research import ISBN, evidence_client


def damaged_epub(path, version="3.0", *, compressed=False, extra=False, files=None):
    files = files or book_files(version)
    with zipfile.ZipFile(path, "w") as archive:
        archive.comment = b"Publisher archive comment"
        names = list(files)
        if not compressed and "mimetype" in files:
            names = [n for n in names if n != "mimetype"] + ["mimetype"]
        for name in names:
            info = zipfile.ZipInfo(name, date_time=(2020, 2, 3, 4, 5, 6))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.comment = b"retained entry comment"
            if name == "mimetype":
                info.compress_type = zipfile.ZIP_DEFLATED if compressed else zipfile.ZIP_STORED
                if extra:
                    info.extra = b"\xfe\xca\x00\x00"
            archive.writestr(info, files[name])
    return path


def setup(tmp_path, config, **kwargs):
    source = damaged_epub(tmp_path / "book.epub", **kwargs)
    before = ingest(source, config)
    assert before.state == "FAILED"
    plan, review = prepare_repairs(before.book_id, config)
    return source, before, plan, review


def apply(plan, config, **kwargs):
    return apply_repairs(plan.book_id, config, plan_id=plan.id, approve=[ACTION], **kwargs)


@pytest.mark.parametrize("version", ["2.0", "3.0"])
@pytest.mark.parametrize("compressed", [False, True])
def test_explicit_repair_preserves_every_resource_and_is_retryable(
    tmp_path, config, version, compressed
):
    files = book_files(version)
    files["unused.bin"] = b"unmanifested data stays intact"
    files["empty/"] = b""
    source, before, plan, review = setup(
        tmp_path, config, version=version, compressed=compressed, extra=True, files=files
    )
    assert plan.actions == [ACTION] and review.is_file()
    assert get_report(Store(config.root), before.book_id) == before
    assert not Store(config.root).resolve(before.book_id)["artifact_path"]
    after = apply(plan, config)
    assert after.state == "VALIDATED" and after.artifact_sha256 != before.book_id
    assert after.inspection.resource_hashes == before.inspection.resource_hashes
    assert after.inspection.spine_text_sha256 == before.inspection.spine_text_sha256
    assert after.repair_changes[0].before_sha256 == before.book_id
    artifact = config.root / "curated" / f"{before.book_id}.epub"
    with zipfile.ZipFile(source) as old, zipfile.ZipFile(artifact) as new:
        assert new.infolist()[0].filename == "mimetype"
        assert new.infolist()[0].compress_type == zipfile.ZIP_STORED
        assert new.infolist()[0].extra == b""
        assert old.comment == new.comment
        assert set(old.namelist()) == set(new.namelist())
        for name in old.namelist():
            assert old.read(name) == new.read(name)
    assert sha256_file(source) == before.book_id
    assert (
        sha256_file(config.root / "originals" / source.with_name(f"{before.book_id}.epub").name)
        == before.book_id
    )
    versions = config.root / "versions" / before.book_id
    assert {p.stem for p in versions.glob("*.epub")} == {before.book_id, after.artifact_sha256}
    assert apply(plan, config) == after
    assert revalidate(before.book_id, config).repair_changes == after.repair_changes


@pytest.mark.parametrize("approve", [[], ["unknown"], [ACTION, "unknown"]])
def test_no_implicit_or_unknown_approval(tmp_path, config, approve):
    _, before, plan, _ = setup(tmp_path, config)
    with pytest.raises(ValueError, match="Explicitly approve"):
        apply_repairs(before.book_id, config, plan_id=plan.id, approve=approve)
    assert get_report(Store(config.root), before.book_id) == before


@pytest.mark.parametrize("defect", ["payload", "missing", "duplicate", "path", "xml", "limit"])
def test_unsupported_damage_is_not_offered(tmp_path, config, defect):
    files = book_files()
    if defect == "payload":
        files["mimetype"] += b"\n"
    elif defect == "missing":
        del files["mimetype"]
    elif defect == "path":
        files["../escape"] = b"bad"
    elif defect == "xml":
        files["EPUB/chapter.xhtml"] = b"<broken"
    elif defect == "limit":
        config.limits.max_members = 1
    source = damaged_epub(tmp_path / "bad.epub", files=files)
    if defect == "duplicate":
        with zipfile.ZipFile(source, "a") as archive:
            with pytest.warns(UserWarning, match="Duplicate"):
                archive.writestr("mimetype", b"application/epub+zip")
    report = ingest(source, config)
    plan, _ = prepare_repairs(report.book_id, config)
    assert plan.actions == []
    with pytest.raises(ValueError, match="supported action"):
        apply(plan, config)


def test_clean_book_has_no_repair_and_review_is_escaped(tmp_path, config, epub):
    named = epub.with_name("<script>alert('x')</script>.epub".replace("/", "-"))
    epub.rename(named)
    report = ingest(named, config)
    plan, review = prepare_repairs(report.book_id, config)
    assert plan.actions == []
    html = review.read_text()
    assert "<script>" not in html and "&lt;script&gt;" in html
    assert "No supported repair" in html
    assert get_report(Store(config.root), report.book_id) == report


@pytest.mark.parametrize("change", ["policy", "engine", "plan", "report", "source"])
def test_stale_and_tampered_evidence_is_rejected(tmp_path, config, change, monkeypatch):
    _, before, plan, _ = setup(tmp_path, config)
    if change == "policy":
        config.quality.require_accessibility_audit = True
    elif change == "engine":
        monkeypatch.setattr("emend.repairs.ENGINE_VERSION", "future")
    elif change == "plan":
        document = Store(config.root).document(before.book_id, "repairs", plan.id)
        Path(document["path"]).write_text("{}")
    elif change == "report":
        path = Path(Store(config.root).resolve(before.book_id)["report_path"])
        path.write_text(path.read_text() + "\n")
    else:
        path = config.root / "originals" / f"{before.book_id}.epub"
        path.chmod(0o644)
        path.write_bytes(b"tampered")
    with pytest.raises(ValueError):
        apply(plan, config)
    assert not (config.root / "curated" / f"{before.book_id}.epub").exists()


@pytest.mark.parametrize(
    "failure",
    [
        "validator",
        "unavailable",
        "payload",
        "directory",
        "mutating_validator",
        "database",
        "invalid_zip",
        "report",
    ],
)
def test_rejected_candidate_leaves_original_report_and_no_ready_artifact(
    tmp_path, config, monkeypatch, failure
):
    import emend.repairs as repairs

    _, before, plan, _ = setup(tmp_path, config)
    if failure == "validator":
        config.tools.epubcheck.write_text(
            config.tools.epubcheck.read_text().replace(
                "'messages': []", "'messages': [{'severity':'ERROR'}]"
            )
        )
    elif failure == "unavailable":
        config.tools.epubcheck = tmp_path / "missing"
    elif failure == "invalid_zip":
        monkeypatch.setattr(
            repairs, "rewrite_container", lambda source, target: target.write_bytes(b"not a ZIP")
        )
        monkeypatch.setattr(
            repairs,
            "container_preserved",
            lambda *args: pytest.fail("Unsafe candidate must not be read again"),
        )
    elif failure == "report":
        monkeypatch.setattr(
            repairs,
            "write_reports",
            lambda *args: (_ for _ in ()).throw(OSError("report write failed")),
        )
    elif failure in {"payload", "directory"}:
        real = repairs.rewrite_container

        def rewrite(source, candidate):
            real(source, candidate)
            with zipfile.ZipFile(candidate) as archive:
                infos = archive.infolist()
                data = {i.filename: archive.read(i) for i in infos}
            with zipfile.ZipFile(candidate, "w") as archive:
                for info in infos:
                    archive.writestr(
                        copy.copy(info),
                        b"changed"
                        if info.filename == "EPUB/style.css" and failure == "payload"
                        else data[info.filename],
                    )
                if failure == "directory":
                    archive.writestr("unexpected/", b"")

        monkeypatch.setattr(repairs, "rewrite_container", rewrite)
    elif failure == "mutating_validator":
        real = repairs.validate_epubcheck

        def validate(candidate, directory, settings):
            result = real(candidate, directory, settings)
            candidate.write_bytes(b"changed")
            return result

        monkeypatch.setattr(repairs, "validate_epubcheck", validate)
    else:
        monkeypatch.setattr(
            Store, "transition", lambda *a, **kw: (_ for _ in ()).throw(OSError("database failed"))
        )
    with pytest.raises((ValueError, OSError)):
        apply(plan, config)
    assert get_report(Store(config.root), before.book_id) == before
    assert sha256_file(config.root / "originals" / f"{before.book_id}.epub") == before.book_id
    assert not (config.root / "curated" / f"{before.book_id}.epub").exists()
    assert list((config.root / "reports" / before.book_id).glob("*/failure.txt"))


def test_cli_json_and_required_approval(tmp_path, config):
    _, before, _, _ = setup(tmp_path, config)
    settings = tmp_path / "config.yaml"
    settings.write_text(yaml.safe_dump(config.model_dump(mode="json")))
    runner = CliRunner()
    result = runner.invoke(app, ["repair-plan", before.book_id, "--json", "-c", str(settings)])
    assert result.exit_code == 0, result.output
    plan = json.loads(result.output)
    assert plan["actions"] == [ACTION]
    args = ["apply-repairs", before.book_id, "--plan", plan["id"], "-c", str(settings)]
    assert runner.invoke(app, args).exit_code == 2
    original_tool = config.tools.epubcheck
    config.tools.epubcheck = tmp_path / "unavailable"
    settings.write_text(yaml.safe_dump(config.model_dump(mode="json")))
    rejected = runner.invoke(app, [*args, "--approve", ACTION, "--json"])
    assert rejected.exit_code == 1 and "candidate rejected" in rejected.output
    assert get_report(Store(config.root), before.book_id) == before
    config.tools.epubcheck = original_tool
    settings.write_text(yaml.safe_dump(config.model_dump(mode="json")))
    result = runner.invoke(app, [*args, "--approve", ACTION, "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["repair_changes"][0]["plan_id"] == plan["id"]


@pytest.mark.parametrize("version", ["2.0", "3.0"])
def test_official_epubcheck_accepts_repaired_containers(tmp_path, config, version):
    jar = os.environ.get("EMEND_TEST_EPUBCHECK_JAR")
    if not jar:
        pytest.skip("Set EMEND_TEST_EPUBCHECK_JAR for official validation")
    config.tools.epubcheck_jar = Path(jar)
    _, before, plan, _ = setup(tmp_path, config, version=version, compressed=True)
    after = apply(plan, config)
    assert next(g for g in after.gates if g.name == "epubcheck").status == "passed"
    assert not inspect_epub(
        config.root / "curated" / f"{before.book_id}.epub", config.limits
    ).blocking


def test_repair_history_survives_metadata_curation(tmp_path, config):
    files = book_files()
    files["EPUB/package.opf"] = files["EPUB/package.opf"].replace(
        b"<dc:publisher>Emend</dc:publisher>",
        f"<dc:identifier>urn:isbn:{ISBN}</dc:identifier>".encode(),
    )
    _, _, plan, _ = setup(tmp_path, config, files=files)
    repaired = apply(plan, config)
    research(plan.book_id, config, client=evidence_client(tmp_path))
    edited = apply_proposals(plan.book_id, config, safe=True)
    assert edited.inspection.metadata["publisher"] == ["Observatory Press"]
    assert edited.repair_changes == repaired.repair_changes
    assert edited.changes[: len(repaired.changes)] == repaired.changes


@pytest.mark.parametrize("when", ["database", "copy"])
def test_activation_failure_restores_retained_current_artifact(tmp_path, config, monkeypatch, when):
    import emend.repairs as repairs

    _, before, plan, _ = setup(tmp_path, config)
    store = Store(config.root)
    row = store.resolve(before.book_id)
    artifact = config.root / "curated" / f"{before.book_id}.epub"
    repairs.atomic_copy(config.root / "originals" / artifact.name, artifact)
    # Model a failed audit with retained current bytes, as revalidation records them.
    retained = before.model_copy(update={"artifact_sha256": before.book_id})
    write_reports(retained, Path(row["report_path"]).parent)
    store.transition(
        before.book_id,
        before.source_name,
        "FAILED",
        "Retained current bytes",
        Path(row["report_path"]),
        artifact,
    )
    plan, _ = prepare_repairs(before.book_id, config)
    if when == "database":
        monkeypatch.setattr(
            Store, "transition", lambda *a, **kw: (_ for _ in ()).throw(OSError("database failed"))
        )
    else:
        real = repairs.atomic_copy
        failed = False

        def copy_then_fail(source, target):
            nonlocal failed
            real(source, target)
            if target == artifact and not failed:
                failed = True
                raise OSError("Failure immediately after replacing artifact")

        monkeypatch.setattr(repairs, "atomic_copy", copy_then_fail)
    with pytest.raises(OSError):
        apply(plan, config)
    assert sha256_file(artifact) == before.book_id
    assert get_report(store, before.book_id) == retained


def test_repair_rejects_validator_modification_of_current_input(tmp_path, config, monkeypatch):
    import emend.repairs as repairs

    _, before, plan, _ = setup(tmp_path, config)
    real = repairs.validate_epubcheck

    def validate(candidate, directory, settings):
        result = real(candidate, directory, settings)
        original = config.root / "originals" / f"{before.book_id}.epub"
        original.chmod(0o644)
        original.write_bytes(b"external tampering")
        return result

    monkeypatch.setattr(repairs, "validate_epubcheck", validate)
    with pytest.raises(ValueError, match="candidate rejected"):
        apply(plan, config)
    assert not (config.root / "curated" / f"{before.book_id}.epub").exists()
    assert get_report(Store(config.root), before.book_id) == before


def test_new_report_invalidates_old_plan_even_when_bytes_are_unchanged(tmp_path, config):
    _, before, plan, _ = setup(tmp_path, config)
    assert revalidate(before.book_id, config).state == "FAILED"
    with pytest.raises(ValueError, match="stale"):
        apply(plan, config)


def test_applied_repair_retry_does_not_claim_failed_revalidation_passed(tmp_path, config):
    _, before, plan, _ = setup(tmp_path, config)
    apply(plan, config)
    config.tools.epubcheck = tmp_path / "missing"
    assert revalidate(before.book_id, config).state == "REVIEW_REQUIRED"
    with pytest.raises(ValueError, match="already applied"):
        apply(plan, config)


@pytest.mark.parametrize("defect", ["extra", "local-only", "prefix"])
@pytest.mark.parametrize("version", ["2.0", "3.0"])
def test_mimetype_header_only_defects_have_explicit_preserving_repair(
    tmp_path, config, defect, version
):
    import struct

    from tests.epub_factory import write_epub

    if jar := os.environ.get("EMEND_TEST_EPUBCHECK_JAR"):
        config.tools.epubcheck = None
        config.tools.epubcheck_jar = Path(jar)
    source = write_epub(tmp_path / "header.epub", version=version)
    if defect == "prefix":
        source.write_bytes(b"publisher preamble" + source.read_bytes())
    else:
        with zipfile.ZipFile(source) as archive:
            entries = [(copy.copy(i), archive.read(i)) for i in archive.infolist()]
        with zipfile.ZipFile(source, "w") as archive:
            for info, data in entries:
                if info.filename == "mimetype":
                    info.extra = b"\xfe\xca\x00\x00"
                archive.writestr(info, data)
        if defect == "local-only":
            data = bytearray(source.read_bytes())
            central = data.index(b"PK\x01\x02")
            # Central header: filename length at 28, extra length at 30, fixed size 46.
            length = struct.unpack_from("<H", data, central + 28)[0]
            assert data[central + 46 : central + 46 + length] == b"mimetype"
            struct.pack_into("<H", data, central + 30, 0)
            del data[central + 46 + length : central + 46 + length + 4]
            end = data.rindex(b"PK\x05\x06")
            size = struct.unpack_from("<I", data, end + 12)[0]
            struct.pack_into("<I", data, end + 12, size - 4)
            source.write_bytes(data)
    before = ingest(source, config)
    assert {f.code for f in before.inspection.findings if f.severity == "error"} == {
        "zip.mimetype_header"
    }
    plan, _ = prepare_repairs(before.book_id, config)
    assert plan.actions == [ACTION]
    if defect == "local-only":
        assert plan.before.extra_bytes == 0 and plan.before.local_extra_bytes == 4
    after = apply(plan, config)
    assert after.state == "VALIDATED"
    assert after.inspection.resource_hashes == before.inspection.resource_hashes
    assert sha256_file(config.root / "originals" / f"{before.book_id}.epub") == before.book_id


def test_central_only_mimetype_extra_is_not_a_local_header_defect(tmp_path, config):
    import struct

    from emend.validation import validate_epubcheck
    from tests.epub_factory import write_epub

    source = write_epub(tmp_path / "central-only.epub")
    data = bytearray(source.read_bytes())
    central = data.index(b"PK\x01\x02")
    length = struct.unpack_from("<H", data, central + 28)[0]
    assert data[central + 46 : central + 46 + length] == b"mimetype"
    struct.pack_into("<H", data, central + 30, 4)
    data[central + 46 + length : central + 46 + length] = b"\xfe\xca\x00\x00"
    end = data.rindex(b"PK\x05\x06")
    size = struct.unpack_from("<I", data, end + 12)[0]
    struct.pack_into("<I", data, end + 12, size + 4)
    source.write_bytes(data)
    if jar := os.environ.get("EMEND_TEST_EPUBCHECK_JAR"):
        config.tools.epubcheck = None
        config.tools.epubcheck_jar = Path(jar)
        evidence = tmp_path / "checker"
        evidence.mkdir()
        assert validate_epubcheck(source, evidence, config).status == "passed"
    assert not inspect_epub(source, config.limits).blocking
