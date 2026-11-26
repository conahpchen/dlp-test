from entries.cowrie.runtime.follow_events import follow


def test_bridge_resumes_at_last_handled_record_across_restart(tmp_path):
    log = tmp_path / "cowrie.json"
    cursor = tmp_path / "cowrie.cursor"
    log.write_bytes(b'{"sequence":1}\n{"sequence":2}\n')

    stream = follow(log, cursor)
    assert next(stream) == b'{"sequence":1}\n'
    assert next(stream) == b'{"sequence":2}\n'
    stream.close()

    resumed = follow(log, cursor)
    assert next(resumed) == b'{"sequence":2}\n'
    resumed.close()


def test_bridge_follows_complete_records_after_rotation(tmp_path):
    log = tmp_path / "cowrie.json"
    cursor = tmp_path / "cowrie.cursor"
    log.write_bytes(b'{"sequence":1}\n{"partial":')

    stream = follow(log, cursor)
    assert next(stream) == b'{"sequence":1}\n'
    log.rename(tmp_path / "cowrie.json.1")
    log.write_bytes(b'{"sequence":2}\n')

    assert next(stream) == b'{"sequence":2}\n'
    stream.close()
