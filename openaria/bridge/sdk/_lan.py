"""LAN discovery and Device API v4 verified download."""

from __future__ import annotations

import hashlib
import hmac
import http.client
import ipaddress
import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from zeroconf import IPVersion, ServiceBrowser, ServiceInfo, ServiceListener, Zeroconf

from ._export import (
    SHA256_RE,
    ArtifactDescriptor,
    export_session_tree,
    safe_segment,
)
from ._json import load_json
from .errors import ContractError, DeleteError, DiscoveryError, ExportError
from .models import (
    DeleteFailure,
    DeleteResult,
    ExportedSession,
    SessionInfo,
    Source,
    SourceMode,
)

SERVICE_TYPE = "_ylx-capture._tcp.local."
DEFAULT_DEVICE_API_PORT = 8080
MAX_JSON_BYTES = 2 * 1024 * 1024
MAX_MANIFEST_BYTES = 16 * 1024 * 1024
MAX_ERROR_BYTES = 4096
MAX_SESSIONS = 10_000
DOWNLOAD_CHUNK_BYTES = 1024 * 1024
MAX_REQUEST_ATTEMPTS = 3
MAX_PROBE_WORKERS = 8
PROBE_TIMEOUT = 3.0
RETRYABLE_HTTP_CODES = frozenset({408, 429, 500, 502, 503, 504})


class _RetryableRequestError(DiscoveryError):
    """A read-only request may succeed after a transient transport failure."""


class _CatalogChangedError(ContractError):
    """Pagination must restart to avoid mixing different catalog snapshots."""


def _retry_request[T](operation: Callable[[], T]) -> T:
    for attempt in range(MAX_REQUEST_ATTEMPTS):
        try:
            return operation()
        except _RetryableRequestError:
            if attempt == MAX_REQUEST_ATTEMPTS - 1:
                raise
            time.sleep(0.15 * 2**attempt)
    raise AssertionError("unreachable retry state")


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        return None


