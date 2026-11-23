from audit.situation_model import assess_next
from audit.situations import list_reports, publish_reports


def test_unsupported_blocking_advice_leaves_original_observation(monkeypatch, tmp_path):
    from audit.alerts import record

    monkeypatch.setenv("HONEYNET_SITUATION_REPORT_FILE", str(tmp_path / "situations.sqlite"))
    monkeypatch.setenv("HONEYNET_ALERTS_FILE", str(tmp_path / "alerts.sqlite"))
    monkeypatch.setenv("RESEARCH_STATE_FILE", str(tmp_path / "unused.sqlite"))
    record("ssh", "connection")
    assert publish_reports() == 1

    class Analyst:
        name = "synthetic-analyst"

        def choose(self, messages):
            return {"assessment": "probing", "severity": "high", "recommendations": [
                {"action": "review_block", "evidence": ["alert:ssh:connection"]},
            ]}

    assert assess_next(Analyst)
    (report,) = list_reports()
    assert report["analysis"] == {"status": "unavailable"}
    assert report["alerts"] == [{"entry": "ssh", "kind": "connection", "count": 1}]
    assert not assess_next(Analyst)


def test_empty_window_does_not_call_provider(monkeypatch, tmp_path):
    monkeypatch.setenv("HONEYNET_SITUATION_REPORT_FILE", str(tmp_path / "situations.sqlite"))
    monkeypatch.setenv("HONEYNET_ALERTS_FILE", str(tmp_path / "unused-alerts.sqlite"))
    monkeypatch.setenv("RESEARCH_STATE_FILE", str(tmp_path / "unused.sqlite"))
    assert publish_reports() == 1

    def unexpected():
        raise AssertionError("No model call is needed without signals")

    assert assess_next(unexpected)
    (report,) = list_reports()
    assert report["analysis"] == {"status": "no_evidence"}
