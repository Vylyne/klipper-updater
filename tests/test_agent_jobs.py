"""Log batching and the fw.build / fw.job.* methods.

The sequence-number contract is the point of most of this: batched log events
carry the index of their *first* line, so the panel can tell a gap from an
in-order append. Get it wrong and a streaming log silently lies after a dropped
frame or a page reload.
"""

from __future__ import annotations

import os
import time

import pytest

from klipper_updater.agent.events import EventEmitter, LogBatcher
from klipper_updater.agent.methods import Api
from klipper_updater.agent.rpc import ERR_INVALID_PARAMS, ERR_METHOD_NOT_FOUND, RpcError
from klipper_updater.jobs import Job, JobRunner


class CapturingEmitter(EventEmitter):
    """Records events instead of sending them.

    Deliberately bypasses the real emit(), including its reserved-name guard -
    that guard is covered against the genuine EventEmitter in
    test_agent_service.py, where it means something.
    """

    def __init__(self) -> None:
        super().__init__(lambda: None)
        self.events: list[tuple[str, dict]] = []

    def emit(self, event: str, data=None) -> bool:
        self.events.append((event, data or {}))
        return True

    def of(self, name: str) -> list[dict]:
        return [data for event, data in self.events if event == name]


# --------------------------------------------------------------------------
# LogBatcher
# --------------------------------------------------------------------------


@pytest.fixture
def batcher():
    emitter = CapturingEmitter()
    b = LogBatcher(emitter)
    b.emitted = emitter  # type: ignore[attr-defined]
    yield b
    b.stop()


def _feed(batcher, job, count, start=0):
    for i in range(start, start + count):
        batcher.add(job, job.append("stdout", f"line {i}"))


def test_nothing_is_emitted_until_a_flush(batcher):
    job = Job("job-1", "build", {})
    _feed(batcher, job, 3)
    assert batcher.emitted.of("log") == []
    batcher.flush()
    assert len(batcher.emitted.of("log")) == 1


def test_a_batch_carries_the_sequence_of_its_first_line(batcher):
    """The client computes its next expected index as seq + len(lines)."""
    job = Job("job-1", "build", {})
    _feed(batcher, job, 3)
    batcher.flush()

    payload = batcher.emitted.of("log")[0]
    assert payload["job_id"] == "job-1"
    assert payload["seq"] == 0
    assert [line["i"] for line in payload["lines"]] == [0, 1, 2]

    _feed(batcher, job, 2, start=3)
    batcher.flush()
    second = batcher.emitted.of("log")[1]
    assert second["seq"] == 3, "must continue where the previous batch ended"


def test_it_flushes_automatically_at_the_line_limit(batcher):
    job = Job("job-1", "build", {}, log_size=1000)
    _feed(batcher, job, LogBatcher.MAX_LINES)
    # Reached the threshold, so it went out without anyone calling flush.
    assert len(batcher.emitted.of("log")) == 1
    assert len(batcher.emitted.of("log")[0]["lines"]) == LogBatcher.MAX_LINES


def test_it_flushes_automatically_at_the_byte_limit(batcher):
    """A wall of compiler errors must not produce one enormous frame."""
    job = Job("job-1", "build", {}, log_size=1000)
    chunk = "x" * 4096
    for _ in range(12):
        batcher.add(job, job.append("stderr", chunk))

    events = batcher.emitted.of("log")
    assert events, "should have flushed on size before the line limit"
    for payload in events:
        size = sum(len(line["t"]) for line in payload["lines"])
        assert size <= LogBatcher.MAX_BYTES + LogBatcher.MAX_LINE_BYTES


def test_one_pathological_line_is_truncated(batcher):
    job = Job("job-1", "build", {}, log_size=10)
    batcher.add(job, job.append("stderr", "y" * (LogBatcher.MAX_LINE_BYTES * 3)))
    batcher.flush()
    text = batcher.emitted.of("log")[0]["lines"][0]["t"]
    assert len(text) < LogBatcher.MAX_LINE_BYTES * 2
    assert text.endswith("[truncated]")


def test_two_jobs_never_share_a_batch(batcher):
    """A batch names one job_id, so mixing them would misattribute output."""
    first = Job("job-1", "build", {})
    second = Job("job-2", "build", {})
    _feed(batcher, first, 2)
    _feed(batcher, second, 2)
    batcher.flush()

    events = batcher.emitted.of("log")
    assert [e["job_id"] for e in events] == ["job-1", "job-2"]