class DeviceApiClient:
    """Small synchronous client for the current local Device API v4 profile."""

    def __init__(
        self,
        endpoint: str,
        *,
        timeout: float = 10.0,
        token: str | None = None,
        opener: urllib.request.OpenerDirector | None = None,
    ) -> None:
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        self.api_base = normalize_api_base(endpoint)
        self.timeout = timeout
        self.token = token
        self._opener = opener or urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            _NoRedirects(),
        )

    def probe(self) -> Source:
        descriptor = self._json_get("device", label="Device API v4 descriptor")
        if not isinstance(descriptor, dict):
            raise ContractError("Device API v4 descriptor must be an object")
        if descriptor.get("schema") != "ylx.device.v4":
            raise ContractError("endpoint is not a Device API v4 device")
        api_version = descriptor.get("api_version")
        if not isinstance(api_version, str) or api_version.split(".", 1)[0] != "4":
            raise ContractError(f"unsupported Device API version: {api_version!r}")
        device = descriptor.get("device")
        if not isinstance(device, dict):
            raise ContractError("Device API v4 descriptor omitted device identity")
        device_id = _required_text(device, "device_id", "device identity")
        device_label = _required_text(device, "device_label", "device identity")
        capabilities_raw = descriptor.get("capabilities")
        if not isinstance(capabilities_raw, dict):
            raise ContractError("Device API v4 descriptor omitted capabilities")
        capabilities = {
            key: value if isinstance(value, bool) else False
            for key, value in capabilities_raw.items()
            if isinstance(key, str)
        }
        missing = [
            name
            for name in ("session_list", "session_detail", "artifact_download")
            if not capabilities.get(name, False)
        ]
        if missing:
            raise DiscoveryError(
                f"{device_label} cannot export sessions; unavailable capabilities: "
                + ", ".join(missing)
            )
        return Source(
            mode=SourceMode.LAN,
            location=self.api_base,
            api_base=self.api_base,
            device_id=device_id,
            device_label=device_label,
            capabilities=capabilities,
        )

    def list_sessions(self, source: Source) -> tuple[SessionInfo, ...]:
        for attempt in range(MAX_REQUEST_ATTEMPTS):
            try:
                return self._list_sessions_once(source)
            except _CatalogChangedError:
                if attempt == MAX_REQUEST_ATTEMPTS - 1:
                    raise
                time.sleep(0.15 * 2**attempt)
        raise AssertionError("unreachable catalog retry state")

    def _list_sessions_once(self, source: Source) -> tuple[SessionInfo, ...]:
        cursor: str | None = None
        seen_cursors: set[str] = set()
        seen_sessions: set[str] = set()
        sessions: list[SessionInfo] = []
        catalog_revision: str | None = None
        while True:
            query: dict[str, str] = {"limit": "200"}
            if cursor is not None:
                query["cursor"] = cursor
            page = self._json_get(
                "sessions",
                query=query,
                label="Device API v4 session list",
            )
            if not isinstance(page, dict):
                raise ContractError("Device API v4 session list must be an object")
            schema = page.get("schema")
            if schema not in {"ylx.session-list.v2", "ylx.session-list.v3"}:
                raise ContractError(f"unsupported session list schema: {schema!r}")
            if schema == "ylx.session-list.v3":
                revision = page.get("catalog_revision")
                if (
                    not isinstance(revision, str)
                    or not revision.startswith("sha256:")
                    or not SHA256_RE.fullmatch(revision.removeprefix("sha256:"))
                ):
                    raise ContractError(
                        "session-list.v3 has an invalid catalog_revision"
                    )
                if catalog_revision is None:
                    catalog_revision = revision
                elif revision != catalog_revision:
                    raise _CatalogChangedError(
                        "Device API v4 catalog_revision changed during pagination"
                    )
            items = page.get("items")
            if not isinstance(items, list):
                raise ContractError("Device API v4 session list items must be an array")
            for item in items:
                session = _session_info(item, source)
                if session.session_id in seen_sessions:
                    raise ContractError(
                        f"Device API v4 repeats session {session.session_id}"
                    )
                seen_sessions.add(session.session_id)
                sessions.append(session)
                if len(sessions) > MAX_SESSIONS:
                    raise ContractError(
                        f"Device API v4 returned more than {MAX_SESSIONS} sessions"
                    )
            next_cursor = page.get("next_cursor")
            if schema == "ylx.session-list.v2":
                if next_cursor not in {None, ""}:
                    raise ContractError(
                        "session-list.v2 cannot be paginated; update the device firmware"
                    )
                break
            if next_cursor is None:
                break
            if (
                not isinstance(next_cursor, str)
                or not next_cursor
                or len(next_cursor.encode()) > 4096
            ):
                raise ContractError("Device API v4 returned an invalid next_cursor")
            if next_cursor in seen_cursors:
                raise ContractError("Device API v4 repeated a pagination cursor")
            seen_cursors.add(next_cursor)
            cursor = next_cursor
        return tuple(sessions)

    def export_session(
        self,
        source: Source,
        session: SessionInfo,
        output_root: Path,
        progress: Callable[[str], None] | None = None,
    ) -> ExportedSession:
        if not session.exportable:
            raise ExportError(
                f"session {session.session_id} is unavailable: {session.unavailable_reason}"
            )
        manifest_bytes = self._manifest(session)

        def write_artifact(artifact: ArtifactDescriptor, destination: Path) -> None:
            self._download_artifact(session.session_id, artifact, destination)

        return export_session_tree(
            source=source,
            session=session,
            output_root=output_root,
            manifest_name="manifest.json",
            manifest_bytes=manifest_bytes,
            artifact_writer=write_artifact,
            progress=progress,
        )

    def delete_sessions(
        self, source: Source, sessions: tuple[SessionInfo, ...]
    ) -> DeleteResult:
        if not source.capabilities.get("session_deletion", False):
            raise DeleteError("设备固件尚不支持远程删除，请升级固件后刷新来源")
        if not 1 <= len(sessions) <= 200:
            raise DeleteError("每次远程删除请选择 1 到 200 个录制")
        expected = {session.session_id for session in sessions}
        if len(expected) != len(sessions) or any(
            not session.exportable or not SHA256_RE.fullmatch(session.manifest_sha256)
            for session in sessions
        ):
            raise DeleteError("删除列表含重复项或缺少有效的录制摘要")
        body = json.dumps(
            {
                "schema": "ylx.session-delete-request.v1",
                "sessions": [
                    {
                        "session_id": session.session_id,
                        "manifest_sha256": session.manifest_sha256,
                    }
                    for session in sessions
                ],
            }
        ).encode()
        headers = {
            "Content-Type": "application/json",
            "Idempotency-Key": str(uuid.uuid4()),
        }
        parsed = urllib.parse.urlsplit(self.api_base)
        headers["Origin"] = f"{parsed.scheme}://{parsed.netloc}"
        csrf = os.environ.get("OPENARIA_DEVICE_CSRF_TOKEN", self.token)
        if csrf:
            headers["X-CSRF-Token"] = csrf

        def read() -> bytes:
            response = self._open(
                "sessions/delete", method="POST", data=body, headers=headers
            )
            try:
                return _read_limited(
                    response, MAX_JSON_BYTES, "session deletion result"
                )
            finally:
                response.close()

        value = load_json(_retry_request(read), "session deletion result")
        if not isinstance(value, dict) or set(value) != {
            "schema",
            "deleted_session_ids",
            "failed_sessions",
        }:
            raise ContractError("invalid session deletion result")
        deleted, failures = value["deleted_session_ids"], value["failed_sessions"]
        if (
            value["schema"] != "ylx.session-delete-result.v1"
            or not isinstance(deleted, list)
            or not isinstance(failures, list)
        ):
            raise ContractError("invalid session deletion result")
        seen = set()
        for session_id in deleted:
            if (
                not isinstance(session_id, str)
                or session_id not in expected
                or session_id in seen
            ):
                raise ContractError("invalid deleted session identity")
            seen.add(session_id)
        for failure in failures:
            if (
                not isinstance(failure, dict)
                or set(failure) != {"session_id", "error"}
                or not isinstance(failure["session_id"], str)
                or failure["session_id"] not in expected
                or failure["session_id"] in seen
                or not isinstance(failure["error"], str)
                or not failure["error"]
            ):
                raise ContractError("invalid failed session identity")
            seen.add(failure["session_id"])
        if seen != expected:
            raise ContractError("session deletion result omitted selected recordings")
        return DeleteResult(
            source, tuple(deleted), tuple(DeleteFailure(**item) for item in failures)
        )

    def _manifest(self, session: SessionInfo) -> bytes:
        safe_segment(session.session_id, "session_id")

        def read() -> tuple[bytes, str | None, str | None]:
            response = self._open(f"sessions/{_quote_segment(session.session_id)}")
            try:
                raw = _read_limited(
                    response, MAX_MANIFEST_BYTES, "Device Session manifest"
                )
                declared = _single_header(response.headers, "YLX-Manifest-SHA256")
                etag = _single_header(response.headers, "ETag")
                return raw, declared, etag
            finally:
                response.close()

        raw, declared, etag = _retry_request(read)
        if declared is None or not SHA256_RE.fullmatch(declared):
            raise ContractError(
                "Device API v4 manifest omitted a valid YLX-Manifest-SHA256"
            )
        if etag != f'"{declared}"':
            raise ContractError(
                "Device API v4 manifest ETag does not match its digest header"
            )
        actual = hashlib.sha256(raw).hexdigest()
        if not hmac.compare_digest(actual, declared):
            raise ContractError(
                "Device API v4 manifest body does not match its digest header"
            )
        if not hmac.compare_digest(actual, session.manifest_sha256):
            raise ContractError(
                "Device API v4 manifest changed after session discovery"
            )
        return raw

    def _download_artifact(
        self,
        session_id: str,
        artifact: ArtifactDescriptor,
        destination: Path,
    ) -> None:
        _retry_request(
            lambda: self._download_artifact_once(session_id, artifact, destination)
        )

    def _download_artifact_once(
        self,
        session_id: str,
        artifact: ArtifactDescriptor,
        destination: Path,
    ) -> None:
        path = (
            f"sessions/{_quote_segment(session_id)}/artifacts/"
            f"{_quote_segment(artifact.artifact_id)}"
        )
        response = self._open(path)
        created = False
        try:
            content_length = _content_length(response.headers)
            if content_length != artifact.size_bytes:
                raise ExportError(
                    f"artifact {artifact.path} Content-Length does not match the manifest"
                )
            etag = _single_header(response.headers, "ETag")
            if etag != f'"{artifact.sha256}"':
                raise ExportError(
                    f"artifact {artifact.path} ETag does not match the manifest"
                )
            media_type = _single_header(response.headers, "Content-Type")
            if (
                media_type is None
                or media_type.split(";", 1)[0].strip() != artifact.media_type
            ):
                raise ExportError(
                    f"artifact {artifact.path} Content-Type does not match the manifest"
                )
            digest = hashlib.sha256()
            received = 0
            with destination.open("xb") as handle:
                created = True
                while chunk := _read_response(
                    response, DOWNLOAD_CHUNK_BYTES, artifact.path
                ):
                    received += len(chunk)
                    if received > artifact.size_bytes:
                        raise ExportError(
                            f"artifact {artifact.path} exceeded its declared size"
                        )
                    digest.update(chunk)
                    handle.write(chunk)
            if received != artifact.size_bytes:
                raise _RetryableRequestError(
                    f"artifact {artifact.path} download was interrupted: "
                    f"expected {artifact.size_bytes} bytes, received {received}"
                )
            if not hmac.compare_digest(digest.hexdigest(), artifact.sha256):
                raise ExportError(
                    f"artifact {artifact.path} failed size/SHA-256 verification"
                )
        except Exception:
            if created:
                destination.unlink(missing_ok=True)
            raise
        finally:
            response.close()

    def _json_get(
        self,
        path: str,
        *,
        query: dict[str, str] | None = None,
        label: str,
    ) -> Any:
        def read() -> bytes:
            response = self._open(path, query=query)
            try:
                return _read_limited(response, MAX_JSON_BYTES, label)
            finally:
                response.close()

        return load_json(_retry_request(read), label)

    def _open(
        self,
        path: str,
        *,
        query: dict[str, str] | None = None,
        method: str = "GET",
        data: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> Any:
        url = f"{self.api_base}/{path.lstrip('/')}"
        if query:
            url = f"{url}?{urllib.parse.urlencode(query)}"
        request_headers = {
            "Accept": "application/json, application/octet-stream;q=0.9",
            "User-Agent": "openaria-bridge-sdk/0.2",
        }
        if self.token:
            request_headers["Authorization"] = f"Bearer {self.token}"
        request_headers.update(headers or {})
        request = urllib.request.Request(
            url, headers=request_headers, method=method, data=data
        )
        try:
            response = self._opener.open(request, timeout=self.timeout)
        except urllib.error.HTTPError as error:
            try:
                detail = (
                    error.read(MAX_ERROR_BYTES)
                    .decode("utf-8", errors="replace")
                    .strip()
                )
            except (http.client.HTTPException, OSError):
                detail = str(error.reason)
            finally:
                error.close()
            error_type = (
                _RetryableRequestError
                if error.code in RETRYABLE_HTTP_CODES
                else DiscoveryError
            )
            raise error_type(
                f"Device API request failed with HTTP {error.code}: {detail or error.reason}"
            ) from error
        except (urllib.error.URLError, http.client.HTTPException, OSError) as error:
            reason = getattr(error, "reason", error)
            raise _RetryableRequestError(
                f"cannot reach Device API at {self.api_base}: {reason}"
            ) from error
        status = getattr(response, "status", response.getcode())
        if status != 200:
            response.close()
            error_type = (
                _RetryableRequestError
                if status in RETRYABLE_HTTP_CODES
                else DiscoveryError
            )
            raise error_type(f"Device API returned unexpected HTTP {status}")
        return response


class _Listener(ServiceListener):
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._endpoints: set[str] = set()

    def add_service(self, zc: Zeroconf, type_: str, name: str) -> None:
        self._resolve(zc, type_, name)

    def update_service(self, zc: Zeroconf, type_: str, name: str) -> None:
        self._resolve(zc, type_, name)

    def remove_service(self, zc: Zeroconf, type_: str, name: str) -> None:
        return None

    def _resolve(self, zc: Zeroconf, type_: str, name: str) -> None:
        info = zc.get_service_info(type_, name, timeout=1000)
        if info is None:
            return
        endpoints = endpoints_from_service_info(info)
        with self._lock:
            self._endpoints.update(endpoints)

    def endpoints(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(sorted(self._endpoints, key=_endpoint_sort_key))


def discover_mdns_endpoints(timeout: float = 3.0) -> tuple[str, ...]:
    if timeout <= 0:
        raise ValueError("discovery timeout must be positive")
    listener = _Listener()
    try:
        zeroconf = Zeroconf(ip_version=IPVersion.All)
    except OSError as error:
        raise DiscoveryError(f"mDNS is unavailable: {error}") from error
    browser: ServiceBrowser | None = None
    try:
        browser = ServiceBrowser(zeroconf, SERVICE_TYPE, listener)
        threading.Event().wait(timeout)
        return listener.endpoints()
    finally:
        if browser is not None:
            browser.cancel()
        zeroconf.close()


def endpoints_from_service_info(info: ServiceInfo) -> tuple[str, ...]:
    port = info.port
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        return ()
    endpoints = {
        f"http://{_url_host(address)}:{port}"
        for address in info.parsed_scoped_addresses()
        if _connectable_address(address)
    }
    return tuple(sorted(endpoints, key=_endpoint_sort_key))


def probe_lan_sources(
    endpoints: Iterable[str],
    *,
    timeout: float,
    token: str | None,
) -> tuple[Source, ...]:
    sources_by_device: dict[str, Source] = {}
    errors: list[str] = []
    endpoints = tuple(dict.fromkeys(endpoints))

    def probe(endpoint: str) -> Source:
        return DeviceApiClient(
            endpoint, timeout=min(timeout, PROBE_TIMEOUT), token=token
        ).probe()

    # Discovery has a shorter socket budget than potentially large downloads.
    with ThreadPoolExecutor(max_workers=MAX_PROBE_WORKERS) as executor:
        probes = {executor.submit(probe, endpoint): endpoint for endpoint in endpoints}
        for future in as_completed(probes):
            endpoint = probes[future]
            try:
                source = future.result()
            except (DiscoveryError, ContractError, ValueError) as error:
                errors.append(f"{endpoint}: {error}")
                continue
            sources_by_device.setdefault(source.device_id, source)
    if not sources_by_device:
        suffix = f" ({'; '.join(errors)})" if errors else ""
        raise DiscoveryError(f"no usable Open Aria Device API v4 device found{suffix}")
    return tuple(
        sorted(sources_by_device.values(), key=lambda source: source.display_name)
    )


def normalize_api_base(endpoint: str) -> str:
    value = endpoint.strip()
    if not value:
        raise ValueError("endpoint must not be empty")
    if "://" not in value:
        value = f"http://{value}"
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("Device API endpoint must use http or https")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("Device API endpoint must not contain credentials")
    if parsed.query or parsed.fragment:
        raise ValueError("Device API endpoint must not contain a query or fragment")
    path = parsed.path.rstrip("/")
    if path not in {"", "/api/v4"}:
        raise ValueError("Device API endpoint path must be empty or /api/v4")
    hostname = parsed.hostname
    if not hostname:
        raise ValueError("Device API endpoint omitted a host")
    try:
        port = parsed.port or DEFAULT_DEVICE_API_PORT
    except ValueError as error:
        raise ValueError("Device API endpoint has an invalid port") from error
    host = parsed.hostname.replace("%25", "%")
    if ":" in host:
        encoded = host.replace("%", "%25")
        host = f"[{encoded}]"
    return f"{parsed.scheme}://{host}:{port}/api/v4"


def _session_info(value: Any, source: Source) -> SessionInfo:
    if not isinstance(value, dict):
        raise ContractError("Device API v4 session item must be an object")
    session_id = _required_text(value, "session_id", "session item")
    safe_segment(session_id, "session_id")
    if value.get("producer_outcome") != "sealed":
        raise ContractError(f"session {session_id} is not sealed")
    device = value.get("device")
    if not isinstance(device, dict):
        raise ContractError(f"session {session_id} omitted device identity")
    if (
        device.get("device_id") != source.device_id
        or device.get("device_label") != source.device_label
    ):
        raise ContractError(f"session {session_id} belongs to a different device")
    display_name = value.get("display_name")
    if not isinstance(display_name, str) or not display_name:
        display_name = session_id
    started_at = value.get("started_at")
    if not isinstance(started_at, str) or not started_at:
        raise ContractError(f"session {session_id} has no started_at")
    duration = value.get("duration_seconds")
    if (
        isinstance(duration, bool)
        or not isinstance(duration, (int, float))
        or duration < 0
    ):
        raise ContractError(f"session {session_id} has an invalid duration")
    total_bytes = value.get("total_bytes")
    if (
        isinstance(total_bytes, bool)
        or not isinstance(total_bytes, int)
        or total_bytes < 0
    ):
        raise ContractError(f"session {session_id} has an invalid total_bytes")
    verification = value.get("verification")
    exportable = False
    reason = "gateway verification is missing"
    manifest_sha256 = ""
    if isinstance(verification, dict):
        manifest = verification.get("manifest_sha256")
        verdict = verification.get("verdict")
        actor = verification.get("actor")
        if actor != "gateway":
            reason = "verification actor is not the gateway"
        elif not isinstance(manifest, str) or not SHA256_RE.fullmatch(manifest):
            reason = "gateway verification has no valid manifest digest"
        elif verdict != "usable":
            reason = "gateway marked the session unusable"
        else:
            exportable = True
            reason = None
            manifest_sha256 = manifest
    return SessionInfo(
        session_id=session_id,
        display_name=display_name,
        started_at=started_at,
        duration_seconds=float(duration),
        total_bytes=total_bytes,
        manifest_sha256=manifest_sha256,
        exportable=exportable,
        unavailable_reason=reason,
    )


def _read_limited(response: Any, maximum: int, label: str) -> bytes:
    declared = _content_length(response.headers, required=False)
    if declared is not None and declared > maximum:
        raise ContractError(f"{label} exceeds the {maximum}-byte limit")
    raw = bytearray()
    while len(raw) <= maximum:
        chunk = _read_response(
            response, min(DOWNLOAD_CHUNK_BYTES, maximum + 1 - len(raw)), label
        )
        if not chunk:
            break
        raw.extend(chunk)
    if len(raw) > maximum:
        raise ContractError(f"{label} exceeds the {maximum}-byte limit")
    if declared is not None and len(raw) < declared:
        raise _RetryableRequestError(
            f"{label} download was interrupted before Content-Length"
        )
    if declared is not None and len(raw) != declared:
        raise ContractError(f"{label} Content-Length does not match its body")
    return bytes(raw)


def _read_response(response: Any, size: int, label: str) -> bytes:
    try:
        return response.read(size)
    except (http.client.HTTPException, OSError) as error:
        raise _RetryableRequestError(
            f"{label} download was interrupted: {error}"
        ) from error


def _single_header(headers: Any, name: str) -> str | None:
    values = headers.get_all(name, [])
    if len(values) > 1:
        raise ContractError(f"Device API response repeats {name}")
    return values[0].strip() if values else None


def _content_length(headers: Any, *, required: bool = True) -> int | None:
    value = _single_header(headers, "Content-Length")
    if value is None:
        if required:
            raise ContractError("Device API response omitted Content-Length")
        return None
    try:
        parsed = int(value)
    except ValueError as error:
        raise ContractError(
            "Device API response has an invalid Content-Length"
        ) from error
    if parsed < 0:
        raise ContractError("Device API response has a negative Content-Length")
    return parsed


def _required_text(value: dict[str, Any], field: str, label: str) -> str:
    result = value.get(field)
    if not isinstance(result, str) or not result.strip():
        raise ContractError(f"{label} omitted {field}")
    return result


def _quote_segment(value: str) -> str:
    safe_segment(value, "URL path segment")
    return urllib.parse.quote(value, safe="")


def _connectable_address(value: str) -> bool:
    bare = value.split("%", 1)[0]
    try:
        address = ipaddress.ip_address(bare)
    except ValueError:
        return False
    if address.is_unspecified or address.is_multicast:
        return False
    return not (address.version == 6 and address.is_link_local and "%" not in value)


def _url_host(value: str) -> str:
    if ":" not in value:
        return value
    return f"[{value.replace('%', '%25')}]"


def _endpoint_sort_key(value: str) -> tuple[int, str]:
    return (1 if "[" in value else 0, value)
