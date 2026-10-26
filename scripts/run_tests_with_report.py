
"""Run the test suite and write an evaluation/test_results.json report."""

import json
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parent.parent
REPORT_PATH = ROOT / "evaluation" / "test_results.json"


class TestReportPlugin:
    def __init__(self):
        self.total = 0
        self.outcomes = {}

    def pytest_collection_finish(self, session):
        self.total = len(session.items)

    def pytest_runtest_logreport(self, report):
        nodeid = report.nodeid

        if report.when == "setup" and report.failed:
            self.outcomes[nodeid] = "failed"
        elif report.when == "setup" and report.skipped:
            self.outcomes[nodeid] = "skipped"
        elif report.when == "call":
            self.outcomes[nodeid] = report.outcome
        elif report.when == "teardown" and report.failed:
            self.outcomes[nodeid] = "failed"

    def pytest_sessionfinish(self, session, exitstatus):
        REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)

        passed = sum(value == "passed" for value in self.outcomes.values())
        failed = sum(value == "failed" for value in self.outcomes.values())
        skipped = sum(value == "skipped" for value in self.outcomes.values())
        unreported = max(
            self.total - passed - failed - skipped,
            0,
        )

        report = {
            "test_cases_total": self.total,
            "test_cases_passed": passed,
            "test_cases_failed": failed,
            "test_cases_skipped": skipped,
            "test_cases_unreported": unreported,
            "test_pass_rate_pct": (
                round(passed / self.total * 100, 2)
                if self.total else None
            ),
            "pytest_exit_code": int(exitstatus),
        }

        REPORT_PATH.write_text(
            json.dumps(report, indent=2),
            encoding="utf-8",
        )


if __name__ == "__main__":
    plugin = TestReportPlugin()
    exit_code = pytest.main(["tests", "-q"], plugins=[plugin])

    print(f"\nTest evaluation report: {REPORT_PATH}")
    print(REPORT_PATH.read_text(encoding="utf-8"))

    raise SystemExit(int(exit_code))
