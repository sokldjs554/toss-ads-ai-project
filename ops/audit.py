"""변조를 알아채는 감사 기록 — 추가 전용 해시 체인.

무엇을 하나
- 항목마다 '바로 앞 항목의 해시' 를 함께 해시해 이어 붙인다(SHA-256, 키 정렬된 JSON). 중간 항목을 고치거나 지우거나 순서를 바꾸면
  그 지점부터 해시가 맞지 않아 `verify()` 가 처음 틀어진 위치를 알려 준다.
- 항목을 저장하기 '전에' 비밀(API 키)·개인정보(이메일·전화번호·주민등록번호 형태·카드번호)를 마스킹한다. 해시는 마스킹한 뒤의 내용에 대해 계산한다.
- 광고 작업의 실행·판정·수정·재검사·담당자 확인·라벨 제출을 같은 형식으로 남기는 어댑터(`audit_from_run` 등)를 둔다.

이 기록이 알아채지 못하는 것 (무결성의 한계)
- 해시 체인만으로는 '끝을 잘라 낸 것' 을 알 수 없다. 마지막 N개를 지운 기록은 그 자체로 멀쩡한 체인이다.
  기록을 보관할 때 마지막 해시(`head_hash`)와 개수를 따로(다른 곳에) 적어 두고 `verify(expected_last_hash=…, expected_count=…)` 로 대조해야 잡힌다.
  앞쪽을 잘라 낸 것도 마찬가지로, 기본 검증은 첫 항목이 순서 0·시작 해시여야 한다고 요구해서 잡고, 이어 붙인 구간은 앞 구간의 마지막 해시를 `anchor` 로 넘겨야 검증된다.
- 공격자가 모든 항목의 해시를 처음부터 다시 계산해 쓰면 체인은 멀쩡해 보인다. 이를 막으려면 (a) 마지막 해시를 따로 보관하거나 (b) `key` 를 주어
  HMAC-SHA256 으로 체인을 만들고 그 키를 기록과 따로 보관해야 한다. 키가 없는 쪽은 새 체인을 만들 수 없다. 이 프로젝트는 키 보관 장치를 갖고 있지 않다 — 호출하는 쪽의 몫이다.
- 변조 탐지이지 변조 방지가 아니다. 파일 자체를 못 쓰게 막지는 않는다. '누가' 기록했는지는 `actor` 문자열을 믿는 것이어서 인증이 아니다.

마스킹의 한계 (잡지 못한 것을 '가려졌다' 고 가정하지 않는다)
- 마스킹은 아래 패턴에 맞는 것만 가린다: `sk-…`·`sk-ant-…` 등 알려진 접두사의 키, 영문·숫자 32자 이상 토큰, 이메일, 한국 전화번호, 주민등록번호 형태 13자리, 루훈 검사를 통과하는 카드번호,
  이름이 비밀을 뜻하는 칸(api_key·password·token 등)의 값. 이름·주소·계좌번호·자유 서술 속 개인정보·전각 숫자나 띄어 쓴 번호 같은 변형 표기는 잡지 못한다.
- 그래서 1차 방어는 '애초에 넣지 않는 것' 이다. 어댑터(`audit_from_run` 등)는 광고 원문 대신 해시·길이·규칙 ID 만 남긴다. 마스킹은 실수로 섞여 들어온 것을 줄이는 2차 방어다.
- 패턴이 넓어서 과하게 가리는 쪽을 택했다(예: 날짜처럼 보이는 13자리 숫자는 주민등록번호 형태로 간주). 가려진 값은 되돌릴 수 없다.

크기·개수 정책 (하나를 골랐다: '가득 차면 명시적 오류')
- 링 버퍼처럼 오래된 항목을 조용히 밀어내지 않고, 압축하지도 않는다. 항목 수가 `max_entries` 에 닿으면 `AuditFull`, 한 항목이 `max_entry_bytes` 를 넘으면 `EntryTooLarge` 를 던진다.
  감사 기록은 '있어야 할 것이 없다' 가 가장 위험하므로, 못 남길 때는 호출한 쪽이 알게 한다(실패를 삼키지 않는다).
- 가득 찬 기록은 `export_jsonl()` 로 내보낸 뒤 `AuditLog.continue_from(old)` 로 마지막 해시를 이어받는 새 구간을 시작한다. 두 구간은 `anchor` 로 이어서 검증한다.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Iterable, Optional

SCHEMA_VERSION = 1
GENESIS_HASH = "0" * 64
DEFAULT_MAX_ENTRIES = 100_000
DEFAULT_MAX_ENTRY_BYTES = 16 * 1024
MAX_VERIFY_BYTES = 20 * 1024 * 1024     # 올려 받은 파일 검증의 상한 (브라우저·서버 메모리 보호)
_MAX_DEPTH = 12

EVENT_TYPES = (
    "run.executed",             # 광고 작업 한 번의 실행
    "ad.judged",                # 광고안 하나의 판정
    "ad.rewrite_checked",       # 자동 수정안의 재검사 결과
    "ad.edited",                # 사람이 문구를 고침
    "ad.rechecked",             # 고친 문구를 다시 검사함
    "human.review_requested",   # 담당자 확인으로 넘김
    "human.review_decided",     # 담당자가 확인을 마침
    "label.submitted",          # 라벨(정답 판정)을 제출함
)
_EVENT_RE = re.compile(r"^[a-z][a-z0-9_.]{0,63}$")


class AuditError(Exception):
    """감사 기록 오류의 뿌리."""


class AuditFull(AuditError):
    """항목 수 상한에 닿았다 — 오래된 항목을 지우지 않고 멈춘다."""


class EntryTooLarge(AuditError):
    """한 항목이 크기 상한을 넘었다 — 잘라 쓰지 않고 거부한다."""


class AuditFormatError(AuditError):
    """항목이나 파일의 형식이 틀렸다."""


class AuditTampered(AuditError):
    """가져온 기록이 검증에 실패했다."""

    def __init__(self, result: "VerifyResult"):
        super().__init__(result.reason)
        self.result = result


# ---------------------------------------------------------------------------
# 마스킹
# ---------------------------------------------------------------------------
_SECRET_KEY_NAME = re.compile(r"(?i)(?:^|[_\-.])(?:api[_\-]?key|secret|password|passwd|token|auth|authorization|credential|private[_\-]?key)$")
_HASH_KEY_NAME = re.compile(r"(?i)(?:hash|sha\d*|digest|fingerprint|checksum)$")

_SK_ANT = re.compile(r"sk-ant-[A-Za-z0-9_\-]{8,}")
_SK = re.compile(r"sk-(?:proj-|svcacct-|admin-)?[A-Za-z0-9_\-]{16,}")
_VENDOR = re.compile(r"(?<![A-Za-z0-9_])(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}|AIza[0-9A-Za-z_\-]{30,}|AKIA[0-9A-Z]{16}|xox[baprs]-[A-Za-z0-9\-]{10,})")
_BEARER = re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=\-]{12,}")
_EMAIL = re.compile(r"(?<![A-Za-z0-9._%+\-])[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)+")
_RRN = re.compile(r"(?<!\d)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])[-\s]?[1-8]\d{6}(?!\d)")
_CARD = re.compile(r"(?<!\d)(?:\d[ \-]?){12,18}\d(?!\d)")
_PHONE = re.compile(r"(?<![\d\-])(?:\+82[-.\s]?0?|0)(?:1[016789]|2|[3-6][1-5]|70)[-.\s]?\d{3,4}[-.\s]?\d{4}(?!\d)")
_REP_PHONE = re.compile(r"(?<![\d\-])1[5-9]\d{2}-\d{4}(?!\d)")
_LONG_TOKEN = re.compile(r"(?<![A-Za-z0-9_\-])[A-Za-z0-9_\-]{32,}(?![A-Za-z0-9_\-])")


def _luhn_ok(digits: str) -> bool:
    total, alt = 0, False
    for ch in reversed(digits):
        d = ord(ch) - 48
        if alt:
            d *= 2
            if d > 9:
                d -= 9
        total += d
        alt = not alt
    return total % 10 == 0


def _mask(kind: str, counts: dict[str, int]) -> str:
    counts[kind] = counts.get(kind, 0) + 1
    return f"[REDACTED:{kind}]"


def redact_text(text: str, counts: Optional[dict[str, int]] = None, *, allow_hash: bool = False) -> str:
    """문자열 하나에서 비밀·개인정보 패턴을 가린다. 같은 입력엔 같은 출력이고, 이미 가린 문자열에 다시 써도 달라지지 않는다.
    allow_hash=True 면 '해시 칸' 으로 알려진 값이라 32자 이상 토큰 규칙만 건너뛴다(키 패턴·이메일 등은 그대로 가린다)."""
    c = counts if counts is not None else {}
    out = _SK_ANT.sub(lambda m: _mask("api_key", c), text)
    out = _SK.sub(lambda m: _mask("api_key", c), out)
    out = _VENDOR.sub(lambda m: _mask("api_key", c), out)
    out = _BEARER.sub(lambda m: f"{m.group(1)} {_mask('api_key', c)}", out)
    out = _EMAIL.sub(lambda m: _mask("email", c), out)
    out = _RRN.sub(lambda m: _mask("rrn", c), out)
    out = _CARD.sub(lambda m: _mask("card", c) if (13 <= len(re.sub(r"\D", "", m.group(0))) <= 19 and _luhn_ok(re.sub(r"\D", "", m.group(0)))) else m.group(0), out)
    out = _PHONE.sub(lambda m: _mask("phone", c), out)
    out = _REP_PHONE.sub(lambda m: _mask("phone", c), out)
    if not allow_hash:
        out = _LONG_TOKEN.sub(lambda m: _mask("token", c) if (re.search(r"\d", m.group(0)) and re.search(r"[A-Za-z]", m.group(0))) else m.group(0), out)
    return out


def _jsonable(obj: Any, depth: int = 0) -> Any:
    """저장할 수 있는 JSON 값으로 바꾼다. 바이트 원문은 담지 않고(크기·해시만), 숫자가 아닌 실수(NaN·무한대)는 문자열로 둔다."""
    if depth > _MAX_DEPTH:
        raise AuditFormatError(f"payload 가 너무 깊게 중첩되었습니다 (>{_MAX_DEPTH})")
    if obj is None or isinstance(obj, (bool, str)):
        return obj
    if isinstance(obj, int):
        return obj
    if isinstance(obj, float):
        return obj if obj == obj and obj not in (float("inf"), float("-inf")) else str(obj)
    if isinstance(obj, Enum):
        return _jsonable(obj.value, depth + 1)
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, (bytes, bytearray)):
        return f"[bytes:{len(obj)} sha256={hashlib.sha256(bytes(obj)).hexdigest()[:16]}]"
    if isinstance(obj, dict):
        return {str(k): _jsonable(v, depth + 1) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(v, depth + 1) for v in obj]
    if isinstance(obj, (set, frozenset)):
        return [_jsonable(v, depth + 1) for v in sorted(obj, key=str)]
    if hasattr(obj, "model_dump"):
        return _jsonable(obj.model_dump(mode="json"), depth + 1)
    return str(obj)


def _redact_value(obj: Any, counts: dict[str, int], key: str = "") -> Any:
    if isinstance(obj, str):
        if key and _SECRET_KEY_NAME.search(key) and obj and not obj.startswith("[REDACTED:"):
            return _mask("secret", counts)
        return redact_text(obj, counts, allow_hash=bool(key and _HASH_KEY_NAME.search(key)))
    if isinstance(obj, dict):
        return {redact_text(str(k), counts): _redact_value(v, counts, str(k)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_redact_value(v, counts, key) for v in obj]
    return obj


def redact(obj: Any) -> Any:
    """값(딕셔너리·리스트·문자열) 안의 비밀·개인정보를 가린 복사본. 원본은 바꾸지 않는다."""
    return _redact_value(_jsonable(obj), {})


def redact_with_report(obj: Any) -> tuple[Any, dict[str, int]]:
    """redact 와 같고, 종류별로 몇 번 가렸는지도 돌려준다 (예: {'email': 2})."""
    counts: dict[str, int] = {}
    return _redact_value(_jsonable(obj), counts), counts


# ---------------------------------------------------------------------------
# 체인
# ---------------------------------------------------------------------------
def _canonical(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")


_ENTRY_FIELDS = ("v", "alg", "index", "ts", "type", "actor", "payload", "redacted", "prev", "hash")
_HASHED_FIELDS = ("v", "alg", "index", "ts", "type", "actor", "payload", "redacted", "prev")


def _digest(body: dict, key: Optional[bytes]) -> str:
    data = _canonical(body)
    if key is None:
        return hashlib.sha256(data).hexdigest()
    return hmac.new(key, data, hashlib.sha256).hexdigest()


@dataclass(frozen=True)
class AuditEntry:
    index: int
    ts: str
    event_type: str
    actor: str
    payload: dict
    redacted: int
    prev_hash: str
    hash: str
    alg: str = "sha256"
    v: int = SCHEMA_VERSION

    def body(self) -> dict:
        return {"v": self.v, "alg": self.alg, "index": self.index, "ts": self.ts, "type": self.event_type, "actor": self.actor,
                "payload": self.payload, "redacted": self.redacted, "prev": self.prev_hash}

    def to_dict(self) -> dict:
        return {**self.body(), "hash": self.hash}

    @classmethod
    def from_dict(cls, d: Any) -> "AuditEntry":
        """파일의 한 줄 → 항목. 필드가 모자라거나 모르는 필드가 있거나 형이 틀리면 AuditFormatError (해시가 지켜 주지 않는 필드가 숨어 있으면 안 된다)."""
        if not isinstance(d, dict):
            raise AuditFormatError("항목이 객체가 아닙니다")
        extra = sorted(set(d) - set(_ENTRY_FIELDS))
        if extra:
            raise AuditFormatError("알 수 없는 필드: " + ", ".join(str(x)[:40] for x in extra[:3]))
        missing = [k for k in _ENTRY_FIELDS if k not in d]
        if missing:
            raise AuditFormatError(f"빠진 필드: {', '.join(missing[:3])}")
        if not (isinstance(d["index"], int) and not isinstance(d["index"], bool) and d["index"] >= 0):
            raise AuditFormatError("index 가 0 이상의 정수가 아닙니다")
        if not (isinstance(d["redacted"], int) and not isinstance(d["redacted"], bool) and d["redacted"] >= 0):
            raise AuditFormatError("redacted 가 0 이상의 정수가 아닙니다")
        if d["v"] != SCHEMA_VERSION:
            raise AuditFormatError(f"지원하지 않는 형식 버전: {d['v']}")
        if d["alg"] not in ("sha256", "hmac-sha256"):
            raise AuditFormatError(f"알 수 없는 해시 방식: {d['alg']}")
        for k in ("ts", "type", "actor", "prev", "hash"):
            if not isinstance(d[k], str):
                raise AuditFormatError(f"{k} 가 문자열이 아닙니다")
        if not isinstance(d["payload"], dict):
            raise AuditFormatError("payload 가 객체가 아닙니다")
        return cls(index=d["index"], ts=d["ts"], event_type=d["type"], actor=d["actor"], payload=d["payload"], redacted=d["redacted"],
                   prev_hash=d["prev"], hash=d["hash"], alg=d["alg"], v=d["v"])


@dataclass(frozen=True)
class VerifyResult:
    ok: bool
    first_bad_index: Optional[int]    # 처음 틀어진 항목의 위치(0부터). 끝이 잘린 경우엔 '있어야 할 다음 위치'
    reason: str                       # 쉬운 한국어 설명
    code: str = "ok"                  # 기계가 읽는 분류: ok · format · index · chain · hash · key · truncated · anchor
    checked: int = 0                  # 검사한 항목 수
    head_hash: str = ""               # 검사한 마지막 항목의 해시 (따로 보관해 두면 끝 잘림을 잡을 수 있다)


class AuditLog:
    """추가 전용 감사 기록. 항목은 한 번 쓰면 고치는 API 가 없다(고친 것은 `verify()` 가 알아챈다)."""

    def __init__(self, *, key: Optional[bytes] = None, max_entries: int = DEFAULT_MAX_ENTRIES, max_entry_bytes: int = DEFAULT_MAX_ENTRY_BYTES,
                 clock: Optional[Callable[[], datetime]] = None, genesis_hash: str = GENESIS_HASH, start_index: int = 0) -> None:
        if max_entries < 1 or max_entry_bytes < 256:
            raise ValueError("max_entries ≥ 1, max_entry_bytes ≥ 256 이어야 합니다")
        self._key = key
        self._alg = "hmac-sha256" if key is not None else "sha256"
        self.max_entries = max_entries
        self.max_entry_bytes = max_entry_bytes
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._genesis = genesis_hash
        self._start = start_index
        self._entries: list[AuditEntry] = []
        self._lock = threading.Lock()

    # --- 읽기 ----------------------------------------------------------------
    @property
    def entries(self) -> tuple[AuditEntry, ...]:
        return tuple(self._entries)

    def __len__(self) -> int:
        return len(self._entries)

    @property
    def head_hash(self) -> str:
        return self._entries[-1].hash if self._entries else self._genesis

    @property
    def next_index(self) -> int:
        return self._start + len(self._entries)

    @property
    def remaining(self) -> int:
        return self.max_entries - len(self._entries)

    # --- 쓰기 ----------------------------------------------------------------
    def append(self, event_type: str, actor: str, payload: Optional[dict] = None, *, ts: Optional[str] = None) -> AuditEntry:
        """항목 하나를 덧붙인다. 비밀·개인정보는 해시를 계산하기 전에 가린다. 상한에 닿으면 AuditFull, 항목이 크면 EntryTooLarge."""
        if not isinstance(event_type, str) or not _EVENT_RE.match(event_type):
            raise AuditFormatError(f"event_type 형식이 맞지 않습니다: {event_type!r}")
        if not isinstance(actor, str) or not actor.strip():
            raise AuditFormatError("actor 가 비어 있습니다")
        if ts is not None and not isinstance(ts, str):
            raise AuditFormatError("ts 는 문자열이어야 합니다")
        counts: dict[str, int] = {}
        safe_payload = _redact_value(_jsonable(payload or {}), counts)
        safe_actor = redact_text(actor.strip(), counts)[:200]
        stamp = ts or self._clock().astimezone(timezone.utc).isoformat()
        with self._lock:
            if len(self._entries) >= self.max_entries:
                raise AuditFull(f"감사 기록이 가득 찼습니다 ({self.max_entries}개). 내보낸 뒤 AuditLog.continue_from() 으로 이어서 시작하세요 — 오래된 항목을 지우지 않습니다.")
            body = {"v": SCHEMA_VERSION, "alg": self._alg, "index": self.next_index, "ts": stamp, "type": event_type, "actor": safe_actor,
                    "payload": safe_payload, "redacted": sum(counts.values()), "prev": self.head_hash}
            digest = _digest(body, self._key)
            entry = AuditEntry(index=body["index"], ts=stamp, event_type=event_type, actor=safe_actor, payload=safe_payload, redacted=body["redacted"],
                               prev_hash=body["prev"], hash=digest, alg=self._alg)
            size = len(_canonical(entry.to_dict()))
            if size > self.max_entry_bytes:
                raise EntryTooLarge(f"항목이 너무 큽니다 ({size}바이트 > {self.max_entry_bytes}). 원문 대신 해시·길이·ID 만 남기세요.")
            self._entries.append(entry)
            return entry

    @classmethod
    def continue_from(cls, old: "AuditLog", **kwargs: Any) -> "AuditLog":
        """가득 찬(또는 내보낸) 기록의 마지막 해시를 이어받는 새 구간. 순서 번호도 이어진다."""
        kwargs.setdefault("key", old._key)
        kwargs.setdefault("max_entries", old.max_entries)
        kwargs.setdefault("max_entry_bytes", old.max_entry_bytes)
        return cls(genesis_hash=old.head_hash, start_index=old.next_index, **kwargs)

    # --- 검증 ----------------------------------------------------------------
    def verify(self, *, expected_last_hash: Optional[str] = None, expected_count: Optional[int] = None) -> VerifyResult:
        """체인을 처음부터 다시 계산해 맞는지 본다. 끝 잘림은 expected_last_hash / expected_count 를 따로 보관해 넘긴 경우에만 알 수 있다 (모듈 설명 참조)."""
        return _verify_entries(list(self._entries), key=self._key, genesis=self._genesis, start=self._start,
                               expected_last_hash=expected_last_hash, expected_count=expected_count)

    # --- 내보내기·가져오기 ---------------------------------------------------------
    def export_jsonl(self) -> str:
        return "".join(json.dumps(e.to_dict(), ensure_ascii=False, sort_keys=True) + "\n" for e in self._entries)

    @classmethod
    def import_jsonl(cls, text: str, *, key: Optional[bytes] = None, anchor: Optional[tuple[int, str]] = None, verify: bool = True,
                     max_entries: int = DEFAULT_MAX_ENTRIES, max_entry_bytes: int = DEFAULT_MAX_ENTRY_BYTES) -> "AuditLog":
        """JSONL → 기록. 형식이 틀리면 AuditFormatError, verify=True 이고 체인이 맞지 않으면 AuditTampered. anchor=(시작 순서, 앞 구간의 마지막 해시) 로 이어 붙인 구간을 가져온다."""
        entries, err = _parse_jsonl(text)
        if err is not None:
            raise AuditFormatError(err[1])
        start, genesis = anchor if anchor is not None else (0, GENESIS_HASH)
        if len(entries) > max_entries:
            raise AuditFull(f"가져온 항목이 상한({max_entries})을 넘습니다")
        log = cls(key=key, max_entries=max_entries, max_entry_bytes=max_entry_bytes, genesis_hash=genesis, start_index=start)
        log._entries = entries
        if verify:
            res = log.verify()
            if not res.ok:
                raise AuditTampered(res)
        return log

    @staticmethod
    def verify_jsonl(text: str, *, key: Optional[bytes] = None, anchor: Optional[tuple[int, str]] = None, expected_last_hash: Optional[str] = None,
                     expected_count: Optional[int] = None) -> VerifyResult:
        """올려 받은 파일을 검증한다. 어떤 입력이어도 예외를 던지지 않고 VerifyResult 로 답한다."""
        if not isinstance(text, str):
            return VerifyResult(False, 0, "파일을 글자로 읽을 수 없습니다.", "format")
        if len(text.encode("utf-8", "ignore")) > MAX_VERIFY_BYTES:
            return VerifyResult(False, None, f"파일이 너무 큽니다 (상한 {MAX_VERIFY_BYTES // (1024 * 1024)}MB).", "format")
        entries, err = _parse_jsonl(text)
        if err is not None:
            return VerifyResult(False, err[0], err[1], "format", checked=len(entries))
        start, genesis = anchor if anchor is not None else (0, GENESIS_HASH)
        return _verify_entries(entries, key=key, genesis=genesis, start=start, expected_last_hash=expected_last_hash, expected_count=expected_count)


def _parse_jsonl(text: str) -> tuple[list[AuditEntry], Optional[tuple[int, str]]]:
    """(읽은 항목들, 오류 (위치, 설명) 또는 None). 빈 줄은 건너뛴다."""
    out: list[AuditEntry] = []
    for line_no, line in enumerate(text.split("\n")):         # splitlines() 는 U+2028·U+0085 같은 문자에서도 줄을 나눠 내용에 그런 글자가 든 항목을 깨뜨린다 — 줄바꿈은 \n 만 쓴다
        line = line.rstrip("\r")
        if not line.strip():
            continue
        pos = len(out)
        try:
            out.append(AuditEntry.from_dict(json.loads(line)))
        except (ValueError, RecursionError, AuditFormatError) as e:
            what = str(e) if isinstance(e, AuditFormatError) else "JSON 으로 읽을 수 없습니다"
            return out, (pos, f"{line_no + 1}번째 줄(항목 {pos}): {what}")
    return out, None


def _verify_entries(entries: list[AuditEntry], *, key: Optional[bytes], genesis: str, start: int,
                    expected_last_hash: Optional[str], expected_count: Optional[int]) -> VerifyResult:
    prev = genesis
    for pos, e in enumerate(entries):
        want_alg = "hmac-sha256" if key is not None else "sha256"
        if e.alg != want_alg:
            if e.alg == "hmac-sha256":
                return VerifyResult(False, pos, "이 기록은 키로 만든 체인(HMAC)입니다. 키를 함께 넣어야 검증할 수 있습니다.", "key", checked=pos, head_hash=prev)
            return VerifyResult(False, pos, f"항목 {pos}: 해시 방식이 기대와 다릅니다 ({e.alg}).", "hash", checked=pos, head_hash=prev)
        if e.index != start + pos:
            return VerifyResult(False, pos, f"항목 {pos}: 순서 번호가 {start + pos} 이어야 하는데 {e.index} 입니다 (항목이 지워졌거나 순서가 바뀌었습니다).", "index", checked=pos, head_hash=prev)
        if not hmac.compare_digest(e.prev_hash.encode(), prev.encode()):
            return VerifyResult(False, pos, f"항목 {pos}: 앞 항목과의 연결이 끊어졌습니다 (앞 항목이 바뀌었거나 지워졌습니다).", "chain", checked=pos, head_hash=prev)
        try:
            want = _digest(e.body(), key)
        except (ValueError, TypeError):
            return VerifyResult(False, pos, f"항목 {pos}: 내용을 해시할 수 없습니다.", "format", checked=pos, head_hash=prev)
        if not hmac.compare_digest(want.encode(), e.hash.encode()):
            return VerifyResult(False, pos, f"항목 {pos}: 내용이 바뀌었습니다 (해시가 맞지 않습니다)." + (" 키가 틀렸을 수도 있습니다." if key is not None else ""),
                                "hash", checked=pos, head_hash=prev)
        prev = e.hash
    n = len(entries)
    if expected_count is not None and n != expected_count:
        return VerifyResult(False, min(n, expected_count), f"항목 수가 따로 보관한 값({expected_count})과 다릅니다 (지금 {n}개). 끝이 잘렸거나 덧붙었습니다.", "truncated", checked=n, head_hash=prev)
    if expected_last_hash is not None and not hmac.compare_digest(prev.encode(), expected_last_hash.strip().lower().encode()):
        return VerifyResult(False, n, "마지막 해시가 따로 보관한 값과 다릅니다 (끝이 잘렸거나 마지막 항목이 바뀌었습니다).", "truncated", checked=n, head_hash=prev)
    return VerifyResult(True, None, f"항목 {n}개의 체인이 맞습니다." + ("" if (expected_last_hash or expected_count is not None) else " (끝 잘림은 따로 보관한 마지막 해시가 있어야 알 수 있습니다.)"),
                        "ok", checked=n, head_hash=prev)


# ---------------------------------------------------------------------------
# 광고 작업 어댑터 — 원문 대신 해시·길이·ID 만 남긴다 (데이터 최소화)
# ---------------------------------------------------------------------------
def text_digest(text: str) -> str:
    """문구의 SHA-256 앞 16자. 같은 문구인지 대조할 수는 있지만 비밀은 아니다(짧은 문구는 대입으로 되짚을 수 있다)."""
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:16]


def _report_payload(rep: Any) -> dict:
    v = rep.verdict
    return {
        "variant_id": rep.variant_id, "final_decision": rep.final_decision.value, "model_decision": v.decision.value, "confidence": v.confidence,
        "rule_ids": sorted({f.rule_id for f in v.findings if f.verified_quote}), "unverified_quotes": sum(1 for f in v.findings if not f.verified_quote),
        "provider": v.provider, "model": v.model, "disagreement": v.disagreement, "second_opinion_failed": v.second_opinion_failed,
        "rewrite_status": rep.rewrite_status.value, "ad_sha256": text_digest(rep.ad_copy.full_text()), "ad_chars": len(rep.ad_copy.full_text()),
    }


def audit_from_run(run: Any, log: Optional[AuditLog] = None, *, actor: str = "system") -> AuditLog:
    """PipelineRun 하나를 감사 기록에 남긴다: 실행 1건 + 광고안마다 판정 1건(+ 자동 수정안 재검사 결과, 담당자 확인 요청)."""
    log = log if log is not None else AuditLog()
    log.append("run.executed", actor, {
        "run_id": run.run_id, "trace_id": run.trace_id, "mode": run.mode, "brief_id": run.brief.brief_id, "category": run.brief.product.category.value,
        "categories": list(run.categories), "n_reports": len(run.reports), "halted": run.halted, "halt_reason": (run.halt_reason or "")[:300],
        "intercept": {"triggered": run.intercept.triggered, "kind": run.intercept.kind.value if run.intercept.kind else None, "hard_block": run.intercept.hard_block},
        "failure_codes": sorted({f.code for f in run.failures}), "total_cost_usd": run.total_cost_usd, "total_latency_ms": run.total_latency_ms,
        "demo_mode": run.mode == "demo",
    })
    for rep in run.reports:
        log.append("ad.judged", actor, {"run_id": run.run_id, **_report_payload(rep)})
        if rep.rewrite_status.value != "none":
            log.append("ad.rewrite_checked", actor, {"run_id": run.run_id, "variant_id": rep.variant_id, "rewrite_status": rep.rewrite_status.value,
                                                      "recheck_decision": rep.rewrite_recheck_decision.value if rep.rewrite_recheck_decision else None,
                                                      "recheck_rule_ids": sorted({f.rule_id for f in rep.rewrite_recheck_findings}),
                                                      "rewrite_sha256": text_digest(rep.verified_rewrite.full_text()) if rep.verified_rewrite else None})
        if rep.final_decision.value == "HUMAN_REVIEW":
            log.append("human.review_requested", actor, {"run_id": run.run_id, "variant_id": rep.variant_id, "reason": (rep.decision_reason or "")[:300]})
    return log


def audit_edit(log: AuditLog, *, variant_id: str, actor: str, before: Any, after: Any) -> AuditEntry:
    """사람이 문구를 고쳤다. 고치기 전·후 원문은 남기지 않고 해시와 길이만 남긴다."""
    b, a = before.full_text(), after.full_text()
    return log.append("ad.edited", actor, {"variant_id": variant_id, "before_sha256": text_digest(b), "after_sha256": text_digest(a),
                                           "before_chars": len(b), "after_chars": len(a), "changed": b != a})


def audit_recheck(log: AuditLog, *, variant_id: str, actor: str, report: Any, run_id: str = "") -> AuditEntry:
    """고친 문구를 다시 검사한 결과."""
    return log.append("ad.rechecked", actor, {"run_id": run_id, **_report_payload(report)})


def audit_human_review(log: AuditLog, *, variant_id: str, reviewer: str, decision: str, note: str = "", run_id: str = "") -> AuditEntry:
    """담당자가 확인을 마쳤다. 메모는 마스킹을 거쳐 300자까지만 남긴다."""
    return log.append("human.review_decided", reviewer, {"run_id": run_id, "variant_id": variant_id, "decision": decision, "note": (note or "")[:300]})


def audit_label(log: AuditLog, *, case_id: str, labeler: str, label: str, rule_ids: Iterable[str] = (), note: str = "") -> AuditEntry:
    """라벨(정답 판정) 제출."""
    return log.append("label.submitted", labeler, {"case_id": case_id, "label": label, "rule_ids": sorted(set(rule_ids)), "note": (note or "")[:300]})
