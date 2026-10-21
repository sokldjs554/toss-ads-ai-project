"""Independent regression for the durable-key boundary on administrator retry."""

import json
import os
import stat

from ops import team_review as team
from scripts import team_review_admin as admin


def test_resume_after_failed_directory_fsync_does_not_rotate_before_durable_key(tmp_path, monkeypatch):
    class MemoryStore:
        def __init__(self):
            self.owner = team.new_access_key()
            self.state = team.new_state(team.key_digest(self.owner))
            self.calls = []

        def execute(self, key, action, payload=None, *, request_id):
            self.calls.append(action)
            self.state, result = team.execute(
                self.state, key, action, payload if payload is not None else {}, request_id=request_id,
            )
            return result

    store = MemoryStore()
    result_file = tmp_path / "owner-operation.json"
    environ = {
        "PREFLIGHT_TEAM_DATABASE_URL": "postgresql://controlled_admin@localhost/controlled_review",
        "PREFLIGHT_TEAM_OWNER_KEY": store.owner,
    }
    original_fsync = os.fsync

    def fail_directory_fsync(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError("controlled directory fsync failure")
        original_fsync(fd)

    monkeypatch.setattr(os, "fsync", fail_directory_fsync)
    factory = lambda *args, **kwargs: store
    assert admin.main(["rotate-owner", "--out", str(result_file)], environ=environ, store_factory=factory) != 0
    assert store.calls == []
    # Publication succeeded before fsync failed: readable does not mean durable.
    assert json.loads(result_file.read_text())["status"] == "prepared"

    assert admin.main(["resume", "--out", str(result_file)], environ=environ, store_factory=factory) != 0
    assert store.calls == [], "A retry must re-establish durable key storage before mutating the database."
    assert team.authenticate(store.state, store.owner)["id"] == team.OWNER_ID
