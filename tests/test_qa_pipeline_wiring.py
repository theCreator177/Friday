"""Regression tests for the auto-QA pipeline wiring in server.py.

ClaudeTaskManager._run_qa reaches for three module-level names — `qa_agent`,
`success_tracker`, and `suggest_followup`. They were referenced but never
imported or instantiated, so every completed task raised NameError on the
first line of _run_qa's try block. The bare `except Exception` logged it and
moved on, so auto-QA, auto-retry, follow-up suggestions and success tracking
were all silently dead while appearing to be wired up.

Nothing caught it because no test ever entered _run_qa. These tests do.
"""

import asyncio
import os
import sys
import unittest
from datetime import datetime
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server  # noqa: E402
from server import ClaudeTask, ClaudeTaskManager  # noqa: E402


def run(coro):
    # asyncio.run closes the loop it creates, so tests don't leak one apiece.
    return asyncio.run(coro)


def make_task(status="completed"):
    return ClaudeTask(
        id="t-1",
        prompt="build a landing page",
        status=status,
        working_dir=".",
        result="done",
        started_at=datetime.now(),
        completed_at=datetime.now(),
    )


class DependenciesResolveTest(unittest.TestCase):
    """The three names _run_qa depends on must exist with a usable interface."""

    def test_qa_agent_exists_with_expected_methods(self):
        self.assertTrue(hasattr(server, "qa_agent"))
        self.assertTrue(callable(getattr(server.qa_agent, "verify", None)))
        self.assertTrue(callable(getattr(server.qa_agent, "auto_retry", None)))

    def test_success_tracker_exists_with_expected_methods(self):
        self.assertTrue(hasattr(server, "success_tracker"))
        self.assertTrue(callable(getattr(server.success_tracker, "log_task", None)))
        self.assertTrue(callable(getattr(server.success_tracker, "log_suggestion", None)))

    def test_suggest_followup_is_callable(self):
        self.assertTrue(callable(getattr(server, "suggest_followup", None)))


class RunQaExecutesTest(unittest.TestCase):
    """_run_qa must complete without swallowing a NameError.

    The original bug hid inside `except Exception` — the method 'succeeded'
    while doing nothing. Asserting the collaborators were actually called is
    what distinguishes working code from silently-dead code.
    """

    def setUp(self):
        self.mgr = ClaudeTaskManager(max_concurrent=1)
        self.task = make_task()

    def test_passing_qa_logs_success_and_notifies(self):
        qa_result = mock.Mock(passed=True, summary="looks good", issues=[])
        with mock.patch.object(server, "qa_agent") as agent, \
             mock.patch.object(server, "success_tracker") as tracker, \
             mock.patch.object(server, "suggest_followup", return_value=None), \
             mock.patch.object(ClaudeTaskManager, "_notify", new=mock.AsyncMock()) as notify:
            agent.verify = mock.AsyncMock(return_value=qa_result)
            run(self.mgr._run_qa(self.task))

        agent.verify.assert_awaited_once()
        tracker.log_task.assert_called_once()
        self.assertTrue(tracker.log_task.call_args.args[2], "success flag should be True")
        notify.assert_awaited()

    def test_suggestion_is_logged_and_pushed_when_offered(self):
        qa_result = mock.Mock(passed=True, summary="ok", issues=[])
        suggestion = mock.Mock(text="add a favicon", action_type="build",
                               action_details={"path": "."})
        with mock.patch.object(server, "qa_agent") as agent, \
             mock.patch.object(server, "success_tracker") as tracker, \
             mock.patch.object(server, "suggest_followup", return_value=suggestion), \
             mock.patch.object(ClaudeTaskManager, "_notify", new=mock.AsyncMock()) as notify:
            agent.verify = mock.AsyncMock(return_value=qa_result)
            run(self.mgr._run_qa(self.task))

        tracker.log_suggestion.assert_called_once_with("t-1", "add a favicon")
        kinds = [c.args[0]["type"] for c in notify.await_args_list]
        self.assertIn("suggestion", kinds)

    def test_failing_qa_triggers_auto_retry(self):
        failed = mock.Mock(passed=False, summary="broken", issues=["no tests"])
        with mock.patch.object(server, "qa_agent") as agent, \
             mock.patch.object(server, "success_tracker") as tracker, \
             mock.patch.object(ClaudeTaskManager, "_notify", new=mock.AsyncMock()):
            agent.verify = mock.AsyncMock(return_value=failed)
            agent.auto_retry = mock.AsyncMock(return_value={"status": "failed", "result": ""})
            run(self.mgr._run_qa(self.task, attempt=1))

        agent.auto_retry.assert_awaited_once()
        tracker.log_task.assert_called_once()
        self.assertFalse(tracker.log_task.call_args.args[2], "success flag should be False")

    def test_retry_ceiling_stops_at_third_attempt(self):
        failed = mock.Mock(passed=False, summary="broken", issues=["still broken"])
        with mock.patch.object(server, "qa_agent") as agent, \
             mock.patch.object(server, "success_tracker") as tracker, \
             mock.patch.object(ClaudeTaskManager, "_notify", new=mock.AsyncMock()):
            agent.verify = mock.AsyncMock(return_value=failed)
            agent.auto_retry = mock.AsyncMock()
            run(self.mgr._run_qa(self.task, attempt=3))

        agent.auto_retry.assert_not_awaited()
        tracker.log_task.assert_called_once()


if __name__ == "__main__":
    unittest.main(verbosity=2)
