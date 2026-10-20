import hashlib
import json
import re
import subprocess
from pathlib import Path

from emend import ENGINE_VERSION
from emend.config import Config, executable
from emend.inspect import sha256_file
from emend.pipeline import get_report
from emend.storage import Store


def command(args: list[str], timeout: int) -> str:
    try:
        result = subprocess.run(
            args, capture_output=True, text=True, errors="replace", timeout=timeout, check=False
        )
    except subprocess.TimeoutExpired as exc:
        raise ValueError(
            "Publisher timed out. Retry to reconcile the destination before publishing again."
        ) from exc
    except OSError as exc:
        raise ValueError(f"Publisher could not start: {exc}") from exc
    if result.returncode:
        raise ValueError(
            f"Publisher exited {result.returncode}: {(result.stderr or result.stdout)[-2000:]}"
        )
    return result.stdout


def publish_calibre(epub: Path, book_id: str, config: Config) -> dict:
    tool = executable("calibredb", config)
    library = config.publish.calibre_library
    if not tool or library is None:
        raise ValueError("Configure publish.calibre_library and install Calibre before publishing.")
    if not (library / "metadata.db").is_file():
        raise ValueError(
            "Calibre library does not exist. Select or create it in Calibre, then configure its path."
        )
    timeout = config.limits.publish_timeout_seconds

    def find() -> list[dict]:
        data = json.loads(
            command(
                [
                    tool,
                    "list",
                    "--with-library",
                    str(library),
                    "--for-machine",
                    "--fields",
                    "id,identifiers,formats",
                    "--search",
                    f"identifiers:emend:{book_id}",
                ],
                timeout,
            )
        )
        if not isinstance(data, list):
            raise ValueError("Calibre returned an unexpected library listing.")
        if any(
            not isinstance(row, dict) or not isinstance(row.get("identifiers", {}), dict)
            for row in data
        ):
            raise ValueError("Calibre returned malformed book records.")
        return [row for row in data if row.get("identifiers", {}).get("emend") == book_id]

    existing = find()
    if not existing:
        command(
            [
                tool,
                "add",
                "--with-library",
                str(library),
                "--duplicates",
                "--identifier",
                f"emend:{book_id}",
                str(epub.resolve()),
            ],
            timeout,
        )
        existing = find()
    if len(existing) != 1:
        raise ValueError(
            "Could not identify exactly one imported Calibre book. Inspect the library before retrying."
        )
    formats = existing[0].get("formats", [])
    if not isinstance(formats, list) or any(not isinstance(path, str) for path in formats):
        raise ValueError("Calibre returned malformed format paths.")
    epubs = [Path(path).resolve() for path in formats if Path(path).suffix.lower() == ".epub"]
    if len(epubs) != 1 or not epubs[0].is_relative_to(library.resolve()) or not epubs[0].is_file():
        raise ValueError("Calibre's imported EPUB could not be verified in the selected library.")
    if sha256_file(epubs[0]) != book_id:
        raise ValueError(
            "Calibre's stored EPUB differs from the approved artifact. Check import plugins; no successful delivery was recorded."
        )
    return {"calibre_id": existing[0]["id"], "library": str(library), "sha256": book_id}


def publish_drive(epub: Path, book_id: str, config: Config) -> dict:
    tool = executable("rclone", config)
    remote = config.publish.drive_remote
    if not tool or not remote:
        raise ValueError(
            "Install rclone and configure publish.drive_remote after running rclone config on your Mac."
        )
    destination = remote_destination(remote, book_id + ".epub")
    command(
        [tool, "copyto", "--checksum", str(epub.resolve()), destination],
        config.limits.publish_timeout_seconds,
    )
    return {"destination": destination, "sha256": book_id}


def remote_destination(remote: str, filename: str) -> str:
    # Accept named rclone remotes, not arbitrary local paths or inline credential backends.
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*:[^\r\n]*", remote) or ".." in remote.split(
        ":", 1
    )[1].split("/"):
        raise ValueError("Use a named rclone remote such as gdrive:Emend/curated.")
    return remote.rstrip("/") + "/" + filename


def target_key(target: str, config: Config) -> str:
    destination = (
        config.publish.calibre_library if target == "calibre" else config.publish.drive_remote
    )
    if destination is None:
        raise ValueError(f"Publishing destination for {target} is not configured.")
    return target + ":" + hashlib.sha256(str(destination).encode()).hexdigest()


def publish(book_id: str, targets: list[str], config: Config) -> dict:
    if not targets or any(target not in {"calibre", "drive"} for target in targets):
        raise ValueError("Select at least one supported target: calibre or drive.")
    store = Store(config.root)
    results = {}
    with store.locked():
        row = store.resolve(book_id)
        book_id = row["id"]
        report = get_report(store, book_id)
        if row["state"] not in {"VALIDATED", "PUBLISHED"} or report.state != "VALIDATED":
            raise ValueError("Publication blocked: this book has not passed its required gates.")
        if report.policy_sha256 != config.policy_hash() or report.engine_version != ENGINE_VERSION:
            raise ValueError(
                "Quality policy or engine changed. Run emend revalidate before publishing."
            )
        if any(g.required and g.status != "passed" for g in report.gates):
            raise ValueError("Publication blocked: required checks are incomplete.")
        artifact = config.root / "curated" / f"{book_id}.epub"
        original = config.root / "originals" / f"{book_id}.epub"
        if (
            report.book_id != book_id
            or not report.artifact_sha256
            or report.inspection.source_sha256 != report.artifact_sha256
            or not artifact.is_file()
            or not original.is_file()
            or sha256_file(artifact) != report.artifact_sha256
            or sha256_file(original) != book_id
        ):
            raise ValueError("Publication blocked: source or artifact hash mismatch.")
        for target in dict.fromkeys(targets):
            try:
                key = target_key(target, config)
                if report.artifact_sha256 != book_id:
                    key += ":" + report.artifact_sha256
            except ValueError as exc:
                results[target] = {"status": "pending", "receipt": {"error": str(exc)}}
                continue
            previous = store.receipt(book_id, key)
            if previous and previous["status"] == "published":
                results[target] = {
                    "status": "already_published",
                    "receipt": json.loads(previous["receipt"]),
                }
                continue
            try:
                receipt = (publish_calibre if target == "calibre" else publish_drive)(
                    artifact, report.artifact_sha256, config
                )
                store.save_receipt(book_id, key, "published", receipt)
                results[target] = {"status": "published", "receipt": receipt}
            except (ValueError, OSError, KeyError, TypeError) as exc:
                receipt = {"error": str(exc)}
                store.save_receipt(book_id, key, "pending", receipt)
                results[target] = {"status": "pending", "receipt": receipt}
        state = (
            "PUBLISHED"
            if all(
                item["status"] in {"published", "already_published"} for item in results.values()
            )
            else "VALIDATED"
        )
        store.transition(
            book_id,
            row["source_name"],
            state,
            json.dumps(results),
            Path(row["report_path"]),
            artifact,
        )
    return results
