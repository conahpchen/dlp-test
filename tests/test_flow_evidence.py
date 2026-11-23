"""An invalid import may be corrected and retried without partial evidence."""

import pytest

from audit.flow_evidence import HEADER, ingest


def test_flow_import_is_atomic_and_reports_unobserved_records(tmp_path):
    path = tmp_path / "flows.sqlite"
    key = "test-key-for-flow-pseudonyms-and-import"
    line = "2 123456789012 eni-abcdef98 192.0.2.1 198.51.100.1 50000 22 6 2 84 100 102 ACCEPT OK"
    with pytest.raises(ValueError, match="line 2"):
        ingest(path, key, [line, "invalid record"])
    assert not path.exists()
    missing = "2 123456789012 eni-abcdef98 - - - - - - - 100 102 - SKIPDATA"
    assert ingest(path, key, [HEADER, line, missing]) == {
        "duplicates": 0, "inserted": 1, "unobserved": 1, "not_eligible": 0,
    }
    assert ingest(path, key, [line])["duplicates"] == 1
    stored = path.read_bytes()
    assert b"192.0.2.1" not in stored and b"198.51.100.1" not in stored
