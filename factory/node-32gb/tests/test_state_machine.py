from __future__ import annotations

import sys
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src import api, graph, run_control


class _Snap:
    def __init__(self, *, next_nodes: list[str] | None = None, values: dict | None = None):
        self.next = next_nodes or []
        self.values = values or {}
        self.metadata = {}


class StateMachineRegressionTests(unittest.TestCase):
    def setUp(self) -> None:
        api._graph = object()
        with api._run_registry_lock:
            api._run_registry.clear()
        with api._run_workers_lock:
            api._run_workers.clear()
        with api._errors_lock:
            api._errors.clear()
        with api._recover_attempts_lock:
            api._recover_attempts.clear()

    def test_start_run_worker_deduplicates_per_thread(self) -> None:
        started = threading.Event()
        release = threading.Event()
        tid = "test-worker-dedupe"

        def _target() -> None:
            started.set()
            release.wait(timeout=1.0)

        self.assertTrue(api._start_run_worker(tid, "start", _target))
        self.assertTrue(started.wait(timeout=0.3))
        self.assertFalse(api._start_run_worker(tid, "recover_continue", _target))

        release.set()
        for _ in range(20):
            if not api._worker_info(tid).get("in_flight"):
                break
            time.sleep(0.02)
        self.assertFalse(api._worker_info(tid).get("in_flight"))

    def test_cancel_marks_immediate_cancel_when_idle_non_interrupt(self) -> None:
        tid = "test-cancel-immediate"
        with api._run_registry_lock:
            api._run_registry[tid] = {"topic": "x", "project_name": "demo"}

        snap = _Snap(next_nodes=["pm_synthesizer"], values={})
        with patch.object(api, "_get_graph_state", return_value=snap):
            out = api.cancel_run(tid)

        self.assertEqual(out["status"], "cancelled")
        self.assertFalse(out["resume_queued"])
        self.assertFalse(out["in_flight"])
        self.assertEqual(api._errors.get(tid), "Run cancelled by user")
        with api._run_registry_lock:
            self.assertTrue(api._run_registry[tid]["cancel_requested"])
        run_control.clear_cancelled(tid)

    def test_cancel_rolls_back_new_registry_entry_if_checkpoint_read_fails(self) -> None:
        tid = "test-cancel-roll-back"
        self.assertFalse(run_control.is_cancelled(tid))
        self.assertNotIn(tid, api._run_registry)

        with patch.object(api, "_get_graph_state", side_effect=Exception("missing thread")):
            with self.assertRaises(api.HTTPException) as ctx:
                api.cancel_run(tid)

        self.assertEqual(ctx.exception.status_code, 502)
        self.assertNotIn(tid, api._run_registry)
        self.assertFalse(run_control.is_cancelled(tid))

    def test_restart_hydrates_registry_from_checkpoint(self) -> None:
        tid = "test-restart-hydrate"
        snap = _Snap(
            next_nodes=["pm_synthesizer"],
            values={
                "topic": "identity sync api",
                "project_name": "demo",
                "max_iterations_before_approval": 3,
            },
        )
        with patch.object(api, "_get_graph_state", return_value=snap):
            with patch.object(api, "_start_run_worker", return_value=True):
                out = api.restart_run(tid)

        self.assertEqual(out["status"], "restart_queued")
        with api._run_registry_lock:
            meta = dict(api._run_registry[tid])
        self.assertEqual(meta["topic"], "identity sync api")
        self.assertEqual(meta["project_name"], "demo")
        self.assertEqual(meta["max_iterations_before_approval"], 3)

    def test_builder_agent_raises_on_nonzero_exit(self) -> None:
        state = {
            "thread_id": "test-builder-fail",
            "project_name": "demo",
            "topic": "topic",
            "architecture_spec": "spec",
        }
        with patch.object(graph, "run_aider", return_value=("build logs", 2)):
            with self.assertRaises(RuntimeError) as ctx:
                graph.builder_agent(state)
        self.assertIn("Builder failed with exit code 2", str(ctx.exception))

    def test_builder_agent_raises_when_cancelled_after_build(self) -> None:
        tid = "test-builder-cancel-post"
        state = {
            "thread_id": tid,
            "project_name": "demo",
            "topic": "topic",
            "architecture_spec": "spec",
        }
        run_control.mark_cancelled(tid)
        try:
            with patch.object(graph, "run_aider", return_value=("build logs", 0)):
                with self.assertRaises(RuntimeError) as ctx:
                    graph.builder_agent(state)
            self.assertIn("Run cancelled by user", str(ctx.exception))
        finally:
            run_control.clear_cancelled(tid)


if __name__ == "__main__":
    unittest.main()