def test_flushing_with_nothing_pending_emits_nothing(batcher):
    batcher.flush()
    batcher.flush()
    assert batcher.emitted.of("log") == []


def test_the_timer_flushes_without_help(batcher):
    job = Job("job-1", "build", {})
    batcher.start()
    _feed(batcher, job, 2)
    deadline = time.monotonic() + 5
    while not batcher.emitted.of("log") and time.monotonic() < deadline:
        time.sleep(0.05)
    assert batcher.emitted.of("log"), "the periodic flush should have fired"


# --------------------------------------------------------------------------
# fw.build
# --------------------------------------------------------------------------


@pytest.fixture
def api(paths, settings, live_registry_text):
    with open(paths.mcus_json, "w", encoding="utf-8") as fh:
        fh.write(live_registry_text)
    settings.dry_run = True
    runner = JobRunner(paths, lambda: settings)
    a = Api(paths, runner=runner)
    a.settings = lambda: settings  # type: ignore[method-assign]
    yield a
    runner._cancel.set()
    runner.wait(timeout=15)


def _stage_config(paths, mcu_type="bttebb36", fw="klipper"):
    os.makedirs(paths.type_dir(mcu_type), exist_ok=True)
    with open(paths.config_file(mcu_type, fw), "w", encoding="utf-8") as fh:
        fh.write("CONFIG_MACH_STM32=y\n")


def test_build_advertises_itself_when_a_runner_is_present(api):
    caps = api.dispatch("fw.ping")["capabilities"]
    assert "fw.build" in caps and "fw.job.get" in caps and "fw.job.cancel" in caps
    assert api.dispatch("fw.ping")["phase"] == 2
    assert api.dispatch("fw.status")["read_only"] is False


def test_build_returns_a_job_id_immediately(api, paths):
    _stage_config(paths)
    res = api.dispatch("fw.build", {"name": "bttebb36", "fw": "klipper"})
    assert res["job_id"].startswith("job-")
    assert res["job"]["state"] == "running"
    assert api.runner.wait(timeout=30)


def test_build_requires_a_valid_fw(api):
    for args in ({}, {"name": "bttebb36"}, {"name": "bttebb36", "fw": "nonsense"}):
        with pytest.raises(RpcError) as exc:
            api.dispatch("fw.build", args)
        assert exc.value.code == ERR_INVALID_PARAMS


def test_build_rejects_an_unknown_type(api):
    with pytest.raises(RpcError) as exc:
        api.dispatch("fw.build", {"name": "nope", "fw": "klipper"})
    assert exc.value.data["code"] == "unknown_type"


def test_build_refuses_an_unconfigured_type_with_the_exact_command_to_run(api):
    """menuconfig is ncurses and cannot run in the agent, so this has to be a
    clear instruction rather than a job that dies immediately."""
    with pytest.raises(RpcError) as exc:
        api.dispatch("fw.build", {"name": "bttebb36", "fw": "klipper"})
    assert exc.value.data["code"] == "no_saved_config"
    assert "menuconfig -t bttebb36 -f klipper" in str(exc.value)


def test_build_completes_and_records_its_artifact(api, paths):
    _stage_config(paths)
    res = api.dispatch("fw.build", {"name": "bttebb36", "fw": "klipper"})
    assert api.runner.wait(timeout=30)

    job = api.runner.get(res["job_id"])
    assert job.state == "succeeded"
    assert job.result["type"] == "bttebb36"
    assert os.path.exists(job.result["bin_path"])

    # And the artifact is no longer stale in the next status call.
    types = {t["name"]: t for t in api.dispatch("fw.status")["types"]}
    assert types["bttebb36"]["artifacts"]["klipper"]["stale"] is False


def test_a_second_build_while_one_runs_is_refused(api, paths):
    _stage_config(paths)
    _stage_config(paths, "bttmmbv1")
    api.dispatch("fw.build", {"name": "bttebb36", "fw": "klipper"})
    try:
        with pytest.raises(RpcError) as exc:
            api.dispatch("fw.build", {"name": "bttmmbv1", "fw": "klipper"})
        assert exc.value.data["code"] == "busy"
    finally:
        api.runner.wait(timeout=30)


# --------------------------------------------------------------------------
# fw.job.get / fw.job.cancel
# --------------------------------------------------------------------------


