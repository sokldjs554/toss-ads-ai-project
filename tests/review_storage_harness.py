"""AppTest는 iframe JS를 실행하지 않는다. 여기서는 그 전송 경계만 대체한다.

실제 IndexedDB 트랜잭션·새로고침·격리는 test_review_persistence_browser.py에서 검증한다.
"""


class BrowserStorageHarness:
    def __init__(self):
        self.jsonl = None
        self.revision = 0
        self.write_id = None
        self.failure = None

    def __call__(self, *, command, **kwargs):
        if command is None:
            return None
        out = {"id": command["id"]}
        if self.failure:
            return {**out, "status": "error", "error": self.failure}
        if command["op"] == "load":
            return {**out, "status": "loaded", "revision": self.revision, "jsonl": self.jsonl}
        if self.write_id == command["write_id"] and self.jsonl == command["jsonl"]:
            return {**out, "status": "saved", "revision": self.revision}
        if self.revision != command["expected_revision"]:
            return {**out, "status": "conflict", "revision": self.revision}
        self.jsonl = command["jsonl"]
        self.write_id = command["write_id"]
        self.revision += 1
        return {**out, "status": "saved", "revision": self.revision}
