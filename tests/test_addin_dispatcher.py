"""Tests for the main thread dispatcher and the thread safe log of fusion_adapter.

A small "main thread" is simulated: `fireCustomEvent` posts the registered custom event handler
to a dedicated thread, which is what Fusion does when it is idle. No Fusion is needed beyond the
official adsk stubs (skipped when they are missing).
"""

from __future__ import annotations

import importlib
import queue
import threading
import time
from collections.abc import Callable, Iterator
from types import ModuleType
from typing import Any

import pytest

pytestmark = pytest.mark.usefixtures("adsk_stubs")

WAIT_S = 5.0


@pytest.fixture(scope="module")
def fa(adsk_stubs: Any) -> ModuleType:
    return importlib.import_module("fusion_adapter")


# Simulated Fusion ----------------------------------------------------------------------------


class FakeEvent:
    def __init__(self) -> None:
        self.handlers: list[Any] = []

    def add(self, handler: Any) -> bool:
        self.handlers.append(handler)
        return True

    def remove(self, handler: Any) -> bool:
        self.handlers.remove(handler)
        return True


class MainLoop:
    """A thread that runs posted callables one after the other, like Fusion's main thread."""

    def __init__(self) -> None:
        self._tasks: queue.Queue[Callable[[], None] | None] = queue.Queue()
        self._thread = threading.Thread(target=self._run, name="fake-main", daemon=True)
        self._thread.start()

    @property
    def ident(self) -> int:
        assert self._thread.ident is not None
        return self._thread.ident

    def post(self, task: Callable[[], None]) -> None:
        self._tasks.put(task)

    def call(self, fn: Callable[[], Any]) -> Any:
        """Run `fn` on the loop thread and return its result (or raise its exception)."""
        outcome: queue.Queue[tuple[bool, Any]] = queue.Queue()

        def task() -> None:
            try:
                outcome.put((True, fn()))
            except Exception as exc:  # re-raised in the calling thread
                outcome.put((False, exc))

        self.post(task)
        ok, value = outcome.get(timeout=WAIT_S)
        if not ok:
            raise value
        return value

    def block(self) -> Callable[[], None]:
        """Keep the loop busy (a modal dialog) until the returned function is called."""
        started = threading.Event()
        release = threading.Event()

        def task() -> None:
            started.set()
            release.wait(WAIT_S * 4)

        self.post(task)
        assert started.wait(WAIT_S)
        return release.set

    def close(self) -> None:
        self._tasks.put(None)
        self._thread.join(WAIT_S)

    def _run(self) -> None:
        while True:
            task = self._tasks.get()
            if task is None:
                return
            task()


class LoopApp:
    """Fake `adsk.core.Application` whose custom events run on the simulated main thread."""

    def __init__(self, loop: MainLoop) -> None:
        self.loop = loop
        self.registered: dict[str, FakeEvent] = {}
        self.unregistered: list[str] = []
        self.fired: list[tuple[str, str]] = []
        self.fire_error: Exception | None = None
        self.fire_result = True
        self.logged: list[str] = []

    def log(self, message: str, *args: Any) -> None:
        self.logged.append(message)

    def registerCustomEvent(self, event_id: str) -> FakeEvent:
        event = FakeEvent()
        self.registered[event_id] = event
        return event

    def unregisterCustomEvent(self, event_id: str) -> bool:
        self.unregistered.append(event_id)
        return True

    def fireCustomEvent(self, event_id: str, info: str = "") -> bool:
        if self.fire_error is not None:
            raise self.fire_error
        self.fired.append((event_id, info))
        event = self.registered.get(event_id)
        if event is not None:
            self.loop.post(lambda: [handler.notify(None) for handler in list(event.handlers)])
        return self.fire_result


@pytest.fixture
def loop() -> Iterator[MainLoop]:
    main = MainLoop()
    yield main
    main.close()


@pytest.fixture
def app(fa: ModuleType, loop: MainLoop, monkeypatch: pytest.MonkeyPatch) -> LoopApp:
    fake = LoopApp(loop)
    monkeypatch.setattr(fa.adsk.core.Application, "get", staticmethod(lambda: fake))
    return fake


