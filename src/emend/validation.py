import json
import subprocess
from pathlib import Path

from emend.config import Config, executable
from emend.models import Gate


def epubcheck_command(config: Config) -> list[str] | None:
    if config.tools.epubcheck_jar is not None:
        java = executable("java", config)
        jar = config.tools.epubcheck_jar
        return [java, "-jar", str(jar)] if java and jar.is_file() else None
    tool = executable("epubcheck", config)
    return [tool] if tool else None


def run_tool(command: list[str], timeout: int, log: Path) -> subprocess.CompletedProcess:
    # No shell interpolation; EPUB paths are passed as separate absolute arguments.
    try:
        result = subprocess.run(
            command, capture_output=True, text=True, errors="replace", timeout=timeout, check=False
        )
    except subprocess.TimeoutExpired as exc:
        log.write_text(f"Tool exceeded {timeout}s timeout.\n")
        raise ValueError(f"Tool exceeded {timeout}s timeout") from exc
    except OSError as exc:
        log.write_text(f"Unable to start tool: {exc}\n")
        raise ValueError(f"Unable to start tool: {exc}") from exc
    log.write_text(f"Exit status: {result.returncode}\n{result.stdout}\n{result.stderr}")
    return result


def validate_epubcheck(epub: Path, directory: Path, config: Config) -> Gate:
    required = config.quality.require_epubcheck
    command = epubcheck_command(config)
    if command is None:
        return Gate(
            name="epubcheck",
            status="unavailable",
            required=required,
            detail="Install EPUBCheck or set tools.epubcheck_jar (Java also required).",
        )
    evidence = directory / "epubcheck.json"
    log = directory / "epubcheck.log"
    try:
        result = run_tool(
            [*command, str(epub.resolve()), "--json", str(evidence)],
            config.limits.tool_timeout_seconds,
            log,
        )
        report = json.loads(evidence.read_text())
        messages = report["messages"]
        if not isinstance(messages, list) or any(
            not isinstance(m, dict) or "severity" not in m for m in messages
        ):
            raise ValueError("Invalid EPUBCheck messages")
        errors = [m for m in messages if m["severity"].upper() in {"ERROR", "FATAL", "USAGE"}]
        warnings = [m for m in messages if m["severity"].upper() == "WARNING"]
        passed = result.returncode == 0 and not errors
        return Gate(
            name="epubcheck",
            status="passed" if passed else "failed",
            required=required,
            detail=f"Exit {result.returncode}; {len(errors)} errors, {len(warnings)} warnings.",
            evidence=str(evidence),
        )
    except (ValueError, OSError, KeyError, TypeError, AttributeError) as exc:
        return Gate(
            name="epubcheck",
            status="failed",
            required=required,
            detail=f"Validation did not produce usable evidence: {exc}",
            evidence=str(log),
        )


def validate_ace(epub: Path, directory: Path, config: Config) -> Gate:
    required = config.quality.require_accessibility_audit
    tool = executable("ace", config)
    if not tool:
        return Gate(
            name="accessibility",
            status="unavailable",
            required=required,
            detail="Ace by DAISY is not installed. No accessibility certification is claimed.",
        )
    out = directory / "ace"
    log = directory / "ace.log"
    try:
        result = run_tool(
            [tool, "--outdir", str(out), str(epub.resolve())],
            config.limits.tool_timeout_seconds,
            log,
        )
        report_path = out / "report.json"
        report = json.loads(report_path.read_text())
        if (
            not isinstance(report, dict)
            or "assertions" not in report
            or not isinstance(report["assertions"], list)
        ):
            raise ValueError("Missing Ace assertions")
        outcomes = []

        def walk(value):
            if isinstance(value, dict):
                if "earl:outcome" in value:
                    outcomes.append(value["earl:outcome"])
                for child in value.values():
                    walk(child)
            elif isinstance(value, list):
                for child in value:
                    walk(child)

        walk(report)
        if not outcomes:
            raise ValueError("Ace report contains no audit outcomes")
        passed = result.returncode == 0 and all(
            outcome in {"pass", "passed", "earl:passed", "inapplicable", "earl:inapplicable"}
            for outcome in outcomes
        )
        return Gate(
            name="accessibility",
            status="passed" if passed else "failed",
            required=required,
            detail=f"Exit {result.returncode}; {len(outcomes)} audit outcomes. Manual accessibility checks remain outside this audit.",
            evidence=str(report_path),
        )
    except (ValueError, OSError, TypeError) as exc:
        return Gate(
            name="accessibility",
            status="failed",
            required=required,
            detail=f"Accessibility audit did not produce usable evidence: {exc}",
            evidence=str(log),
        )
