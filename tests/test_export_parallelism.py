import dataclasses
import threading

import pytest
from test_workflow_stability import SESSION, SOURCE

from openaria.bridge.sdk import ExportedSession, ExportError, OpenAriaSDK
from openaria.bridge.sdk import client as module


def test_parallel_batch_is_bounded_continues_and_preserves_catalog_order(
    tmp_path, monkeypatch
):
    barrier = threading.Barrier(2)
    lock = threading.Lock()
    active = peak = 0
    sessions = tuple(dataclasses.replace(SESSION, session_id=str(i)) for i in range(6))

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        def export_session(self, source, session, output, progress, options):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
            try:
                barrier.wait(timeout=3)
                if session.session_id == "1":
                    raise ExportError("damaged")
                progress(session.session_id)
                return ExportedSession(
                    session.session_id, output / session.session_id, 1, 10
                )
            finally:
                with lock:
                    active -= 1

    sdk = OpenAriaSDK(output=tmp_path)
    monkeypatch.setattr(sdk, "list_sessions", lambda *args, **kwargs: sessions)
    monkeypatch.setattr(module, "DeviceApiClient", Client)
    result = sdk.export(source=SOURCE, continue_on_error=True, max_workers=2)
    assert peak == 2
    assert [s.session_id for s in result.sessions] == ["0", "2", "3", "4", "5"]
    assert [(f.session_id, f.error) for f in result.failed_sessions] == [
        ("1", "damaged")
    ]


@pytest.mark.parametrize("workers", [0, 9, True, 1.5])
def test_invalid_worker_count_rejected_before_discovery(workers):
    with pytest.raises(ValueError, match="max_workers"):
        OpenAriaSDK().export(max_workers=workers)