class Recorder:
    """A `handle` function that records every call and the thread it ran on."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, int]] = []
        self.error: Exception | None = None
        self.gate: threading.Event | None = None

    def __call__(self, action: str, data: str) -> str:
        self.calls.append((action, data, threading.get_ident()))
        if self.gate is not None:
            self.gate.wait(WAIT_S * 4)
        if self.error is not None:
            raise self.error
        return f"reply:{action}:{data}"


def make_dispatcher(
    fa: ModuleType,
    app: LoopApp,
    loop: MainLoop,
    handle: Recorder | None = None,
    timeout_s: float = WAIT_S,
) -> tuple[Any, Any, Recorder]:
    """A started dispatcher (started on the loop thread, which becomes the 'main thread')."""
    recorder = handle if handle is not None else Recorder()
    safe_log = fa.ThreadSafeLog(direct=lambda message: app.logged.append(f"direct:{message}"))
    dispatcher = fa.MainThreadDispatcher(
        app, "test_event", handle=recorder, safe_log=safe_log, timeout_s=timeout_s
    )
    loop.call(dispatcher.start)
    return dispatcher, safe_log, recorder


def run_in_threads(
    fn: Callable[[int], Any], count: int
) -> tuple[list[threading.Thread], dict[int, Any]]:
    results: dict[int, Any] = {}

    def work(index: int) -> None:
        try:
            results[index] = fn(index)
        except Exception as exc:  # compared by the test
            results[index] = exc

    threads = [threading.Thread(target=work, args=(index,), daemon=True) for index in range(count)]
    for thread in threads:
        thread.start()
    return threads, results


def wait_until(condition: Callable[[], bool], timeout_s: float = WAIT_S) -> None:
    deadline = time.monotonic() + timeout_s
    while not condition():
        assert time.monotonic() < deadline, "condition not reached in time"
        time.sleep(0.005)


# MainThreadDispatcher ------------------------------------------------------------------------


def test_dispatch_runs_the_handler_on_the_main_thread_and_returns_the_reply(
    fa: ModuleType, app: LoopApp, loop: MainLoop
) -> None:
    dispatcher, _, recorder = make_dispatcher(fa, app, loop)

    assert dispatcher.dispatch("search", '{"query": "C2040"}') == 'reply:search:{"query": "C2040"}'

    assert recorder.calls == [("search", '{"query": "C2040"}', loop.ident)]
    assert threading.get_ident() != loop.ident
    assert app.fired[0][0] == "test_event"


def test_start_registers_the_event_and_stop_removes_it(
    fa: ModuleType, app: LoopApp, loop: MainLoop
) -> None:
    dispatcher, _, _ = make_dispatcher(fa, app, loop)
    event = app.registered["test_event"]
    assert len(event.handlers) == 1
    assert app.unregistered == ["test_event"]  # leftover of an unclean stop removed before

    loop.call(dispatcher.stop)
    assert event.handlers == []
    assert app.unregistered == ["test_event", "test_event"]
    loop.call(dispatcher.stop)  # idempotent


def test_start_fails_clearly_when_the_event_cannot_be_registered(
    fa: ModuleType, app: LoopApp
) -> None:
    app.registerCustomEvent = lambda event_id: None  # type: ignore[method-assign,assignment]
    with pytest.raises(RuntimeError, match="custom event"):
        fa.MainThreadDispatcher(app, "test_event").start()


def test_many_threads_get_their_own_reply_and_all_run_on_the_main_thread(
    fa: ModuleType, app: LoopApp, loop: MainLoop
) -> None:
    dispatcher, _, recorder = make_dispatcher(fa, app, loop)

    threads, results = run_in_threads(lambda i: dispatcher.dispatch("act", str(i)), 40)
    for thread in threads:
        thread.join(WAIT_S)

    assert results == {i: f"reply:act:{i}" for i in range(40)}
    assert {ident for _, _, ident in recorder.calls} == {loop.ident}
    assert sorted(int(data) for _, data, _ in recorder.calls) == list(range(40))


def test_requests_run_in_the_order_they_were_queued(
    fa: ModuleType, app: LoopApp, loop: MainLoop
) -> None:
    dispatcher, _, recorder = make_dispatcher(fa, app, loop)
    release = loop.block()  # Fusion is busy, the requests pile up
    threads = []
    for index in range(6):
        thread = threading.Thread(target=dispatcher.dispatch, args=("act", str(index)), daemon=True)
        thread.start()
        threads.append(thread)
        wait_until(lambda n=index: len(dispatcher._queue) == n + 1)

    release()
    for thread in threads:
        thread.join(WAIT_S)

    assert [data for _, data, _ in recorder.calls] == [str(i) for i in range(6)]


def test_a_handler_exception_reaches_the_caller_and_does_not_break_the_dispatcher(
    fa: ModuleType, app: LoopApp, loop: MainLoop
) -> None:
    dispatcher, _, recorder = make_dispatcher(fa, app, loop)
    recorder.error = ValueError("boom")
    with pytest.raises(ValueError, match="boom"):
        dispatcher.dispatch("act", "1")

    recorder.error = None
    assert dispatcher.dispatch("act", "2") == "reply:act:2"


def test_a_timeout_error_raised_by_the_handler_is_passed_on_unchanged(
    fa: ModuleType, app: LoopApp, loop: MainLoop
) -> None:
    dispatcher, _, recorder = make_dispatcher(fa, app, loop)
    recorder.error = TimeoutError("network timed out")
    with pytest.raises(TimeoutError, match="network timed out"):
        dispatcher.dispatch("act", "1")


def test_a_request_without_a_handler_fails_clearly(
    fa: ModuleType, app: LoopApp, loop: MainLoop
) -> None:
    dispatcher = fa.MainThreadDispatcher(app, "test_event", timeout_s=WAIT_S)
    loop.call(dispatcher.start)
    with pytest.raises(fa.DispatcherStoppedError, match="not ready"):
        dispatcher.dispatch("act", "1")


def test_a_timed_out_request_is_dropped_and_its_action_never_runs(
    fa: ModuleType, app: LoopApp, loop: MainLoop
) -> None:
    dispatcher, _, recorder = make_dispatcher(fa, app, loop, timeout_s=0.2)
    release = loop.block()

    with pytest.raises(TimeoutError, match="did not process 'act'"):
        dispatcher.dispatch("act", "late")

    assert len(dispatcher._queue) == 0  # nothing leaks
    release()
    loop.call(lambda: None)  # the loop is idle again, the stale event has been handled
    assert recorder.calls == []


def test_a_request_that_is_already_running_when_the_caller_gives_up_runs_exactly_once(
    fa: ModuleType, app: LoopApp, loop: MainLoop
) -> None:
    recorder = Recorder()
    recorder.gate = threading.Event()
    dispatcher, _, _ = make_dispatcher(fa, app, loop, recorder, timeout_s=0.2)

    with pytest.raises(TimeoutError):
        dispatcher.dispatch("act", "slow")
    assert len(recorder.calls) == 1  # it is in progress on the main thread

    recorder.gate.set()
    loop.call(lambda: None)
    assert len(recorder.calls) == 1
    assert len(dispatcher._queue) == 0


def test_close_wakes_the_waiting_requests_with_an_error_and_refuses_new_ones(
    fa: ModuleType, app: LoopApp, loop: MainLoop
) -> None:
    dispatcher, _, recorder = make_dispatcher(fa, app, loop, timeout_s=30)
    release = loop.block()
    threads, results = run_in_threads(lambda i: dispatcher.dispatch("act", str(i)), 3)
    wait_until(lambda: len(dispatcher._queue) == 3)

    started = time.monotonic()
    dispatcher.close()  # no Fusion call needed: works while the main thread is busy
    for thread in threads:
        thread.join(WAIT_S)

    assert time.monotonic() - started < WAIT_S
    assert all(isinstance(result, fa.DispatcherStoppedError) for result in results.values())
    with pytest.raises(fa.DispatcherStoppedError):
        dispatcher.dispatch("act", "after")
    release()
    loop.call(lambda: None)
    assert recorder.calls == []  # nothing ran after the stop


def test_a_failing_fire_custom_event_takes_the_request_back(
    fa: ModuleType, app: LoopApp, loop: MainLoop
) -> None:
    dispatcher, _, recorder = make_dispatcher(fa, app, loop)
    app.fire_error = RuntimeError("fusion is closing")
    with pytest.raises(RuntimeError, match="fusion is closing"):
        dispatcher.dispatch("act", "1")
    assert len(dispatcher._queue) == 0

    app.fire_error = None
    app.fire_result = False  # the event is not registered or Fusion refused it
    with pytest.raises(RuntimeError, match="did not accept"):
        dispatcher.dispatch("act", "2")
    assert len(dispatcher._queue) == 0
    loop.call(lambda: None)
    assert recorder.calls == []


def test_too_many_waiting_requests_are_refused(
    fa: ModuleType, app: LoopApp, loop: MainLoop, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(fa, "MAX_PENDING_REQUESTS", 2)
    dispatcher, _, _ = make_dispatcher(fa, app, loop, timeout_s=30)
    release = loop.block()
    threads, results = run_in_threads(lambda i: dispatcher.dispatch("act", str(i)), 2)
    wait_until(lambda: len(dispatcher._queue) == 2)

    with pytest.raises(fa.DispatcherBusyError):
        dispatcher.dispatch("act", "one too many")

    release()
    for thread in threads:
        thread.join(WAIT_S)
    assert sorted(results.values()) == ["reply:act:0", "reply:act:1"]


def test_stopping_in_the_middle_of_a_burst_leaves_no_thread_hanging(
    fa: ModuleType, app: LoopApp, loop: MainLoop
) -> None:
    dispatcher, _, _ = make_dispatcher(fa, app, loop, timeout_s=30)

    threads, results = run_in_threads(lambda i: _dispatch_until_stopped(fa, dispatcher, i), 8)
    time.sleep(0.05)
    dispatcher.close()
    loop.call(dispatcher.stop)
    for thread in threads:
        thread.join(WAIT_S)

    assert not [thread for thread in threads if thread.is_alive()]
    assert all(result == "stopped" for result in results.values())


def _dispatch_until_stopped(fa: ModuleType, dispatcher: Any, index: int) -> str:
    while True:
        try:
            dispatcher.dispatch("act", str(index))
        except fa.DispatcherStoppedError:
            return "stopped"


# ThreadSafeLog -------------------------------------------------------------------------------


def test_log_from_the_main_thread_is_written_at_once(
    fa: ModuleType, app: LoopApp, loop: MainLoop
) -> None:
    _, safe_log, _ = make_dispatcher(fa, app, loop)

    loop.call(lambda: safe_log("on main"))

    assert app.logged == ["direct:on main"]
    assert app.fired == []  # no wake up needed


def test_log_from_another_thread_is_queued_and_flushed_on_the_main_thread(
    fa: ModuleType, app: LoopApp, loop: MainLoop
) -> None:
    written: list[tuple[str, int]] = []
    safe_log = fa.ThreadSafeLog(direct=lambda m: written.append((m, threading.get_ident())))
    dispatcher = fa.MainThreadDispatcher(app, "test_event", safe_log=safe_log)
    loop.call(dispatcher.start)

    safe_log("from the test thread")  # not the main thread of the add-in

    wait_until(lambda: bool(written))
    assert written == [("from the test thread", loop.ident)]
    assert app.fired[-1][0] == "test_event"  # the dispatcher event carried the flush


def test_log_lines_keep_their_order_across_threads(
    fa: ModuleType, app: LoopApp, loop: MainLoop
) -> None:
    written: list[str] = []
    safe_log = fa.ThreadSafeLog(direct=written.append)
    dispatcher = fa.MainThreadDispatcher(app, "test_event", safe_log=safe_log)
    loop.call(dispatcher.start)
    release = loop.block()  # lines pile up while the main thread is busy

    for number in range(3):
        safe_log(f"other {number}")
    release()
    loop.call(lambda: safe_log("main last"))

    assert written == ["other 0", "other 1", "other 2", "main last"]


def test_drain_flushes_queued_log_lines(fa: ModuleType, app: LoopApp, loop: MainLoop) -> None:
    dispatcher, safe_log, _ = make_dispatcher(fa, app, loop)
    release = loop.block()
    safe_log("queued while busy")
    assert "direct:queued while busy" not in app.logged

    release()
    loop.call(dispatcher.drain)

    assert "direct:queued while busy" in app.logged


def test_log_survives_a_failing_wake_up_and_a_stopped_dispatcher(
    fa: ModuleType, app: LoopApp, loop: MainLoop
) -> None:
    dispatcher, safe_log, _ = make_dispatcher(fa, app, loop)
    app.fire_error = RuntimeError("fusion is closing")
    safe_log("still queued")  # must not raise
    assert "direct:still queued" not in app.logged

    app.fire_error = None
    loop.call(dispatcher.stop)  # unbinds and flushes
    assert "direct:still queued" in app.logged
    fired_before = len(app.fired)
    safe_log("after stop")  # queued without waking anybody
    assert len(app.fired) == fired_before
    loop.call(safe_log.flush)
    assert "direct:after stop" in app.logged


def test_log_queue_is_bounded(fa: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fa, "MAX_PENDING_LOG_LINES", 3)
    written: list[str] = []
    safe_log = fa.ThreadSafeLog(direct=written.append)
    safe_log._main_ident = -1  # every real thread counts as "another thread"
    for number in range(10):
        safe_log(f"line {number}")
    safe_log._main_ident = threading.get_ident()
    safe_log.flush()
    assert written == ["line 7", "line 8", "line 9"]


def test_unbound_log_writes_directly(fa: ModuleType) -> None:
    written: list[str] = []
    fa.ThreadSafeLog(direct=written.append)("hello")
    assert written == ["hello"]
