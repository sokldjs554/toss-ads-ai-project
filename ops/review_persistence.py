"""검수 큐 자동 저장 프로토콜. 저장 완료는 브라우저의 트랜잭션 완료 응답으로만 확정한다.

실제 저장소는 ui/review_storage_frontend/storage.js의 방문자별 IndexedDB다.
이 클래스는 UI와 저장소 사이에서 복원 전 편집, 오래된 응답, 손상된 JSONL,
충돌 뒤 덮어쓰기를 막는다. 저장 위치나 서버 전역 큐를 만들지 않는다.
"""
from __future__ import annotations

from typing import Any
from uuid import uuid4

from ops.review_queue import QueueFormatError, ReviewQueue


def _request(op: str, **values: Any) -> dict:
    return {"id": uuid4().hex, "op": op, **values}


def _revision(value: Any) -> bool:
    return type(value) is int and 0 <= value <= 2**53 - 1


class QueuePersistence:
    """세션별 저장 상태. UI는 ready일 때만 큐를 변경할 수 있다."""

    def __init__(self) -> None:
        self.revision = 0
        self.error: str | None = None
        self.recovery_jsonl: str | None = None
        self._loaded = False
        self._saved_jsonl: str | None = None
        self._pending: dict | None = _request("load")

    @property
    def ready(self) -> bool:
        return self._loaded and self._pending is None and self.error is None

    @property
    def saving(self) -> bool:
        return bool(self._pending and self._pending["op"] == "save")

    @property
    def unsaved_jsonl(self) -> str | None:
        return self._pending.get("jsonl") if self._pending else None

    def command(self, queue: ReviewQueue | None = None) -> dict | None:
        """확인 대기 중인 요청을 재사용한다. 실패·충돌이면 명시적 재시도까지 멈춘다."""
        if self.error:
            return None
        if self.ready and queue is not None:
            text = queue.to_jsonl()
            if text != self._saved_jsonl:
                # 저장에도 가져오기와 같은 형식·상태·인용 검증을 적용한다.
                ReviewQueue.from_jsonl(text)
                self._pending = _request("save", write_id=uuid4().hex, expected_revision=self.revision, jsonl=text)
        return dict(self._pending) if self._pending else None

    def accept(self, response: Any) -> ReviewQueue | None:
        """요청 id가 일치하는 응답만 처리하고, 유효한 복원본만 호출자에게 돌려준다."""
        if response is None or self._pending is None or self.error:
            return None
        if not isinstance(response, dict):
            self.error = "invalid"
            return None
        if response.get("id") != self._pending["id"]:
            return None
        status = response.get("status")
        if status == "error":
            kind = response.get("error")
            self.error = kind if kind in {"quota", "denied", "unavailable", "aborted", "invalid"} else "unavailable"
            return None
        if status == "conflict" and self._pending["op"] == "save":
            self.error = "conflict"
            return None
        revision = response.get("revision")
        if not _revision(revision):
            self.error = "invalid"
            return None
        if status == "loaded" and self._pending["op"] == "load":
            text = response.get("jsonl")
            try:
                if revision == 0 and text is None:
                    queue = ReviewQueue()
                elif revision > 0 and isinstance(text, str):
                    queue = ReviewQueue.from_jsonl(text)
                else:
                    raise QueueFormatError("저장본 형식이 올바르지 않습니다")
            except (QueueFormatError, ValueError, TypeError, AttributeError, OverflowError):
                self.error = "invalid"
                self.recovery_jsonl = text if isinstance(text, str) else None
                return None
            self.revision = revision
            self._saved_jsonl = queue.to_jsonl()
            self._loaded = True
            self._pending = None
            self.recovery_jsonl = None
            return queue
        if status == "saved" and self._pending["op"] == "save" and revision == self._pending["expected_revision"] + 1:
            self.revision = revision
            self._saved_jsonl = self._pending["jsonl"]
            self._pending = None
            return None
        self.error = "invalid"
        return None

    def retry(self) -> None:
        """재시도 id는 새로 만들되, 저장 식별자는 유지해 응답 유실도 중복 저장하지 않는다."""
        if self.error == "conflict":
            raise ValueError("저장 충돌은 최신 저장본을 불러와 해결해야 합니다")
        if self._pending:
            self._pending = {**self._pending, "id": uuid4().hex}
        self.error = None

    def reload(self) -> None:
        """UI에서 사용자가 선택한 경우에만 미저장 요청 대신 최신본 읽기를 요청한다."""
        self._pending = _request("load")
        self.error = None
