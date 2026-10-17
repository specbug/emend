import hashlib
import json
import os
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path

from emend.config import MetadataPolicy
from emend.research.models import Evidence
from emend.storage import now

ALLOWED_HOSTS = {"openlibrary.org", "www.googleapis.com"}
MAX_RESPONSE = 2 * 1024**2


class ProviderError(ValueError):
    pass


def check_url(url: str):
    parsed = urllib.parse.urlsplit(url)
    if (
        parsed.scheme != "https"
        or parsed.hostname not in ALLOWED_HOSTS
        or parsed.username
        or parsed.password
        or parsed.port not in {None, 443}
    ):
        raise ProviderError("Provider URL is outside the supported HTTPS destinations.")


class SameProviderRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        check_url(newurl)
        if urllib.parse.urlsplit(req.full_url).hostname != urllib.parse.urlsplit(newurl).hostname:
            raise ProviderError("Cross-provider redirect rejected.")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class EvidenceClient:
    def __init__(self, cache: Path, policy: MetadataPolicy, *, offline=False, refresh=False):
        if offline and refresh:
            raise ValueError("--offline and --refresh cannot be combined.")
        self.cache = cache
        self.policy = policy
        self.offline = offline
        self.refresh = refresh
        self.documents: dict[str, Evidence] = {}

    def get(self, url: str) -> dict:
        check_url(url)
        if url in self.documents:
            return self.documents[url].payload
        key = hashlib.sha256(url.encode()).hexdigest()
        path = self.cache / (key + ".json")
        if path.exists() and not self.refresh:
            try:
                if path.stat().st_size > MAX_RESPONSE * 2:
                    raise ValueError("oversized cache entry")
                entry = json.loads(path.read_text())
                body = entry["body"].encode("utf-8")
                stamp = datetime.fromisoformat(entry["fetched_at"])
                if entry["url"] != url or hashlib.sha256(body).hexdigest() != entry["sha256"]:
                    raise ValueError("cache integrity mismatch")
                if stamp.tzinfo is None or stamp > datetime.now(UTC) + timedelta(minutes=5):
                    raise ValueError("invalid cache timestamp")
                if datetime.now(UTC) - stamp <= timedelta(days=self.policy.cache_days):
                    return self.remember(url, body, entry["fetched_at"])
            except (ValueError, KeyError, TypeError, AttributeError) as exc:
                raise ProviderError(f"Cached response is invalid; use --refresh: {exc}") from exc
        if self.offline:
            raise ProviderError("No fresh cached evidence is available offline.")
        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": "Emend/0.2 (local EPUB metadata research)",
                "Accept": "application/json",
            },
        )
        try:
            with urllib.request.build_opener(SameProviderRedirect()).open(
                request, timeout=self.policy.request_timeout_seconds
            ) as response:
                check_url(response.geturl())
                body = response.read(MAX_RESPONSE + 1)
                if len(body) > MAX_RESPONSE:
                    raise ProviderError("Provider response exceeds the size limit.")
        except urllib.error.HTTPError as exc:
            raise ProviderError(f"Provider returned HTTP {exc.code}.") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ProviderError(f"Provider unavailable: {exc}") from exc
        fetched = now()
        payload = self.remember(url, body, fetched)
        self.cache.mkdir(parents=True, exist_ok=True)
        entry = {
            "url": url,
            "fetched_at": fetched,
            "body": body.decode("utf-8"),
            "sha256": hashlib.sha256(body).hexdigest(),
        }
        fd, temporary = tempfile.mkstemp(dir=self.cache, prefix=".evidence-")
        try:
            with os.fdopen(fd, "w") as out:
                json.dump(entry, out, ensure_ascii=False)
            os.replace(temporary, path)
        finally:
            Path(temporary).unlink(missing_ok=True)
        return payload

    def remember(self, url: str, body: bytes, fetched: str) -> dict:
        try:
            payload = json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeError) as exc:
            raise ProviderError("Provider response is not valid JSON.") from exc
        if not isinstance(payload, dict):
            raise ProviderError("Provider JSON must be an object.")
        self.documents[url] = Evidence(
            url=url, fetched_at=fetched, sha256=hashlib.sha256(body).hexdigest(), payload=payload
        )
        return payload