def test_job_get_returns_the_job_and_its_log(api, paths):
    _stage_config(paths)
    res = api.dispatch("fw.build", {"name": "bttebb36", "fw": "klipper"})
    assert api.runner.wait(timeout=30)

    got = api.dispatch("fw.job.get", {"job_id": res["job_id"]})
    assert got["job"]["id"] == res["job_id"]
    assert got["log"], "a build should have produced output"
    assert got["log_from"] == 0
    assert got["log_next"] == len(got["log"])
    assert set(got["log"][0]) == {"i", "s", "t"}


def test_job_get_honours_log_from_for_gap_recovery(api, paths):
    _stage_config(paths)
    res = api.dispatch("fw.build", {"name": "bttebb36", "fw": "klipper"})
    assert api.runner.wait(timeout=30)

    everything = api.dispatch("fw.job.get", {"job_id": res["job_id"]})
    total = everything["log_next"]
    tail = api.dispatch("fw.job.get", {"job_id": res["job_id"], "log_from": total - 3})
    assert [line["i"] for line in tail["log"]] == [total - 3, total - 2, total - 1]
    assert tail["log_from"] == total - 3


def test_job_get_with_no_id_returns_the_current_job(api, paths):
    _stage_config(paths)
    api.dispatch("fw.build", {"name": "bttebb36", "fw": "klipper"})
    got = api.dispatch("fw.job.get", {})
    assert got["job"]["kind"] == "build"
    assert api.runner.wait(timeout=30)


def test_job_get_for_an_unknown_id_is_a_typed_error(api):
    with pytest.raises(RpcError) as exc:
        api.dispatch("fw.job.get", {"job_id": "job-999"})
    assert exc.value.data["code"] == "unknown_job"


def test_job_get_rejects_a_non_integer_log_from(api, paths):
    _stage_config(paths)
    res = api.dispatch("fw.build", {"name": "bttebb36", "fw": "klipper"})
    assert api.runner.wait(timeout=30)
    with pytest.raises(RpcError) as exc:
        api.dispatch("fw.job.get", {"job_id": res["job_id"], "log_from": "abc"})
    assert exc.value.code == ERR_INVALID_PARAMS


def test_cancel_reports_that_a_build_stops_immediately(api, paths):
    _stage_config(paths)
    res = api.dispatch("fw.build", {"name": "bttebb36", "fw": "klipper"})
    out = api.dispatch("fw.job.cancel", {"job_id": res["job_id"]})
    assert out["cancelling"] is True
    assert out["immediate"] is True
    assert api.runner.wait(timeout=30)
    assert api.runner.get(res["job_id"]).state in ("cancelled", "succeeded")


def test_cancel_with_no_running_job_is_rejected(api):
    with pytest.raises(RpcError) as exc:
        api.dispatch("fw.job.cancel", {})
    assert exc.value.code == ERR_INVALID_PARAMS


def test_status_surfaces_the_running_job_and_then_history(api, paths):
    _stage_config(paths)
    api.dispatch("fw.build", {"name": "bttebb36", "fw": "klipper"})
    running = api.dispatch("fw.status")
    assert running["job"] is not None
    assert running["job"]["kind"] == "build"

    assert api.runner.wait(timeout=30)
    finished = api.dispatch("fw.status")
    assert finished["job"] is None
    assert finished["recent"][0]["state"] == "succeeded"


def test_job_methods_are_absent_without_a_runner(paths, live_registry_text):
    with open(paths.mcus_json, "w", encoding="utf-8") as fh:
        fh.write(live_registry_text)
    readonly = Api(paths)
    for method in ("fw.build", "fw.job.get", "fw.job.cancel"):
        with pytest.raises(RpcError) as exc:
            readonly.dispatch(method, {})
        assert exc.value.code == ERR_METHOD_NOT_FOUND


def test_calling_a_job_method_directly_without_a_runner_is_a_clean_error(paths):
    """dispatch() filters these out before they get here, so this guard is only
    reachable by calling the method directly - but it must not be a NameError."""
    readonly = Api(paths)
    for call in (
        lambda: readonly.build({"name": "x", "fw": "klipper"}),
        lambda: readonly.job_get({}),
        lambda: readonly.job_cancel({}),
    ):
        with pytest.raises(RpcError) as exc:
            call()
        assert exc.value.code == ERR_METHOD_NOT_FOUND
        assert "read-only" in str(exc.value)
