"""검수 대기열의 방문자별 브라우저 자동 저장 연결. Python 서버 디스크를 쓰지 않는다."""
from __future__ import annotations

from pathlib import Path

import streamlit as st
import streamlit.components.v1 as components

from ops.review_persistence import QueuePersistence
from ops.review_queue import QueueFormatError

_NAMESPACE = "preflight-studio-review-v1"
_FRONTEND = Path(__file__).with_name("review_storage_frontend")


def _component(**kwargs):
    return components.declare_component("review_queue_storage", path=str(_FRONTEND))(**kwargs)


def _store() -> QueuePersistence:
    if "rq_persistence" not in st.session_state:
        if "rq_queue" in st.session_state:
            st.session_state["rq_legacy_queue"] = st.session_state["rq_queue"]
        st.session_state["rq_persistence"] = QueuePersistence()
    return st.session_state["rq_persistence"]


def backup_download(text: str, *, file_name: str, label: str, key: str) -> None:
    """서버가 없는 실행판에서도 전체 JSONL을 다운로드한다. 화면에 정답을 펼치지 않는다."""
    _component(command=None, namespace=_NAMESPACE, backup={"text": text, "file_name": file_name, "label": label}, key=key, default=None)


def ready() -> bool:
    """복원 또는 저장 완료를 확인한 뒤에만 True. 실패할 때는 큐와 저장본을 보존한다."""
    ss = st.session_state
    store = _store()
    try:
        command = store.command(ss.get("rq_queue"))
    except (QueueFormatError, ValueError, TypeError):
        store.error = "invalid"
        command = None
    response = _component(command=command, namespace=_NAMESPACE, key="rq_storage_bridge", default=None)
    restored = store.accept(response)
    if restored is not None:
        legacy = ss.pop("rq_legacy_queue", None)
        if legacy is not None and legacy.to_jsonl() != restored.to_jsonl():
            # Upgrade an already-open session without dropping its old in-memory work.
            # An existing, different browser snapshot needs the same explicit conflict
            # resolution as another tab; do not choose either copy silently.
            store.command(legacy)
            ss["rq_required"] = legacy.required_labels
            if store.revision:
                store.error = "conflict"
            else:
                st.rerun()
        else:
            if "rq_queue" in ss:
                ss["rq_nonce"] = ss.get("rq_nonce", 0) + 1
            ss["rq_queue"] = restored
            ss["rq_required"] = restored.required_labels
            ss.pop("rq_export_result", None)

    st.caption("대기열은 현재 브라우저 프로필에 자동 저장합니다. 다른 기기와 공유되지 않습니다. "
               "공용 기기에서는 같은 프로필을 쓰는 사람도 볼 수 있습니다. 사이트 데이터를 지우거나 시크릿 창을 닫으면 저장본이 사라질 수 있습니다.")
    if store.error:
        messages = {
            "conflict": "다른 탭에서 먼저 저장했습니다. 이 화면의 변경은 저장하지 않았습니다. 현재 작업을 백업한 뒤 최신 저장본을 불러와 다시 작업해 주세요.",
            "quota": "브라우저의 저장 공간이 부족해 저장하지 못했습니다. 현재 작업은 이 화면에 남아 있습니다. 백업한 뒤 저장 공간을 확보하고 다시 시도해 주세요.",
            "denied": "브라우저가 저장을 허용하지 않아 대기열을 열거나 저장하지 못했습니다. 사이트의 저장 권한을 확인한 뒤 다시 시도해 주세요.",
            "invalid": "저장본을 읽지 못했습니다. 형식이나 라벨 기록이 올바르지 않아 자동으로 덮어쓰지 않았습니다. 복구용 백업을 보관한 뒤 저장본을 확인해 주세요.",
            "aborted": "브라우저 저장이 중단됐습니다. 저장 완료를 확인하지 못했으므로 현재 작업을 백업하거나 다시 시도해 주세요.",
            "unavailable": "브라우저 저장소에 연결하지 못했습니다. 저장 완료를 확인하지 못했으므로 현재 작업을 백업하거나 다시 시도해 주세요.",
        }
        st.error(messages[store.error])
        backup = store.unsaved_jsonl or store.recovery_jsonl
        if backup:
            # A real Blob download also works in stlite. Never show raw gold answers
            # in a storage error message, and never truncate a recovery backup.
            backup_download(backup, file_name="review_queue_recovery.jsonl", label="현재 작업 전체 백업 내려받기", key="rq_storage_backup")
        if store.error == "conflict":
            if st.button("최신 저장본 불러오기 (현재 변경 버림)", key="rq_storage_reload"):
                ss.pop("rq_msg", None)
                store.reload()
                st.rerun()
        elif st.button("자동 저장 다시 시도", key="rq_storage_retry"):
            store.retry()
            st.rerun()
        return False
    if not store.ready:
        st.info("대기열을 이 브라우저에 저장하는 중입니다. 완료 안내가 나올 때까지 이 화면을 유지해 주세요." if store.saving else "이 브라우저에 저장한 대기열을 불러오는 중입니다. 확인이 끝나면 작업할 수 있습니다.")
        if st.button("저장 상태 다시 확인", key="rq_storage_retry"):
            store.retry()
            st.rerun()
        return False
    if store.revision:
        st.caption("이 브라우저에 저장됨 · 새로고침하거나 서버가 다시 시작돼도 같은 브라우저에서 이어서 작업할 수 있습니다.")
    return True


def save_changes() -> None:
    """즉시 재실행하지 않는 설정 변경이나 만료 점유 회수도 자동 저장 경로에 넣는다."""
    if _store().command(st.session_state.get("rq_queue")) is not None:
        st.rerun()
