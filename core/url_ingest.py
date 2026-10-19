"""상품 URL 이해 — 페이지 가져오기 · 읽기 · 사실 초안.

외부 페이지는 **신뢰할 수 없는 입력**이다. 이 모듈의 가치는 두 가지다.
1) 보안: 서버가 사설망·메타데이터 주소로 끌려가지 않게(SSRF), 화면에 보이지 않는 글에 숨은 지시문이 광고 사실로 새지 않게.
2) 정직: 페이지에서 읽은 값은 '후보'일 뿐이다. 사용자가 직접 골라 `confirm()` 을 거친 것만 `Fact` 가 된다.

흐름: `fetch_page`(가져오기, 서버 환경만) → `parse_product_page`(읽기) → `to_draft`(사실 초안) → `confirm`(사용자가 고른 것만 ProductFacts).
한 번에 하려면 `ingest_url` / `ingest_html`.

이 모듈이 하지 않는 것 (한계)
- 자바스크립트를 실행하지 않는다. 브라우저에서 그려지는 SPA 의 본문은 읽지 못하고 '읽지 못함' 으로 돌려준다.
- 외부 스타일시트는 가져오지 않는다. 같은 페이지 안의 <style> 과 style 속성만 본다. 외부 CSS 로 숨긴 글은 보이는 글로 읽힌다.
- 인젝션 탐지는 패턴 기반이다 (guardrails.injection + 이 파일의 페이지용 보강 패턴). 바꿔 말한 지시문은 놓칠 수 있다.
  그래서 방어는 탐지에만 기대지 않는다: 화면에 보이지 않는 글은 탐지 여부와 상관없이 사실이 되지 않고, 보이는 글도 사용자가 고르기 전에는 사실이 아니다.
- 가격은 '숫자+원' 꼴만 읽는다. 마이크로데이터(itemprop)·이미지 속 가격·'2만 9천 9백원' 같은 꼴은 읽지 않는다.
- 이름 확인(DNS) 시간은 총 8초 제한에 들어가지 않을 수 있다 (시스템 이름 확인은 중간에 끊을 수 없다). 끝난 뒤 제한을 넘겼으면 실패로 돌려준다.
"""
from __future__ import annotations

import io
import ipaddress
import json
import re
import unicodedata
import zlib
from enum import Enum
from html import escape as _escape
from html.parser import HTMLParser
from pathlib import Path
from typing import Callable, Iterable, Literal, Optional, Protocol
from urllib.parse import quote, urljoin

from pydantic import BaseModel, ConfigDict, Field

from core import config
from core.schemas import Fact, ProductCategory, ProductFacts
from guardrails import injection
from guardrails.normalize import is_invisible, normalize

# ---------------------------------------------------------------------------
# 상수 — 모두 이 프로젝트가 정한 값이다 (실제 서비스의 기준이 아님)
# ---------------------------------------------------------------------------
MAX_URL_LEN = 2048
ALLOWED_PORTS = (80, 443)
MAX_REDIRECTS = 3
TOTAL_TIMEOUT_S = 8.0
MAX_BODY_BYTES = int(1.5 * 1024 * 1024)
READ_CHUNK = 16 * 1024
USER_AGENT = "PreflightBot/1.0 (personal project; reads one product page; no cookies)"
ALLOWED_CONTENT_TYPES = ("text/html", "application/xhtml+xml")
SAMPLE_PREFIX = "sample://"

TITLE_MAX = 120          # 상품명·제목
FACT_MAX = 160           # 사실 문장 하나
EVIDENCE_MAX = 200       # 근거 발췌
LANDING_MAX = 2000       # 랜딩 문구
HIDDEN_SNIPPET_MAX = 300
MAX_FACTS = 30
MAX_BODY_FACTS = 14
MAX_IMAGES = 12
MAX_IMAGE_URL = 1000
MAX_VISIBLE_CHARS = 30_000   # 이 길이까지만 모아 검사한다
MAX_HIDDEN_CHARS = 20_000
MAX_HIDDEN_SNIPPETS = 50
MAX_TAGS = 60_000
MAX_DEPTH = 400            # 브라우저도 구문 분석 깊이에 상한을 둔다. 이보다 깊게 중첩된 문서는 거기까지만 읽는다
MAX_JSONLD_BLOCKS = 20
MAX_JSONLD_BYTES = 200_000
MAX_STYLE_BYTES = 100_000
MIN_HIDDEN_CHARS_FOR_FLAG = 10   # 이보다 짧은 숨은 글(예: '<!-- 헤더 -->')은 알림 대상이 아니다
_JS_NOTICE = re.compile(r"javascript|자바스크립트|\bjs\b|enable\s+scripts?|쿠키를?\s*(사용|허용)", re.IGNORECASE)
SPA_MAX_CHARS = 100              # 보이는 글이 이보다 적고 구조화 정보도 없으면 껍데기로 본다
LOW_CONTENT_CHARS = 150

# 연결을 허용하지 않는 호스트 이름 (이름 확인 전 단계)
_BLOCKED_HOSTS = frozenset({"localhost", "ip6-localhost", "ip6-loopback", "metadata", "metadata.google.internal", "metadata.goog",
                            "instance-data", "kubernetes", "kubernetes.default", "host.docker.internal", "gateway.docker.internal"})
_BLOCKED_SUFFIXES = (".local", ".localhost", ".internal", ".intranet", ".lan", ".home", ".corp", ".private", ".localdomain",
                     ".home.arpa", ".svc", ".cluster.local", ".consul")

_V4_BLOCKED = [ipaddress.ip_network(n) for n in (
    "0.0.0.0/8", "10.0.0.0/8", "100.64.0.0/10", "127.0.0.0/8", "169.254.0.0/16", "172.16.0.0/12", "192.0.0.0/24", "192.0.2.0/24",
    "192.88.99.0/24", "192.168.0.0/16", "198.18.0.0/15", "198.51.100.0/24", "203.0.113.0/24", "224.0.0.0/4", "240.0.0.0/4")]
_V6_GLOBAL = ipaddress.ip_network("2000::/3")
_V6_BLOCKED = [ipaddress.ip_network(n) for n in ("2001::/23", "2001:db8::/32", "3fff::/20")]   # 전역 대역 안의 예약 구간(Teredo·문서용 등)


class FetchError(Exception):
    """가져오기 실패. `code` 는 기계가 보는 값, `user_message` 는 화면에 보일 쉬운 말."""

    def __init__(self, code: str, user_message: str):
        super().__init__(f"{code}: {user_message}")
        self.code = code
        self.user_message = user_message


class UrlRejected(FetchError):
    """주소 자체가 안전하지 않거나 형식이 맞지 않아 연결을 시도하지 않은 경우."""


class DraftError(ValueError):
    """초안을 사실로 확정할 수 없는 경우 (선택이 없음·알 수 없는 사실·상태가 읽기 가능이 아님 …)."""


# ---------------------------------------------------------------------------
# 1) 주소 검사 (SSRF)
# ---------------------------------------------------------------------------
class ValidatedUrl(BaseModel):
    model_config = ConfigDict(frozen=True)

    url: str                     # 정규화된 주소 (조각 제거)
    scheme: str
    host: str                    # 소문자 ASCII. IDN 은 punycode. IPv6 는 대괄호 없이
    port: int
    target: str                  # 요청 줄의 경로+질의 (ASCII, 퍼센트 인코딩)
    ip_literal: Optional[str] = None

    @property
    def host_header(self) -> str:
        h = f"[{self.host}]" if ":" in self.host else self.host
        default = 443 if self.scheme == "https" else 80
        return h if self.port == default else f"{h}:{self.port}"


_CTRL_OR_SPACE = re.compile(r"[\s\x00-\x1f\x7f-\x9f]")
_DOT_VARIANTS = str.maketrans({"\u3002": ".", "\uff61": ".", "\uff0e": "."})
_NUMERIC_LABEL = re.compile(r"^(?:0x[0-9a-f]*|[0-9]+)$", re.IGNORECASE)
_HOST_OK = re.compile(r"^[a-z0-9._-]+$")


def _ip_block_reason(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> Optional[str]:
    """연결하면 안 되는 주소면 이유 코드, 아니면 None. 파이썬 버전마다 달라지는 is_global 대신 대역을 직접 나열한다."""
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            return _ip_block_reason(ip.ipv4_mapped)
        packed = ip.packed
        if packed[:12] == bytes.fromhex("0064ff9b0000000000000000"):     # NAT64 64:ff9b::/96 — 안에 든 IPv4 를 본다
            return _ip_block_reason(ipaddress.IPv4Address(packed[12:]))
        if packed[:2] == b"\x20\x02":                                      # 6to4 2002::/16 — 안에 든 IPv4 를 본다
            return _ip_block_reason(ipaddress.IPv4Address(packed[2:6]))
        if ip not in _V6_GLOBAL:
            return "non_global_v6"        # ::1, ::, ::/96(IPv4 호환), fc00::/7, fe80::/10, ff00::/8 … 전역 유니캐스트가 아닌 전부
        if any(ip in n for n in _V6_BLOCKED):
            return "reserved_v6"
        return None
    if str(ip) == "169.254.169.254":
        return "metadata"
    for net in _V4_BLOCKED:
        if ip in net:
            return {"100.64.0.0/10": "cgnat", "127.0.0.0/8": "loopback", "169.254.0.0/16": "linklocal", "224.0.0.0/4": "multicast"}.get(str(net), "private_or_reserved")
    return None


def _parse_ip_number(part: str) -> Optional[int]:
    if part == "":
        return None
    low = part.lower()
    if low.startswith("0x"):
        digits = low[2:]
        if digits == "":
            return 0
        return int(digits, 16) if re.fullmatch(r"[0-9a-f]+", digits) else None
    if len(part) > 1 and part[0] == "0":
        return int(part, 8) if re.fullmatch(r"[0-7]+", part) else None
    return int(part, 10) if re.fullmatch(r"[0-9]+", part) else None


def parse_ipv4_loose(host: str) -> ipaddress.IPv4Address:
    """inet_aton 식 표기(10진 정수·16진·8진·점 사이 일부 생략)를 IPv4 로 푼다. 풀 수 없으면 ValueError.
    브라우저와 C 라이브러리는 `2130706433`·`0x7f.0.0.1`·`017700000001`·`127.1` 을 모두 127.0.0.1 로 읽는다 — 이 우회를 막으려면 같은 방식으로 읽어야 한다."""
    parts = host.split(".")
    if not 1 <= len(parts) <= 4:
        raise ValueError("IPv4 표기가 아님")
    nums: list[int] = []
    for p in parts:
        n = _parse_ip_number(p)
        if n is None:
            raise ValueError("IPv4 표기가 아님")
        nums.append(n)
    if any(n > 255 for n in nums[:-1]) or nums[-1] >= 256 ** (5 - len(nums)):
        raise ValueError("IPv4 범위를 벗어남")
    value = nums[-1]
    for i, n in enumerate(nums[:-1]):
        value += n << (8 * (3 - i))
    return ipaddress.IPv4Address(value)


def _reject_private(_where: str = "") -> UrlRejected:
    return UrlRejected("private_ip", "회사 안쪽(내부망)이나 개인용 주소로는 연결하지 않습니다. 공개된 상품 페이지 주소를 넣어 주세요.")


def validate_url(url: str) -> ValidatedUrl:
    """주소를 검사하고 정규화한다. 이름 확인(DNS)은 하지 않는다 — 호스트가 IP 리터럴이면 여기서 대역까지 판정한다.
    거부: http/https 외 · 2,048자 초과 · 공백·제어문자 · 백슬래시 · `user:pass@` · 허용 포트(80/443) 밖 · 사설·루프백·링크로컬·멀티캐스트·예약·CGNAT 주소
    (10진·16진·8진·생략형 IPv4, IPv6, IPv4-mapped IPv6, 대괄호 포함) · localhost · *.local/*.internal 등 · 메타데이터 호스트 · 점이 없는 호스트 이름."""
    if not isinstance(url, str) or not url:
        raise UrlRejected("empty", "주소를 입력해 주세요.")
    if len(url) > MAX_URL_LEN:
        raise UrlRejected("too_long", "주소가 너무 깁니다 (2,048자 이하).")
    if _CTRL_OR_SPACE.search(url):
        raise UrlRejected("control", "주소에 공백이나 보이지 않는 문자가 들어 있습니다.")
    if "\\" in url:
        raise UrlRejected("backslash", "주소 형식이 올바르지 않습니다.")
    m = re.match(r"^([A-Za-z][A-Za-z0-9+.\-]*):", url)
    scheme = m.group(1).lower() if m else ""
    if scheme not in ("http", "https"):
        raise UrlRejected("scheme", "http 또는 https 로 시작하는 주소만 쓸 수 있습니다.")
    rest = url[len(scheme) + 1:]
    if not rest.startswith("//"):
        raise UrlRejected("format", "주소 형식이 올바르지 않습니다.")
    rest = rest[2:]
    end = next((i for i, ch in enumerate(rest) if ch in "/?#"), len(rest))
    authority, tail = rest[:end], rest[end:]
    if "@" in authority:
        raise UrlRejected("userinfo", "주소에 아이디·비밀번호(user:pass@)를 넣을 수 없습니다.")
    if not authority:
        raise UrlRejected("format", "주소에 사이트 이름이 없습니다.")

    # 호스트·포트 분리
    if authority.startswith("["):
        close = authority.find("]")
        if close < 0:
            raise UrlRejected("format", "주소 형식이 올바르지 않습니다.")
        raw_host, port_part = authority[: close + 1], authority[close + 1:]
        if port_part and not port_part.startswith(":"):
            raise UrlRejected("format", "주소 형식이 올바르지 않습니다.")
        port_s = port_part[1:]
    else:
        if authority.count(":") > 1:
            raise UrlRejected("format", "주소 형식이 올바르지 않습니다.")
        raw_host, _, port_s = authority.partition(":")
    if port_s == "":
        port = 443 if scheme == "https" else 80
    else:
        if not re.fullmatch(r"[0-9]{1,5}", port_s):
            raise UrlRejected("port", "연결할 수 없는 포트입니다 (80, 443 만 가능).")
        port = int(port_s)
        if port not in ALLOWED_PORTS:
            raise UrlRejected("port", "연결할 수 없는 포트입니다 (80, 443 만 가능).")

    ip_literal: Optional[str] = None
    host = unicodedata.normalize("NFKC", raw_host).translate(_DOT_VARIANTS).lower()
    if host.startswith("["):
        if not host.endswith("]"):
            raise UrlRejected("format", "주소 형식이 올바르지 않습니다.")
        inner = host[1:-1]
        if "%" in inner:
            raise UrlRejected("format", "주소 형식이 올바르지 않습니다.")
        try:
            ip6 = ipaddress.IPv6Address(inner)
        except ValueError:
            raise UrlRejected("format", "주소 형식이 올바르지 않습니다.") from None
        if _ip_block_reason(ip6):
            raise _reject_private("v6")
        host, ip_literal = str(ip6), str(ip6)
    else:
        if host.endswith("."):
            host = host[:-1]
        if not host:
            raise UrlRejected("format", "주소에 사이트 이름이 없습니다.")
        if not host.isascii():
            try:
                host = host.encode("idna").decode("ascii")
            except UnicodeError:
                raise UrlRejected("format", "사이트 이름을 해석할 수 없습니다.") from None
        if not _HOST_OK.match(host) or len(host) > 253:
            raise UrlRejected("format", "사이트 이름에 쓸 수 없는 문자가 있습니다.")
        labels = host.split(".")
        if any(not lab or len(lab) > 63 for lab in labels):
            raise UrlRejected("format", "사이트 이름 형식이 올바르지 않습니다.")
        if _NUMERIC_LABEL.match(labels[-1]):          # 마지막 칸이 숫자면 IP 표기다 — 풀어서 판정, 못 풀면 거부
            try:
                ip4 = parse_ipv4_loose(host)
            except ValueError:
                raise UrlRejected("format", "숫자로 된 주소를 해석할 수 없습니다.") from None
            if _ip_block_reason(ip4):
                raise _reject_private("v4")
            host, ip_literal = str(ip4), str(ip4)
        else:
            if host in _BLOCKED_HOSTS or host.endswith(_BLOCKED_SUFFIXES):
                raise UrlRejected("blocked_host", "내부용 이름(localhost·.local·.internal 등)으로는 연결하지 않습니다.")
            if "." not in host:
                raise UrlRejected("blocked_host", "점이 없는 내부용 이름으로는 연결하지 않습니다. 전체 주소(예: shop.example.com)를 넣어 주세요.")

    path_query = tail.split("#", 1)[0]
    if not path_query.startswith("/"):
        path_query = "/" + path_query
    target = quote(path_query, safe="/?&=%:@!$'()*+,;~-._[]")
    host_part = f"[{host}]" if ":" in host else host
    default = 443 if scheme == "https" else 80
    shown_url = f"{scheme}://{host_part}{'' if port == default else ':' + str(port)}{target}"
    return ValidatedUrl(url=shown_url, scheme=scheme, host=host, port=port, target=target, ip_literal=ip_literal)


def check_resolved_addresses(addresses: Iterable[str]) -> list[str]:
    """이름 확인 결과(모든 A/AAAA)를 검사한다. **하나라도** 막힌 대역이면 전체를 거부한다 — 일부만 사설인 답은 리바인딩 시도일 수 있다."""
    out: list[str] = []
    for a in addresses:
        try:
            ip = ipaddress.ip_address(str(a).strip())
        except ValueError:
            raise UrlRejected("private_ip", "이름 확인 결과를 해석할 수 없어 연결하지 않습니다.") from None
        if _ip_block_reason(ip):
            raise _reject_private("resolved")
        if str(ip) not in out:
            out.append(str(ip))
    if not out:
        raise FetchError("dns", "사이트 주소를 찾을 수 없습니다.")
    return out


# ---------------------------------------------------------------------------
# 2) 가져오기
# ---------------------------------------------------------------------------
class FetchResult(BaseModel):
    url: str                              # 마지막으로 받은 주소 (리다이렉트 뒤)
    status: int = 200
    content_type: str = "text/html"
    charset: Optional[str] = None
    body: bytes = b""
    truncated: bool = False               # 본문 상한(1.5MB)에서 잘렸다
    redirects: list[str] = Field(default_factory=list)
    remote_ip: Optional[str] = None


class Fetcher(Protocol):
    """주소를 받아 본문을 돌려주는 것. 테스트에서는 가짜를 넣는다."""

    def fetch(self, url: str) -> FetchResult: ...


Resolver = Callable[[str, int], list]
Connector = Callable[..., object]


def _default_resolver(host: str, port: int) -> list:
    import socket   # 지연 import — 브라우저 실행판에서는 쓰지 않는다

    out: list[str] = []
    for _fam, _t, _p, _c, sockaddr in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM):
        if sockaddr[0] not in out:
            out.append(sockaddr[0])
    return out


def _default_connector(ip: str, port: int, *, tls: bool, server_hostname: str, timeout: float):
    """확인된 IP 하나에 직접 연결한다. TLS 의 SNI·인증서 이름 검사는 원래 호스트 이름으로 한다."""
    import socket

    sock = socket.create_connection((ip, port), timeout=timeout)
    if not tls:
        return sock
    import ssl

    try:
        return ssl.create_default_context().wrap_socket(sock, server_hostname=server_hostname)
    except Exception:
        sock.close()
        raise


class _DeadlineReader(io.RawIOBase):
    """소켓에서 읽을 때마다 남은 시간을 다시 계산한다. 느리게 조금씩 보내는 서버가 총 시간 제한을 늘리지 못하게 한다."""

    def __init__(self, sock, deadline: float, clock: Callable[[], float]):
        super().__init__()
        self._sock, self._deadline, self._clock = sock, deadline, clock

    def readable(self) -> bool:
        return True

    def readinto(self, b) -> int:
        remaining = self._deadline - self._clock()
        if remaining <= 0:
            raise FetchError("timeout", "페이지를 가져오는 데 8초가 넘게 걸려 멈췄습니다.")
        try:
            self._sock.settimeout(remaining)
        except OSError:
            pass
        try:
            data = self._sock.recv(len(b))
        except TimeoutError:
            raise FetchError("timeout", "페이지를 가져오는 데 8초가 넘게 걸려 멈췄습니다.") from None
        n = len(data)
        b[:n] = data
        return n


class _SockShim:
    """http.client.HTTPResponse 가 소켓에서 필요로 하는 것은 makefile 하나뿐이다."""

    def __init__(self, reader):
        self._reader = reader

    def makefile(self, *_a, **_k):
        return self._reader


class _Inflater:
    """gzip/deflate 를 출력 상한을 지키며 푼다 (압축 폭탄 방어: 한 번에 푸는 양을 상한+1 로 제한)."""

    def __init__(self, kind: str):
        self.kind = kind
        self._d = zlib.decompressobj(31) if kind == "gzip" else None
        self._head = b""

    def feed(self, data: bytes, room: int) -> tuple[bytes, bool]:
        """(풀린 바이트(최대 room), 상한을 넘어 잘렸는가)"""
        if self._d is None:                              # deflate: zlib 머리말이 있으면 zlib, 없으면 raw
            self._head += data
            if len(self._head) < 2:
                return b"", False
            h = self._head
            wbits = 15 if (h[0] & 0x0F) == 8 and ((h[0] << 8) | h[1]) % 31 == 0 else -15
            self._d = zlib.decompressobj(wbits)
            data, self._head = h, b""
        out = bytearray()
        try:
            piece = self._d.decompress(data, room + 1)
            while True:
                out += piece
                if len(out) > room:
                    return bytes(out[:room]), True
                tail = self._d.unconsumed_tail
                if not tail or self._d.eof:
                    break
                piece = self._d.decompress(tail, room - len(out) + 1)
        except zlib.error:
            raise FetchError("encoding", "압축된 페이지를 풀 수 없습니다.") from None
        return bytes(out), False


def _charset_of(content_type: str) -> Optional[str]:
    m = re.search(r"charset\s*=\s*[\"']?([A-Za-z0-9_\-:.]+)", content_type or "", re.IGNORECASE)
    return m.group(1) if m else None


class HttpFetcher:
    """표준 라이브러리만 쓰는 가져오기. 서버 환경에서만 쓴다 (브라우저 실행판에서는 `fetch_page` 가 막는다).

    - 이름 확인 결과가 하나라도 사설·루프백·링크로컬·멀티캐스트·예약·CGNAT 이면 연결하지 않는다.
    - 확인한 IP 에 직접 연결하고, 연결 시점에 이름을 다시 확인하지 않는다 (DNS 리바인딩 방어). Host 헤더·TLS SNI 는 원래 호스트.
    - 리다이렉트는 3번까지, 매번 주소 검사와 이름 확인을 다시 한다. https → http 로 낮추는 이동은 거부한다.
    - 총 8초 · 본문 1.5MB(넘으면 자르고 truncated) · text/html·application/xhtml+xml 만 · 쿠키·인증 헤더 없음 · gzip/deflate 만(해제 후 상한 재확인).
    """

    def __init__(self, resolver: Optional[Resolver] = None, connector: Optional[Connector] = None, clock: Optional[Callable[[], float]] = None,
                 max_body: int = MAX_BODY_BYTES, timeout_s: float = TOTAL_TIMEOUT_S):
        self._resolver = resolver or _default_resolver
        self._connector = connector or _default_connector
        if clock is None:
            import time

            clock = time.monotonic
        self._clock = clock
        self.max_body = max_body
        self.timeout_s = timeout_s

    # -- 주소 → 연결할 IP 목록 ----------------------------------------------------
    def _addresses(self, vu: ValidatedUrl, deadline: float) -> list[str]:
        if vu.ip_literal:
            return [vu.ip_literal]
        try:
            answers = self._resolver(vu.host, vu.port)
        except FetchError:
            raise
        except OSError:
            raise FetchError("dns", "사이트 주소를 찾을 수 없습니다.") from None
        if self._clock() > deadline:
            raise FetchError("timeout", "페이지를 가져오는 데 8초가 넘게 걸려 멈췄습니다.")
        return check_resolved_addresses(answers)

    def _connect(self, vu: ValidatedUrl, ips: list[str], deadline: float):
        for ip in ips:                                    # 모두 이미 검사한 주소다. 이름을 다시 확인하지 않고 이 주소만 쓴다.
            remaining = deadline - self._clock()
            if remaining <= 0:
                raise FetchError("timeout", "페이지를 가져오는 데 8초가 넘게 걸려 멈췄습니다.")
            try:
                return ip, self._connector(ip, vu.port, tls=(vu.scheme == "https"), server_hostname=vu.host, timeout=remaining)
            except FetchError:
                raise
            except OSError:
                continue
        raise FetchError("connect", "사이트에 연결하지 못했습니다.")

    def _read_body(self, resp, deadline: float) -> tuple[bytes, bool]:
        enc = (resp.getheader("Content-Encoding") or "identity").strip().lower()
        if enc in ("", "identity"):
            inflater = None
        elif enc in ("gzip", "x-gzip"):
            inflater = _Inflater("gzip")
        elif enc == "deflate":
            inflater = _Inflater("deflate")
        else:
            raise FetchError("encoding", "지원하지 않는 압축 방식입니다 (gzip, deflate 만 읽습니다).")
        out = bytearray()
        raw_total = 0
        truncated = False
        while True:
            if self._clock() > deadline:
                raise FetchError("timeout", "페이지를 가져오는 데 8초가 넘게 걸려 멈췄습니다.")
            try:
                chunk = resp.read1(READ_CHUNK)
            except FetchError:
                raise
            except OSError:
                raise FetchError("connect", "페이지를 받는 중 연결이 끊겼습니다.") from None
            if not chunk:
                break
            raw_total += len(chunk)
            room = self.max_body - len(out)
            if inflater is None:
                if len(chunk) > room:
                    out += chunk[:room]
                    truncated = True
                    break
                out += chunk
            else:
                piece, cut = inflater.feed(chunk, room)
                out += piece
                if cut:
                    truncated = True
                    break
                if raw_total > self.max_body:             # 압축된 쪽도 상한을 지킨다
                    truncated = True
                    break
        return bytes(out), truncated

    def fetch(self, url: str) -> FetchResult:
        import http.client   # 지연 import

        deadline = self._clock() + self.timeout_s
        current = url
        chain: list[str] = []
        prev_scheme: Optional[str] = None
        for hop in range(MAX_REDIRECTS + 1):
            vu = validate_url(current)
            if prev_scheme == "https" and vu.scheme == "http":
                raise FetchError("https_downgrade", "안전한 연결(https)에서 보안이 없는 연결(http)로 옮겨 가는 주소는 따라가지 않습니다.")
            ips = self._addresses(vu, deadline)
            ip, sock = self._connect(vu, ips, deadline)
            resp = None
            reader = io.BufferedReader(_DeadlineReader(sock, deadline, self._clock), buffer_size=READ_CHUNK)
            try:
                request = (f"GET {vu.target} HTTP/1.1\r\nHost: {vu.host_header}\r\nUser-Agent: {USER_AGENT}\r\n"
                           "Accept: text/html,application/xhtml+xml\r\nAccept-Language: ko-KR,ko;q=0.9,en;q=0.5\r\n"
                           "Accept-Encoding: gzip, deflate\r\nConnection: close\r\n\r\n")
                try:
                    sock.sendall(request.encode("ascii"))
                    resp = http.client.HTTPResponse(_SockShim(reader), method="GET")
                    resp.begin()
                except FetchError:
                    raise
                except (OSError, http.client.HTTPException):
                    raise FetchError("connect", "사이트가 올바른 응답을 주지 않았습니다.") from None
                status = resp.status
                if status in (301, 302, 303, 307, 308):
                    location = (resp.getheader("Location") or "").strip()
                    if not location:
                        raise FetchError("status", "이동 주소가 없는 응답입니다.")
                    if hop >= MAX_REDIRECTS:
                        raise FetchError("redirect_limit", "주소가 3번 넘게 다른 곳으로 옮겨 가서 멈췄습니다.")
                    prev_scheme = vu.scheme
                    current = urljoin(vu.url, location)
                    chain.append(current)
                    continue
                if not 200 <= status < 300 or status in (204, 205):
                    raise FetchError("status", f"페이지를 열 수 없습니다 (응답 코드 {status}).")
                content_type = resp.getheader("Content-Type") or ""
                media = content_type.split(";", 1)[0].strip().lower()
                if media not in ALLOWED_CONTENT_TYPES:
                    raise FetchError("content_type", f"웹 페이지(HTML)가 아니라서 읽지 않았습니다 (종류: {media or '알 수 없음'}).")
                body, truncated = self._read_body(resp, deadline)
                return FetchResult(url=vu.url, status=status, content_type=media, charset=_charset_of(content_type), body=body,
                                   truncated=truncated, redirects=chain, remote_ip=ip)
            finally:
                for closable in (resp, reader, sock):          # 응답 → 읽기 어댑터 → 소켓 순서로 닫는다
                    try:
                        if closable is not None:
                            closable.close()
                    except Exception:   # noqa: BLE001 — 닫기 실패는 결과에 영향이 없다
                        pass
        raise FetchError("redirect_limit", "주소가 3번 넘게 다른 곳으로 옮겨 가서 멈췄습니다.")   # pragma: no cover — 반복문 안에서 끝난다


# --- 예시(오프라인) 페이지 ---------------------------------------------------------
PAGES_DIR = Path(__file__).resolve().parent.parent / "evals" / "datasets" / "pages"
_SAMPLE_NAME = re.compile(r"^[A-Za-z0-9_\-]{1,64}$")
_REPEAT = re.compile(rb"<!--pf:repeat times=([0-9]{1,6})-->(.*?)<!--pf:end-->", re.DOTALL)


def is_sample_url(url: str) -> bool:
    return isinstance(url, str) and url.startswith(SAMPLE_PREFIX)


def load_sample_index(root: Optional[Path] = None) -> dict:
    p = (root or PAGES_DIR) / "index.json"
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"pages": []}


class SampleFetcher:
    """`sample://<이름>` → `evals/datasets/pages/<이름>.html`. 합성 페이지이며 실제 쇼핑몰이 아니다. 네트워크를 쓰지 않는다.
    큰 페이지는 파일에 `<!--pf:repeat times=N-->…<!--pf:end-->` 표시를 두고 읽을 때 펼친다 (저장소·브라우저 번들에 1.5MB 파일을 두지 않기 위해)."""

    def __init__(self, root: Optional[Path] = None, max_body: int = MAX_BODY_BYTES):
        self.root = root or PAGES_DIR
        self.max_body = max_body

    def fetch(self, url: str) -> FetchResult:
        if not is_sample_url(url):
            raise FetchError("sample_not_found", "예시 페이지 주소가 아닙니다.")
        name = url[len(SAMPLE_PREFIX):]
        if name.endswith(".html"):
            name = name[:-5]
        if not _SAMPLE_NAME.match(name):
            raise FetchError("sample_not_found", "예시 페이지 이름이 올바르지 않습니다.")
        path = self.root / f"{name}.html"
        try:
            data = path.read_bytes()
        except OSError:
            raise FetchError("sample_not_found", "그런 예시 페이지가 없습니다.") from None
        data = _REPEAT.sub(lambda m: m.group(2) * min(int(m.group(1)), 100_000), data)
        truncated = len(data) > self.max_body
        return FetchResult(url=url, status=200, content_type="text/html", charset=None, body=data[: self.max_body], truncated=truncated)

    def list_samples(self, split: str = "dev") -> list[tuple[str, str]]:
        """(이름, 화면에 보일 설명) 목록. 기본은 개발용 페이지만 — 최종 확인용(holdout)은 평가에서만 쓴다."""
        out = []
        for p in load_sample_index(self.root).get("pages", []):
            if split in ("all", p.get("split", "dev")) and p.get("label"):
                out.append((p["id"], p["label"]))
        return out


def fetch_page(url: str, fetcher: Optional[Fetcher] = None) -> FetchResult:
    """주소에서 페이지를 가져온다. `sample://` 은 예시 페이지, 그 외는 서버 환경에서만 (브라우저 실행판에서는 막힘)."""
    if is_sample_url(url):
        return (fetcher if isinstance(fetcher, SampleFetcher) else SampleFetcher()).fetch(url)
    if config.IN_BROWSER:
        raise FetchError("unavailable", "이 브라우저 실행판에서는 주소로 가져올 수 없습니다. 페이지 소스를 붙여 넣거나 예시 페이지를 골라 주세요.")
    return (fetcher or HttpFetcher()).fetch(url)


# ---------------------------------------------------------------------------
# 3) 글자 처리 · 인젝션 선별
# ---------------------------------------------------------------------------
_WS = re.compile(r"\s+")


def clean_block(s: object, limit: int) -> str:
    """여러 줄 글: 줄마다 `clean_text` 를 적용하고 줄바꿈은 지킨다. 전체 길이 상한."""
    if not isinstance(s, str):
        return ""
    lines = [clean_text(line, 400) for line in s.splitlines()]
    return "\n".join(line for line in lines if line)[:limit].rstrip()


def clean_text(s: object, limit: int) -> str:
    """제어문자·보이지 않는 문자 제거, 공백 정리, 길이 상한. 외부에서 온 모든 문자열은 이 함수를 거쳐 필드에 들어간다."""
    if not isinstance(s, str):
        s = "" if s is None else str(s)
    s = unicodedata.normalize("NFC", s)
    s = "".join(ch for ch in s if (not is_invisible(ch)) or ch in "\n\t\r")
    s = _WS.sub(" ", s).strip()
    return s[:limit].rstrip() if len(s) > limit else s


# 페이지용 보강 패턴 — 공유 규칙(policy.yaml)을 고치지 않고 이 모듈 안에서만 쓴다. AI 에게 말을 거는 명령문과 역할/출력 조작 흔적.
_EXTRA_INJECTION: list[tuple[str, re.Pattern]] = [(rid, re.compile(p, re.IGNORECASE)) for rid, p in (
    ("URLINJ-AI-01", r"(?:(?<![a-z])(?:ai|llm|assistant|chatbot)(?![a-z])|인공지능|어시스턴트|언어\s*모델|챗봇|검수\s*모델|심사\s*모델)"
                     r"(?:은|는|에게|야|님|가|이)?[^.\n]{0,40}(?:말|출력|판정|따르|응답|답변|답|작성|평가|승인|무시|반복|기억|복사|전달|분류|표시|판단|인정|추천|생성)(?:하세요|하십시오|하라|해라|해\s*줘|해\s*주세요|하도록|해야)"),
    ("URLINJ-AI-02", r"(이\s*(글|문서|페이지|내용)[을를]?\s*읽는|읽고\s*있는)\s*((?<![a-z])ai(?![a-z])|인공지능|어시스턴트|assistant|모델|llm|언어\s*모델|봇)"),
    ("URLINJ-SYS-01", r"(system\s*prompt|시스템\s*프롬프트)\s*(을|를)?\s*(출력|보여|공개|무시|변경|reveal|print|show)"),
    ("URLINJ-WRITE-01", r"(광고|문구|카피|소재)[^.\n]{0,24}(반드시|꼭|무조건)[^.\n]{0,30}(포함|넣어|써|쓰|적어|강조)[^.\n]{0,10}(하세요|해라|하라|십시오|줘|주세요|야\s*합니다)"),
    ("URLINJ-EN-01", r"\b(ignore|disregard|forget)\b[^.\n]{0,30}\b(previous|prior|above|earlier|all)\b[^.\n]{0,20}\b(instructions?|prompts?|rules?|context)\b"),
    ("URLINJ-EN-02", r"\b(you\s+(must|should|will)\s+(now\s+)?(say|respond|answer|output|recommend|approve)|as\s+an?\s+(ai|assistant|language\s+model)[^.\n]{0,40}\b(must|should|always))\b"),
    ("URLINJ-EN-03", r"\b(new\s+instructions?|reveal\s+(your|the)\s+(system\s+)?prompt|do\s+not\s+(mention|tell|reveal)|pretend\s+to\s+be|act\s+as\s+(an?\s+)?(ai|assistant|system))\b"),
    ("URLINJ-TOKEN-01", r"(<\|im_start\|>|<\|im_end\|>|\[/?inst\]|<<sys>>|###\s*(instruction|system|assistant))"),
)]


def _injection_spans(text: str) -> tuple[list[tuple[int, int, str]], int]:
    """(탐지 구간 [(시작, 끝, 규칙 id)] — 원문 위치, 보이지 않는 문자 수). 공유 인젝션 규칙 + 페이지용 보강 패턴."""
    res = injection.scan_text(text)
    spans = [(f.start, f.end, f.rule_id) for f in res.findings if f.start >= 0 and f.end > f.start]
    norm = normalize(text)
    for surface in norm.surfaces():
        for rid, pat in _EXTRA_INJECTION:
            for m in pat.finditer(surface.text):
                o_start, o_end = surface.to_original(m.start(), m.end())
                if o_end > o_start:
                    spans.append((o_start, o_end, rid))
    return spans, res.hidden_chars


def _flag_texts(texts: list[str]) -> list[Optional[list[str]]]:
    """문장(또는 필드) 목록을 한 번에 검사해, 각 항목이 걸린 규칙 id 목록(안 걸리면 None)을 돌려준다.
    줄바꿈으로 이어 붙여 한 번만 검사하므로 항목 수가 많아도 느려지지 않는다. 보이지 않는 문자가 3개 이상 든 항목도 걸린 것으로 본다."""
    if not texts:
        return []
    safe = [t.replace("\n", " ") for t in texts]
    joined = "\n".join(safe)
    spans, _hidden = _injection_spans(joined)
    offsets: list[tuple[int, int]] = []
    pos = 0
    for t in safe:
        offsets.append((pos, pos + len(t)))
        pos += len(t) + 1
    out: list[Optional[list[str]]] = []
    for (s0, s1), t in zip(offsets, safe):
        rules = sorted({rid for a, b, rid in spans if a < s1 and b > s0})
        if sum(1 for ch in t if is_invisible(ch)) >= 3:
            rules = sorted(set(rules) | {"INJ-INSTR-01"})
        out.append(rules or None)
    return out


# ---------------------------------------------------------------------------
# 4) 문자 인코딩
# ---------------------------------------------------------------------------
_SNIFF_META = re.compile(rb"<meta[^>]+charset\s*=\s*[\"']?\s*([A-Za-z0-9_\-:.]+)", re.IGNORECASE)
_SNIFF_META2 = re.compile(rb"<meta[^>]+content\s*=\s*[\"'][^\"']*charset\s*=\s*([A-Za-z0-9_\-:.]+)", re.IGNORECASE)
_LATIN_LABELS = {"iso-8859-1", "latin1", "latin-1", "windows-1252", "cp1252", "ascii", "us-ascii", "iso8859-1"}
_KOREAN_LABELS = {"euc-kr", "euckr", "ks_c_5601-1987", "ksc5601", "korean", "cp949", "uhc", "x-windows-949", "ms949", "iso-2022-kr"}


def _norm_charset(label: Optional[str]) -> Optional[str]:
    if not label:
        return None
    low = label.strip().strip("\"'").lower()
    if low in _KOREAN_LABELS:
        return "cp949"       # EUC-KR 의 상위 집합 — 확장 한글(예: '똠')까지 읽는다
    if low in ("utf8", "utf-8"):
        return "utf-8"
    import codecs

    try:
        info = codecs.lookup(low)
    except LookupError:
        return None
    if not getattr(info, "_is_text_encoding", True) or info.name in ("utf-7", "idna", "punycode"):
        return None
    return info.name


def decode_html(data: bytes, header_charset: Optional[str] = None, partial: bool = False) -> tuple[str, str]:
    """바이트 → (글, 쓴 인코딩). 순서: BOM → utf-8 → 헤더 → meta → cp949. 디코딩이 안 되면 다음 후보로 넘어간다."""
    if data.startswith(b"\xef\xbb\xbf"):
        return data[3:].decode("utf-8", errors="replace"), "utf-8"
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        try:
            return data.decode("utf-16"), "utf-16"
        except UnicodeError:
            pass
    head = data[:4096]
    declared: list[str] = []
    for cand in (header_charset, (_SNIFF_META.search(head) or _SNIFF_META2.search(head) or [None, None])[1]):
        if isinstance(cand, bytes):
            cand = cand.decode("ascii", "replace")
        n = _norm_charset(cand)
        if n and n not in declared:
            declared.append(n)
    # utf-8 우선: 한글이 든 바이트가 utf-8 로 문제없이 읽히면 선언이 거짓이어도 utf-8 이 맞다 (euc-kr 글이 우연히 utf-8 로 읽힐 가능성은 사실상 없다).
    # 안 읽히면 선언된 인코딩 → cp949(EUC-KR 의 상위 집합) 순서로 넘어간다.
    latin = [n for n in declared if n in _LATIN_LABELS or n in ("latin_1", "iso8859-1", "ascii", "cp1252")]
    order = ["utf-8", *[n for n in declared if n not in latin], "cp949", *latin]       # 라틴 계열 선언은 한글 페이지의 흔한 오표기라 cp949 다음에 둔다
    seen: set[str] = set()
    for enc in order:
        if enc in seen:
            continue
        seen.add(enc)
        try:
            if partial:       # 상한에서 잘린 본문: 끝의 잘린 글자 하나 때문에 디코딩 전체가 실패하지 않게 한다
                import codecs

                return codecs.getincrementaldecoder(enc)(errors="strict").decode(data, final=False), enc
            return data.decode(enc), enc
        except (UnicodeError, LookupError):
            continue
    return data.decode("utf-8", errors="replace"), "utf-8(손상 대체)"


# ---------------------------------------------------------------------------
# 5) 스타일 · 숨김 판정
# ---------------------------------------------------------------------------
_NAMED_COLORS = {"white": (255, 255, 255), "black": (0, 0, 0), "red": (255, 0, 0), "green": (0, 128, 0), "blue": (0, 0, 255), "yellow": (255, 255, 0),
                 "gray": (128, 128, 128), "grey": (128, 128, 128), "silver": (192, 192, 192), "ivory": (255, 255, 240), "snow": (255, 250, 250),
                 "whitesmoke": (245, 245, 245), "ghostwhite": (248, 248, 255), "navy": (0, 0, 128), "orange": (255, 165, 0)}
_CLEAR = object()    # 투명 — 배경색 비교에서 제외


def _parse_color(v: str):
    v = (v or "").strip().lower().replace("!important", "").strip()
    if not v or v in ("inherit", "initial", "unset", "currentcolor"):
        return None
    if v == "transparent":
        return _CLEAR
    if v in _NAMED_COLORS:
        return _NAMED_COLORS[v]
    m = re.fullmatch(r"#([0-9a-f]{3}|[0-9a-f]{4}|[0-9a-f]{6}|[0-9a-f]{8})", v)
    if m:
        h = m.group(1)
        if len(h) in (3, 4):
            h = "".join(c * 2 for c in h)
        if len(h) == 8 and int(h[6:], 16) == 0:
            return _CLEAR
        return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))
    m = re.fullmatch(r"rgba?\(\s*([0-9.]+)[\s,]+([0-9.]+)[\s,]+([0-9.]+)(?:[\s,/]+([0-9.]+%?))?\s*\)", v)
    if m:
        a = m.group(4)
        if a is not None and float(a.rstrip("%")) / (100 if a.endswith("%") else 1) <= 0.05:
            return _CLEAR
        return tuple(max(0, min(255, int(float(x)))) for x in m.groups()[:3])
    return None


def _first_color(shorthand: str):
    for tok in re.split(r"\s+(?![^(]*\))", shorthand.strip()):
        c = _parse_color(tok)
        if c is not None:
            return c
    return None


def _strip_css_comments(css: str) -> str:
    """/* … */ 를 지운다. 정규식의 게으른 반복은 닫히지 않은 '/*' 가 많으면 제곱 시간이 되므로 find 로 한 번만 훑는다."""
    out: list[str] = []
    i = 0
    while True:
        j = css.find("/*", i)
        if j < 0:
            out.append(css[i:])
            break
        out.append(css[i:j])
        k = css.find("*/", j + 2)
        if k < 0:
            break
        i = k + 2
    return "".join(out)


def _unescape_css(s: str) -> str:
    s = _strip_css_comments(s)
    s = re.sub(r"\\([0-9a-fA-F]{1,6})\s?", lambda m: chr(int(m.group(1), 16)) if int(m.group(1), 16) < 0x110000 else "", s)
    return re.sub(r"\\(.)", r"\1", s)


def _style_props(decl: str) -> dict[str, str]:
    props: dict[str, str] = {}
    for part in _unescape_css(decl[:4000]).split(";"):
        k, sep, v = part.partition(":")
        if sep:
            props[k.strip().lower()] = v.replace("!important", "").strip().lower()
    return props


def _css_number(v: str) -> Optional[tuple[float, str]]:
    m = re.match(r"^\s*(-?[0-9]*\.?[0-9]+)\s*([a-z%]*)", v or "")
    return (float(m.group(1)), m.group(2)) if m else None


def _props_hidden(p: dict[str, str]) -> Optional[str]:
    """스타일 선언만 보고 '화면에 보이지 않는다' 고 할 수 있는 이유 (없으면 None)."""
    if p.get("display") == "none":
        return "display:none"
    if p.get("visibility") in ("hidden", "collapse"):
        return "visibility:hidden"
    fs = _css_number(p.get("font-size", ""))
    if fs is not None and ((fs[1] in ("", "px", "pt") and fs[0] <= 1) or (fs[1] in ("em", "rem", "ex") and fs[0] <= 0.1) or (fs[1] == "%" and fs[0] <= 10)):
        return "font-size:0"
    op = _css_number(p.get("opacity", ""))
    if op is not None and op[0] <= 0.05:
        return "opacity:0"
    for key in ("left", "right", "top", "bottom", "margin-left", "margin-right", "margin-top", "text-indent", "inset-inline-start"):
        n = _css_number(p.get(key, ""))
        if n is not None and ((n[1] in ("", "px", "pt") and n[0] <= -500) or (n[1] in ("em", "rem", "ex", "ch") and n[0] <= -30) or (n[1] in ("vw", "vh", "%") and n[0] <= -100)):
            return "offscreen"
    clip = p.get("clip", "")
    if re.match(r"rect\(\s*(0|1px)[\s,]+(0|1px)[\s,]+(0|1px)[\s,]+(0|1px)\s*\)", clip.replace("px", "px")):
        return "clip"
    if re.match(r"(inset\(\s*(50|100)%|circle\(\s*0|polygon\(\s*0)", p.get("clip-path", "")):
        return "clip-path"
    w, h = _css_number(p.get("width", "")), _css_number(p.get("height", ""))
    clipped = p.get("overflow") == "hidden" or p.get("overflow-x") == "hidden" or p.get("overflow-y") == "hidden"
    if w is not None and h is not None and w[0] <= 1 and h[0] <= 1:
        return "1px-box"
    if clipped and ((h is not None and h[0] == 0) or (w is not None and w[0] == 0) or (_css_number(p.get("max-height", "")) or (1, ""))[0] == 0):
        return "zero-box"
    if re.match(r"scale(?:x|y)?\(\s*0(\.0+)?\s*\)", p.get("transform", "")):
        return "scale(0)"
    if p.get("color") == "transparent" or p.get("-webkit-text-fill-color") == "transparent":
        return "color:transparent"
    return None


_HIDE_CLASSES = frozenset({"hidden", "d-none", "sr-only", "visually-hidden", "visuallyhidden", "screen-reader-text", "screen-reader-only", "invisible",
                           "hide", "u-hidden", "offscreen", "off-screen", "is-hidden", "a11y-hidden"})
_SELECTOR = re.compile(r"^([a-z][a-z0-9]*)?((?:\.[\w-]+)*)(#[\w-]+)?$", re.IGNORECASE)


def _parse_stylesheet(css: str) -> list[tuple[Optional[str], frozenset, Optional[str], dict[str, str]]]:
    """<style> 안의 단순 선택자(태그 · .클래스 · #id · 조합)만 읽는다. 자손/자식 결합자·의사 클래스는 무시한다."""
    rules = []
    css = _unescape_css(css)
    for chunk in css.split("}")[:2000]:          # '}' 로 끊으면 @media 안의 규칙도 한 덩어리씩 나온다. 정규식 대신 쪼개기: 선형 시간
        head, brace, body = chunk.rpartition("{")
        if not brace:
            continue
        sel_text = head.rpartition("{")[2]
        if len(rules) >= 500:
            break
        props = _style_props(body)
        if not props:
            continue
        for sel in sel_text.split(","):
            m = _SELECTOR.match(sel.strip())
            if not m or not sel.strip():
                continue
            tag, classes, ident = m.group(1), m.group(2), m.group(3)
            rules.append((tag.lower() if tag else None, frozenset(c for c in classes.split(".") if c), ident[1:] if ident else None, props))
    return rules


class _RuleIndex:
    """스타일 규칙을 선택자의 열쇠(id → 첫 클래스 → 태그)로 색인해 두고, 요소마다 후보 규칙만 원래 순서대로 적용한다 (요소 수 × 규칙 수 만큼 훑지 않는다)."""

    def __init__(self, rules: list):
        self.rules = rules
        self.by_key: dict[tuple[str, str], list[int]] = {}
        for i, (tag, classes, ident, _props) in enumerate(rules):
            key = ("id", ident) if ident else (("class", sorted(classes)[0]) if classes else ("tag", tag or ""))
            self.by_key.setdefault(key, []).append(i)

    def props_for(self, tag: str, classes: frozenset, ident: str) -> dict[str, str]:
        if not self.rules:
            return {}
        cand: list[int] = list(self.by_key.get(("tag", tag), ()))
        for c in classes:
            cand += self.by_key.get(("class", c), ())
        if ident:
            cand += self.by_key.get(("id", ident), ())
        props: dict[str, str] = {}
        for i in sorted(set(cand)):
            rtag, rclasses, rid, rprops = self.rules[i]
            if (rtag is None or rtag == tag) and rclasses <= classes and (rid is None or rid == ident):
                props.update(rprops)
        return props


def _close_colors(a, b, tol: int = 12) -> bool:
    return isinstance(a, tuple) and isinstance(b, tuple) and all(abs(x - y) <= tol for x, y in zip(a, b))


# ---------------------------------------------------------------------------
# 6) 읽기 결과 모델
# ---------------------------------------------------------------------------
class HiddenText(BaseModel):
    text: str
    reason: str                  # display:none · comment · noscript … (상세 기록용)
    injection: bool = False


class InjectionHit(BaseModel):
    where: Literal["hidden", "visible", "meta", "jsonld", "attribute"]
    text: str = ""               # 걸린 문장 (표시 전에 반드시 HTML 이스케이프; 프롬프트에 넣지 말 것)
    rules: list[str] = Field(default_factory=list)


class PriceInfo(BaseModel):
    amount: Optional[int] = None          # 원화일 때만 정수. 다른 통화는 text 로만
    currency: str = "KRW"
    text: str                             # 일관된 표시: 29,900원 / USD 29.90
    source: Literal["jsonld", "og", "body"]
    kind: Literal["sale", "list", "unknown", "conditional", "other"] = "unknown"


class JsonLdOffer(BaseModel):
    price: Optional[PriceInfo] = None
    high_price: Optional[PriceInfo] = None
    valid_until: str = ""
    availability: str = ""


class JsonLdProduct(BaseModel):
    types: list[str] = Field(default_factory=list)
    name: str = ""
    description: str = ""
    brand: str = ""
    category: str = ""
    color: str = ""
    material: str = ""
    images: list[str] = Field(default_factory=list)
    offers: list[JsonLdOffer] = Field(default_factory=list)
    properties: list[tuple[str, str]] = Field(default_factory=list)


class Sentence(BaseModel):
    text: str
    evidence: str = ""


class PageSignals(BaseModel):
    script_count: int = 0
    has_password_input: bool = False
    purchase_words: int = 0
    price_cards: int = 0                  # 링크·이미지와 가격을 함께 가진 목록 항목 수 (여러 상품 목록 단서)
    og_type: str = ""
    article_hint: bool = False
    jsonld_list_items: int = 0
    empty_app_root: bool = False
    login_words: bool = False


class PageExtract(BaseModel):
    url: str = ""
    encoding: str = ""
    title: str = ""
    h1: str = ""
    meta_description: str = ""
    og: dict[str, list[str]] = Field(default_factory=dict)
    products: list[JsonLdProduct] = Field(default_factory=list)
    jsonld_blocks: int = 0
    jsonld_invalid: int = 0
    body_prices: list[PriceInfo] = Field(default_factory=list)
    sentences: list[Sentence] = Field(default_factory=list)         # 보이는 본문(인젝션 걸린 문장 제외)
    visible_chars: int = 0
    hidden_text: list[HiddenText] = Field(default_factory=list)
    injection_hits: list[InjectionHit] = Field(default_factory=list)
    image_urls: list[str] = Field(default_factory=list)
    signals: PageSignals = Field(default_factory=PageSignals)
    truncated: bool = False
    plain_text: bool = False
    parse_error: bool = False
    notes: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# 7) HTML 읽기
# ---------------------------------------------------------------------------
_VOID = frozenset({"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr", "keygen", "command"})
_BLOCK = frozenset({"address", "article", "aside", "blockquote", "body", "caption", "details", "div", "dl", "fieldset", "figcaption", "figure", "footer",
                    "form", "h1", "h2", "h3", "h4", "h5", "h6", "header", "hr", "li", "main", "nav", "ol", "p", "pre", "section", "summary", "table", "tbody",
                    "tfoot", "thead", "tr", "ul", "br", "option", "select", "button", "html", "head", "title"})
_BLOCK_START = _BLOCK | {"dt"}          # <dt> 는 시작할 때만 줄을 끊고(앞 항목과 분리), 끝에서는 값과 이어 붙인다
_BLOCK_END = _BLOCK | {"dd"}            # <dd> 는 끝날 때만 줄을 끊는다
_CELL_SEP = {"th": ": ", "dt": ": ", "td": " / "}     # 표의 머리칸·정의 목록의 항목은 값과 한 줄로 이어 붙인다 ('소재: 면 100%')
_EXCLUDE_TAGS = frozenset({"nav", "footer", "aside"})                 # 본문 요약·사실 후보에서 뺀다 (메뉴·바닥글·추천 영역)
_STOP_FOR = {"li": {"ul", "ol", "menu"}, "dt": {"dl"}, "dd": {"dl"}, "tr": {"table", "tbody", "thead", "tfoot"}, "td": {"tr", "table"}, "th": {"tr", "table"},
             "option": {"select", "datalist"}}
_CLOSES = {"li": {"li"}, "dt": {"dt", "dd"}, "dd": {"dt", "dd"}, "tr": {"tr", "td", "th"}, "td": {"td", "th"}, "th": {"td", "th"}, "option": {"option"}}
_P_BOUNDARY = frozenset({"div", "section", "article", "td", "th", "li", "body", "blockquote", "ul", "ol", "table", "aside", "nav", "header", "footer", "form",
                         "main", "button", "figure", "details", "dl", "html"})
_P_CLOSERS = frozenset({"p", "div", "ul", "ol", "table", "h1", "h2", "h3", "h4", "h5", "h6", "form", "hr", "section", "article", "blockquote", "pre", "dl", "nav",
                        "header", "footer", "aside", "main", "figure", "address", "fieldset"})
_CARD_WORDS = ("item", "card", "product", "goods", "prd", "prod")
_IMG_SKIP = re.compile(r"(logo|icon|sprite|pixel|blank|spacer|tracking|favicon|1x1|badge|bnr|banner)", re.IGNORECASE)

_MAN = re.compile(r"(\d{1,4})\s*만\s*(?:(\d{1,3})\s*천)?\s*(?:(\d{1,3}(?:,\d{3})*|\d{1,4})\s*)?원")
_CHEON = re.compile(r"(\d{1,3})\s*천\s*원")
_WON = re.compile(r"(?:(?:₩|KRW)\s*(\d{1,3}(?:,\d{3})+|\d+)|(\d{1,3}(?:,\d{3})+|\d+)\s*(?:원|₩|KRW))", re.IGNORECASE)
_LIST_LABEL = re.compile(r"(정가|소비자가|정상가|원가|시중가|권장\s*소비자가|할인\s*전|기존가|판매\s*전)")
_COND_LABEL = re.compile(r"(쿠폰|카드|회원|첫\s*구매|앱\s*전용|신규|웰컴|결제\s*할인|추가\s*할인|적용)")
_OTHER_BEFORE = re.compile(r"(배송비|배송|적립|포인트|캐시백|사은품|최소|최대|보증금|연회비|수수료|설치비|할부|상품권)")
_OTHER_AFTER = re.compile(r"^\s*(이상|이하|미만|초과|할인\s*쿠폰|쿠폰|적립|포인트|캐시백|추가|별도)")
_SALE_LABEL = re.compile(r"(할인가|특가|세일가|혜택가|최종|판매가|오늘만|지금|회원가|구매가|결제\s*금액|가격|price)", re.IGNORECASE)
_BEFORE_LABELS = ((_LIST_LABEL, "list"), (_OTHER_BEFORE, "other"), (_COND_LABEL, "conditional"), (_SALE_LABEL, "sale"))
_PURCHASE = re.compile(r"(구매하기|바로\s*구매|장바구니|주문하기|결제하기|담기|add\s+to\s+cart|buy\s+now|구매\s*하기|옵션\s*선택|신청하기|가입하기|가입\s*신청|상담\s*신청|한도\s*조회|발급\s*신청|예약하기|무료\s*체험|다운로드|설치하기)", re.IGNORECASE)
_LOGIN_WORDS = re.compile(r"(로그인|sign\s*in|log\s*in|회원\s*전용|로그인\s*후)", re.IGNORECASE)
_SENT_SPLIT = re.compile(r"(?<=[.!?。…])\s+|\s*[|｜]\s*|\s*[•▪▶▷◆◇■□●○★☆※]\s*|\s{3,}")
# 사실 후보로 볼 만한 문장의 단서. '까지·부터·이상·이하·이내·가능' 은 일반 문장에도 흔해서 숫자가 함께 있을 때만 단서로 본다.
_FACT_CUE = re.compile(r"\d|무료|배송|반품|교환|환불|쿠폰|할인|적립|포인트|무이자|보증|A/?S\b|정품|한정|재고|소재|원단|원산지|제조|성분|용량|색상|사이즈|구성|증정|사은품|별도|추가|유효|기한|만료|"
                       r"자동\s*결제|해지|위약금|수수료|심사|금리|이자|한도|상환|연체|하락|손실|위험|불이익|(?<![가-힣])유의|제한|책임|건강기능식품|의약품|섭취|보관|알레르기|부작용|주의|상담|임산부|"
                       r"인증|특허|수상|불가|제외|제공|지원|포함|당일|익일", re.IGNORECASE)
_COND_CUE = re.compile(r"이상|이하|까지|부터|이내|제외|한정|1인|인당|단,|※|조건|별도|자동\s*결제|유효|기한|만료|심사|해지|위약금|쿠폰|적용\s*시|결제\s*시|구매\s*시|주문\s*시|연체|하락|손실|불가")
_BOILER = re.compile(r"(로그인|회원가입|개인정보|이용약관|고객센터|사업자\s*등록|통신판매|대표자|copyright|©|all rights|쿠키\s*(정책|설정|동의|허용|사용)|쿠키를|cookie\s*(policy|settings|consent)|뉴스레터|구독하기|공유하기|오시는\s*길|FAX|팩스|"
                     r"장바구니\s*담기|바로\s*구매|관심\s*상품|top\s*으로|맨\s*위로|\bmenu\b|전체\s*메뉴)", re.IGNORECASE)


def _collapse(s: str) -> str:
    return _WS.sub(" ", s).strip()


def _fmt_krw(n: int) -> str:
    return f"{n:,}원"


def _price_from_value(raw: object, currency: str, source: str, kind: str = "unknown") -> Optional[PriceInfo]:
    """JSON-LD·메타의 가격 값 → PriceInfo. 쉼표·원·₩ 를 걷어내 숫자를 보존하고 표시는 일관되게 만든다."""
    from decimal import Decimal, InvalidOperation

    if isinstance(raw, bool) or raw is None:
        return None
    s = str(raw).strip()
    if not s or len(s) > 40:
        return None
    s = re.sub(r"[₩원\s]|KRW|won", "", s, flags=re.IGNORECASE).replace(",", "")
    try:
        d = Decimal(s)
    except InvalidOperation:
        return None
    if not d.is_finite() or d < 0 or d > Decimal(10) ** 10:
        return None
    cur = (currency or "KRW").strip().upper()[:8] or "KRW"
    if cur in ("KRW", "WON", "₩"):
        if d != d.to_integral_value():
            return PriceInfo(amount=None, currency="KRW", text=f"{d:f}원", source=source, kind=kind)   # type: ignore[arg-type]
        return PriceInfo(amount=int(d), currency="KRW", text=_fmt_krw(int(d)), source=source, kind=kind)   # type: ignore[arg-type]
    shown = f"{d:,.2f}" if d != d.to_integral_value() else f"{int(d):,}"
    return PriceInfo(amount=None, currency=cur, text=f"{cur} {shown}", source=source, kind=kind)   # type: ignore[arg-type]


class _El:
    __slots__ = ("tag", "uid", "hidden", "color", "bg", "strike", "exclude", "card", "has_price", "has_link", "h1", "ui")

    def __init__(self, tag: str, uid: int):
        self.tag, self.uid = tag, uid
        self.hidden: Optional[str] = None
        self.color = None
        self.bg = (255, 255, 255)
        self.strike = False
        self.exclude = False
        self.card = False
        self.ui = False
        self.has_price = False
        self.has_link = False
        self.h1 = False


class _Stop(Exception):
    """읽을 수 있는 양의 상한에 닿았다 — 거기까지의 결과를 쓴다."""


class _PageParser(HTMLParser):
    def __init__(self, page_url: str, rules):
        super().__init__(convert_charrefs=True)
        self.page_url = page_url
        self.rules = _RuleIndex(rules)
        self.stack: list[_El] = []
        self._uid = 0
        self._tags = 0
        self._cap: Optional[list] = None            # [종류, 모은 조각들, 속성]
        self._cap_len = 0
        self.title = ""
        self.meta: dict[str, list[str]] = {}
        self.meta_charset = ""
        self.jsonld_raw: list[str] = []
        self.h1s: list[str] = []
        self._h1_parts: list[str] = []
        self.lines: list[str] = []
        self.chrome_lines: list[str] = []
        self.ui_lines: list[str] = []
        self._ui: list[str] = []
        self.visible_chars = 0
        self._stored_chars = 0
        self._vis: list[tuple[str, bool]] = []
        self._chrome: list[str] = []
        self._line_card: Optional[_El] = None
        self.body_prices: list[PriceInfo] = []
        self.hidden: list[HiddenText] = []
        self._hidden_chars = 0
        self._hidden_root = -1
        self._hidden_parts: list[str] = []
        self._hidden_part_chars = 0
        self._hidden_reason = ""
        self.attr_texts: list[str] = []
        self.images: list[str] = []
        self.base_href = ""
        self.signals = PageSignals()
        self.cards_done = 0
        self.app_root_ids: list[_El] = []

    # -- 상태 ---------------------------------------------------------------------------
    def _top(self) -> Optional[_El]:
        return self.stack[-1] if self.stack else None

    def _in(self, tags) -> bool:
        return any(e.tag in tags for e in self.stack)

    def _implied_close(self, tag: str) -> None:
        closers = _CLOSES.get(tag)
        if closers:
            stops = _STOP_FOR.get(tag, set())
            for i in range(len(self.stack) - 1, -1, -1):
                t = self.stack[i].tag
                if t in closers:
                    self._pop_to(i)
                    break
                if t in stops:
                    break
        if tag in _P_CLOSERS:
            for i in range(len(self.stack) - 1, -1, -1):
                t = self.stack[i].tag
                if t == "p":
                    self._pop_to(i)
                    break
                if t in _P_BOUNDARY:
                    break

    def _pop_to(self, index: int) -> None:
        """stack[index:] 를 닫는다 (안쪽부터)."""
        while len(self.stack) > index:
            el = self.stack.pop()
            if el.tag in _CELL_SEP:
                self.handle_data(_CELL_SEP[el.tag])
            if el.tag in _BLOCK_END:
                self._flush_line()          # 줄을 먼저 비워야 이 항목 안의 가격이 항목에 기록된다
            if el.card and el.has_price and el.has_link:
                self.cards_done += 1
            if el.h1:
                text = _collapse("".join(self._h1_parts))
                self._h1_parts = []
                if text:
                    self.h1s.append(text)
            if self._hidden_root >= len(self.stack):
                self._flush_hidden()

    def _flush_hidden(self) -> None:
        text = _collapse("".join(self._hidden_parts))
        if text and len(self.hidden) < MAX_HIDDEN_SNIPPETS and self._hidden_chars < MAX_HIDDEN_CHARS:
            self.hidden.append(HiddenText(text=text[:HIDDEN_SNIPPET_MAX], reason=self._hidden_reason))
            self._hidden_chars += len(text)
        self._hidden_parts, self._hidden_part_chars, self._hidden_root, self._hidden_reason = [], 0, -1, ""

    # -- 줄 모으기 ----------------------------------------------------------------------
    def _flush_line(self) -> None:
        if self._ui:
            text = _collapse("".join(self._ui))
            self._ui = []
            if text and len(self.ui_lines) < 100:
                self.ui_lines.append(text[:120])
        if self._chrome:
            text = _collapse("".join(self._chrome))
            self._chrome = []
            if text and len(self.chrome_lines) < 200:
                self.chrome_lines.append(text[:400])
        if not self._vis:
            return
        marked = "".join(f"\ue000{t}\ue001" if strike else t for t, strike in self._vis)
        self._vis = []
        card, self._line_card = self._line_card, None
        collapsed = _collapse(marked)
        plain: list[str] = []
        spans: list[tuple[int, int]] = []
        open_at = -1
        for ch in collapsed:
            if ch == "\ue000":
                open_at = len(plain)
            elif ch == "\ue001":
                if open_at >= 0:
                    spans.append((open_at, len(plain)))
                open_at = -1
            else:
                plain.append(ch)
        line = re.sub(r"(?:\s*[/:])+\s*$", "", "".join(plain).strip()).strip()      # 표 칸 사이에 끼운 이음표가 줄 끝에 남지 않게
        if not line:
            return
        self.visible_chars += len(line)
        if self._stored_chars < MAX_VISIBLE_CHARS:
            self.lines.append(line[:4000])
            self._stored_chars += len(line)
        found = self._scan_prices(line, spans)
        if found and card is not None:
            card.has_price = True
        self.body_prices.extend(found)

    def _scan_prices(self, line: str, strikes: list[tuple[int, int]]) -> list[PriceInfo]:
        found: list[tuple[int, int, int]] = []
        taken: list[tuple[int, int]] = []
        for m in _MAN.finditer(line):
            amount = int(m.group(1)) * 10_000 + (int(m.group(2)) * 1_000 if m.group(2) else 0) + (int(m.group(3).replace(",", "")) if m.group(3) else 0)
            found.append((m.start(), m.end(), amount))
            taken.append((m.start(), m.end()))
        for m in _CHEON.finditer(line):
            if not any(a <= m.start() < b for a, b in taken):
                found.append((m.start(), m.end(), int(m.group(1)) * 1_000))
                taken.append((m.start(), m.end()))
        for m in _WON.finditer(line):
            if any(a <= m.start() < b for a, b in taken):
                continue
            found.append((m.start(), m.end(), int((m.group(1) or m.group(2)).replace(",", ""))))
        out: list[PriceInfo] = []
        prev_end = 0
        for start, end, amount in sorted(found):
            if not 100 <= amount <= 100_000_000:
                prev_end = end
                continue
            before = line[max(prev_end, start - 12):start]
            after = line[end:end + 10]
            kind = "unknown"
            if any(a < end and b > start for a, b in strikes):
                kind = "list"
            else:
                nearest = -1
                for pat, k in _BEFORE_LABELS:           # 가격 바로 앞에 가장 가까운 이름표가 종류를 정한다 ('무료배송 판매가 9,900원' 은 판매가)
                    last = max((m.end() for m in pat.finditer(before)), default=-1)
                    if last > nearest:
                        nearest, kind = last, k
                if nearest < 0 and _OTHER_AFTER.match(after):
                    kind = "other"
            out.append(PriceInfo(amount=amount, text=_fmt_krw(amount), source="body", kind=kind))   # type: ignore[arg-type]
            prev_end = end
        return out

    # -- 속성 → 요소 상태 ---------------------------------------------------------------
    def _compute(self, el: _El, tag: str, a: dict[str, str]) -> None:
        parent = self._top()
        if parent is not None:
            el.color, el.bg, el.strike, el.exclude, el.ui = parent.color, parent.bg, parent.strike, parent.exclude, parent.ui
        classes = frozenset(c for c in re.split(r"\s+", a.get("class", "").lower()) if c)
        ident = a.get("id", "").lower()
        props = self.rules.props_for(tag, classes, ident)
        if "style" in a:
            props.update(_style_props(a["style"]))
        reason: Optional[str] = None
        if tag in ("noscript", "template"):
            reason = tag
        elif tag == "svg":
            reason = "svg"
        elif "hidden" in a:
            reason = "hidden"
        elif a.get("aria-hidden", "").strip().lower() == "true":
            reason = "aria-hidden"
        else:
            bad = classes & _HIDE_CLASSES
            reason = ("class:" + sorted(bad)[0]) if bad else _props_hidden(props)
        # 색: 글자색·배경색 상속
        color = _first_color(props["color"]) if "color" in props else None
        bg = None
        for k in ("background-color", "background"):
            if k in props:
                bg = _first_color(props[k])
                if bg is not None:
                    break
        if tag == "font" and a.get("color"):
            color = _parse_color(a["color"]) or color
        if tag == "body":
            color = _parse_color(a.get("text", "")) or color
        if a.get("bgcolor"):
            bg = _parse_color(a["bgcolor"]) or bg
        if color is not None and color is not _CLEAR:
            el.color = color
        if bg is not None and bg is not _CLEAR:
            el.bg = bg
        if reason is None and el.color is not None and _close_colors(el.color, el.bg):
            reason = "color=background"
        el.hidden = reason
        if tag in ("del", "s", "strike") or props.get("text-decoration", "").find("line-through") >= 0 or props.get("text-decoration-line", "") == "line-through":
            el.strike = True
        if tag in _EXCLUDE_TAGS:
            el.exclude = True
        if tag in ("button", "select", "option", "datalist"):
            el.ui = True            # 버튼·선택 상자의 글자는 본문이 아니다 (구매 단서로만 본다)
        if tag in ("li", "article", "tr") or (tag == "div" and any(w in classes_and_id(classes, ident) for w in _CARD_WORDS)):
            el.card = True
        if tag == "h1":
            el.h1 = True

    # -- HTMLParser 훅 ------------------------------------------------------------------
    def handle_starttag(self, tag, attrs):
        self._tags += 1
        if self._tags > MAX_TAGS:
            raise _Stop()
        a: dict[str, str] = {}
        for k, v in attrs:
            k = k.lower()
            if k not in a:
                a[k] = v if v is not None else ""
        if self._cap is not None and self._cap[0] == "title":
            if tag in _BLOCK or tag in ("script", "style", "meta", "link"):
                self._end_capture()          # 닫는 </title> 이 없는 깨진 문서: 제목 모으기를 거기서 끝내고 이어서 읽는다
            else:
                return                       # 제목 안의 <b> 같은 태그는 글자로만 본다
        if tag in ("script", "style"):
            if tag == "script":
                self.signals.script_count += 1
            kind = "jsonld" if (tag == "script" and "ld+json" in a.get("type", "").lower()) else ("style" if tag == "style" else "script")
            self._cap, self._cap_len = [kind, [], a], 0
            return
        if tag == "title" and not self._in({"svg"}) and not self.title:
            self._cap, self._cap_len = ["title", [], a], 0
            return
        self._implied_close(tag)
        if tag in _VOID:
            self._void(tag, a)
            return
        if len(self.stack) >= MAX_DEPTH:
            raise _Stop()
        self._uid += 1
        el = _El(tag, self._uid)
        self._compute(el, tag, a)
        if tag in _BLOCK_START:
            self._flush_line()
        if el.hidden and self._hidden_root < 0:
            self._hidden_root, self._hidden_reason = len(self.stack), el.hidden
        self.stack.append(el)
        if tag == "a" and a.get("href"):
            card = self._nearest_card()
            if card is not None:
                card.has_link = True
        if tag in ("div", "span", "main") and a.get("id", "").lower() in ("root", "app", "__next", "__nuxt", "app-root", "main-app"):
            self.app_root_ids.append(el)
        for key in ("alt", "title", "aria-label"):
            if len(a.get(key, "")) >= 10 and len(self.attr_texts) < 100:
                self.attr_texts.append(a[key][:300])

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in _VOID:
            self.handle_endtag(tag)

    def _void(self, tag: str, a: dict[str, str]) -> None:
        top = self._top()
        hidden_ctx = self._hidden_root >= 0 or (top is not None and top.hidden is not None)
        if tag == "meta":
            name = (a.get("name") or a.get("property") or a.get("itemprop") or "").strip().lower()
            content = a.get("content", "")
            if a.get("charset"):
                self.meta_charset = a["charset"]
            if name and content and len(self.meta) < 80 and (name == "description" or name.startswith(("og:", "product:", "twitter:"))):
                self.meta.setdefault(name, [])
                if len(self.meta[name]) < 12:
                    self.meta[name].append(content[:600])
        elif tag == "base" and a.get("href") and not self.base_href:
            self.base_href = a["href"]
        elif tag == "br":
            self._flush_line()
        elif tag == "hr":
            self._flush_line()
        elif tag == "img":
            self._image(a, hidden_ctx)
            for key in ("alt", "title"):
                if len(a.get(key, "")) >= 10 and len(self.attr_texts) < 100:
                    self.attr_texts.append(a[key][:300])
            card = self._nearest_card()
            if card is not None and not hidden_ctx:
                card.has_link = True
        elif tag == "input":
            typ = a.get("type", "text").lower()
            if typ == "password" and not hidden_ctx:
                self.signals.has_password_input = True
            elif typ in ("submit", "button") and a.get("value") and not hidden_ctx:
                self._ui.append(" " + a["value"][:60] + " ")
                self._flush_line()

    def _nearest_card(self) -> Optional[_El]:
        for el in reversed(self.stack):
            if el.card:
                return el
        return None

    def _image(self, a: dict[str, str], hidden_ctx: bool) -> None:
        if hidden_ctx or self._in(_EXCLUDE_TAGS) or len(self.images) >= MAX_IMAGES * 3:
            return
        src = a.get("src") or a.get("data-src") or a.get("data-original") or a.get("data-lazy-src") or ""
        if not src and a.get("srcset"):
            src = a["srcset"].split(",")[0].strip().split(" ")[0]
        for dim in ("width", "height"):
            n = _css_number(a.get(dim, ""))
            if n is not None and n[0] <= 2:
                return
        if src:
            self.images.append(src)

    def _end_capture(self) -> None:
        kind, parts = self._cap[0], self._cap[1]
        text = "".join(parts)
        self._cap, self._cap_len = None, 0
        if kind == "title":
            self.title = _collapse(text)[:500]
        elif kind == "jsonld" and len(self.jsonld_raw) < MAX_JSONLD_BLOCKS:
            self.jsonld_raw.append(text[:MAX_JSONLD_BYTES])

    def handle_endtag(self, tag):
        if self._cap is not None:
            kind = self._cap[0]
            if (tag in ("script", "style") and kind in ("jsonld", "style", "script")) or (tag == "title" and kind == "title"):
                self._end_capture()
            return
        if tag in _VOID:
            return
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i].tag == tag:
                self._pop_to(i)
                return

    def handle_data(self, data):
        if self._cap is not None:
            if self._cap_len < (MAX_JSONLD_BYTES if self._cap[0] == "jsonld" else MAX_STYLE_BYTES):
                self._cap[1].append(data)
                self._cap_len += len(data)
            return
        if not data:
            return
        if self._hidden_root >= 0:
            if self._hidden_chars + self._hidden_part_chars < MAX_HIDDEN_CHARS:
                self._hidden_parts.append(data)
                self._hidden_part_chars += len(data)
            return
        top = self._top()
        if top is not None and top.ui:
            self._ui.append(data)
            return
        if top is not None and top.exclude:
            self._chrome.append(data)
            return
        if not data.strip() and not self._vis:
            return
        if (top is not None and top.h1 or self._in({"h1"})) and sum(len(x) for x in self._h1_parts) < 600:
            self._h1_parts.append(data)
        if self._line_card is None:
            self._line_card = self._nearest_card()
        self._vis.append((data, bool(top and top.strike)))

    def handle_comment(self, data):
        text = _collapse(data)
        if len(text) >= 3 and not re.match(r"^\[if\b|^<!\[endif", text, re.IGNORECASE) and len(self.hidden) < MAX_HIDDEN_SNIPPETS and self._hidden_chars < MAX_HIDDEN_CHARS:
            self.hidden.append(HiddenText(text=text[:HIDDEN_SNIPPET_MAX], reason="comment"))
            self._hidden_chars += len(text)

    def unknown_decl(self, data):
        self.handle_comment(data)

    def finish(self) -> None:
        if self._cap is not None:
            self._end_capture()
        self._flush_line()
        self._pop_to(0)
        if self._hidden_root >= 0 or self._hidden_parts:
            self._flush_hidden()


def classes_and_id(classes: frozenset, ident: str) -> str:
    return " ".join(sorted(classes)) + " " + ident


def _split_sentences(line: str) -> list[str]:
    """한 줄을 문장 단위로 쪼갠다. 160자를 넘는 조각은 쉼표·공백에서 나눠 모두 남긴다 (끝을 잘라 버리면 뒤에 붙은 조건이 사라진다)."""
    out: list[str] = []
    for part in _SENT_SPLIT.split(line):
        part = _collapse(part)
        if not part:
            continue
        while len(part) > FACT_MAX:
            cut = max(part.rfind(sep, 0, FACT_MAX) for sep in (", ", "; ", " / ", " – ", " - "))
            if cut < 40:
                cut = part.rfind(" ", 0, FACT_MAX)
            if cut < 40:
                cut = FACT_MAX
            out.append(part[:cut].rstrip(" ,;/"))
            part = part[cut:].lstrip(" ,;/-–")
        if part:
            out.append(part)
    return out


# ---------------------------------------------------------------------------
# 8) JSON-LD
# ---------------------------------------------------------------------------
_PRODUCT_TYPES = frozenset({"product", "productgroup", "individualproduct", "someproducts", "vehicle", "financialproduct", "loanorcredit", "creditcard",
                            "bankaccount", "depositaccount", "paymentcard", "softwareapplication", "mobileapplication", "webapplication", "course", "service",
                            "drug", "dietarysupplement", "mortgageloan", "insuranceproduct"})
_ARTICLE_TYPES = frozenset({"article", "blogposting", "newsarticle", "report", "scholarlyarticle", "techarticle", "socialmediaposting", "discussionforumposting"})
_SKIP_KEYS = frozenset({"review", "reviews", "author", "publisher", "isrelatedto", "issimilarto", "hasvariant", "isaccessoryorsparepartfor", "isconsumablefor",
                        "creator", "provider", "seller", "manufacturer", "aggregaterating", "offers", "image", "logo", "breadcrumb"})


def _types_of(node: dict) -> list[str]:
    t = node.get("@type")
    items = t if isinstance(t, list) else [t]
    return [str(x).rsplit("/", 1)[-1].strip() for x in items if isinstance(x, (str,))]


def _first_str(v: object, limit: int = 600) -> str:
    if isinstance(v, str):
        return v[:limit]
    if isinstance(v, dict):
        return _first_str(v.get("name") or v.get("@value") or "", limit)
    if isinstance(v, list):
        for x in v:
            s = _first_str(x, limit)
            if s:
                return s
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return str(v)
    return ""


def _offer_of(o: dict) -> JsonLdOffer:
    cur = _first_str(o.get("priceCurrency")) or "KRW"
    spec = o.get("priceSpecification")
    spec = spec[0] if isinstance(spec, list) and spec and isinstance(spec[0], dict) else (spec if isinstance(spec, dict) else {})
    raw = o.get("price", o.get("lowPrice", spec.get("price")))
    cur = _first_str(o.get("priceCurrency") or spec.get("priceCurrency")) or cur
    return JsonLdOffer(price=_price_from_value(raw, cur, "jsonld", "sale"),
                       high_price=_price_from_value(o.get("highPrice"), cur, "jsonld", "sale") if o.get("highPrice") is not None else None,
                       valid_until=_first_str(o.get("priceValidUntil"), 40), availability=_first_str(o.get("availability"), 80).rsplit("/", 1)[-1])


def _collect_offers(v: object, out: list[JsonLdOffer], depth: int = 0) -> None:
    if depth > 3 or len(out) >= 24:
        return
    if isinstance(v, list):
        for x in v[:24]:
            _collect_offers(x, out, depth + 1)
    elif isinstance(v, dict):
        if isinstance(v.get("offers"), (dict, list)) and not any(k in v for k in ("price", "lowPrice")):
            _collect_offers(v["offers"], out, depth + 1)
        else:
            offer = _offer_of(v)
            if offer.price is not None or offer.valid_until:
                out.append(offer)


def _product_of(node: dict) -> JsonLdProduct:
    p = JsonLdProduct(types=_types_of(node), name=_first_str(node.get("name"), 400), description=_first_str(node.get("description"), 1500),
                      brand=_first_str(node.get("brand"), 200), category=_first_str(node.get("category"), 200), color=_first_str(node.get("color"), 100),
                      material=_first_str(node.get("material"), 100))
    imgs = node.get("image")
    for x in (imgs if isinstance(imgs, list) else [imgs]):
        u = _first_str(x.get("url") if isinstance(x, dict) else x, MAX_IMAGE_URL)
        if u and len(p.images) < 6:
            p.images.append(u)
    _collect_offers(node.get("offers"), p.offers)
    props = node.get("additionalProperty")
    for x in (props if isinstance(props, list) else [props])[:20]:
        if isinstance(x, dict):
            k, v = _first_str(x.get("name"), 80), _first_str(x.get("value"), 160)
            if k and v:
                p.properties.append((k, v))
    return p


def _walk_jsonld(node: object, out_products: list[JsonLdProduct], meta: dict, depth: int = 0, budget: Optional[list[int]] = None) -> None:
    budget = budget if budget is not None else [3000]
    budget[0] -= 1
    if budget[0] < 0 or depth > 6:
        return
    if isinstance(node, list):
        for x in node[:200]:
            _walk_jsonld(x, out_products, meta, depth + 1, budget)
        return
    if not isinstance(node, dict):
        return
    types = [t.lower() for t in _types_of(node)]
    if any(t in _PRODUCT_TYPES for t in types):
        out_products.append(_product_of(node))
        return
    if any(t in _ARTICLE_TYPES for t in types):
        meta["article"] = True
    if "itemlist" in types:
        items = node.get("itemListElement")
        meta["list_items"] = meta.get("list_items", 0) + (len(items) if isinstance(items, list) else 0)
    for k, v in node.items():
        if k.lower() in _SKIP_KEYS or k.startswith("@") and k != "@graph":
            continue
        if isinstance(v, (dict, list)):
            _walk_jsonld(v, out_products, meta, depth + 1, budget)


def _parse_jsonld(blocks: list[str]) -> tuple[list[JsonLdProduct], dict, int]:
    products: list[JsonLdProduct] = []
    meta: dict = {}
    invalid = 0
    for raw in blocks:
        text = raw.strip()
        text = re.sub(r"^\s*(<!--|//<!\[CDATA\[|/\*<!\[CDATA\[\*/)", "", text)
        text = re.sub(r"(-->|//\]\]>|/\*\]\]>\*/)\s*$", "", text).strip()
        if not text:
            continue
        try:
            data = json.loads(text)
        except (ValueError, RecursionError):
            invalid += 1                  # 잘못된 JSON 은 무시한다
            continue
        _walk_jsonld(data, products, meta)
    return products, meta, invalid


# ---------------------------------------------------------------------------
# 9) 읽기 진입점
# ---------------------------------------------------------------------------
_STYLE_OPEN = re.compile(r"<style\b[^>]{0,200}>", re.IGNORECASE)
_STYLE_CLOSE = re.compile(r"</style", re.IGNORECASE)


def _style_blocks(text: str, limit: int = 20) -> list[str]:
    """<style>…</style> 안의 글. 정규식 하나로 찾으면 여는 태그만 많은 입력에서 제곱 시간이 되므로, 앞에서부터 한 번만 훑는다."""
    out: list[str] = []
    pos = 0
    while len(out) < limit:
        m = _STYLE_OPEN.search(text, pos)
        if not m:
            break
        c = _STYLE_CLOSE.search(text, m.end())
        if not c:
            break
        out.append(text[m.end():c.start()])
        pos = c.end()
    return out


def _looks_like_markup(s: str) -> bool:
    return bool(re.search(r"<\s*/?\s*(html|head|body|div|p|span|h[1-6]|meta|title|script|ul|li|a|img|table|br|section|article|main|style)\b", s[:200_000], re.IGNORECASE))


def _text_to_html(s: str) -> str:
    return "<html><body>" + "".join(f"<p>{_escape(line)}</p>" for line in s.splitlines() if line.strip()) + "</body></html>"


def _absolutize(src: str, base: str) -> Optional[str]:
    src = src.strip()
    if not src or src.lower().startswith(("data:", "javascript:", "blob:", "about:")):
        return None
    if src.startswith("//"):
        src = "https:" + src
    elif not re.match(r"^[A-Za-z][A-Za-z0-9+.\-]*:", src):
        if not base or not re.match(r"^https?://", base, re.IGNORECASE):
            return None
        src = urljoin(base, src)
    try:
        return validate_url(src).url          # 주소 형식·사설 IP 리터럴을 여기서도 거른다 (이미지를 나중에 가져오더라도 같은 기준)
    except UrlRejected:
        return None


def parse_product_page(html: str | bytes, url: str = "", *, charset: Optional[str] = None, truncated: bool = False, plain_text_ok: bool = False) -> PageExtract:
    """HTML(또는 바이트)을 읽어 `PageExtract` 로. 표준 `html.parser` 만 쓰며 깨진 HTML 에도 예외를 내지 않는다.
    화면에 보이지 않는 글은 `hidden_text` 로 따로 모으고 사실로 쓰지 않는다. 인젝션으로 보이는 문장은 보이는 본문에서도 빼고 `injection_hits` 에 남긴다."""
    ext = PageExtract(url=clean_text(url, 2048), truncated=truncated)
    try:
        _parse_into(ext, html, url, charset, plain_text_ok)
    except Exception:   # noqa: BLE001 — 어떤 입력에도 예외로 끝나지 않는다. 읽지 못했다고 돌려주고 화면이 안내한다
        ext.parse_error = True
        ext.notes.append("읽는 중 오류가 나 중단했습니다.")
    return ext


def _parse_into(ext: PageExtract, source: str | bytes, url: str, charset: Optional[str], plain_text_ok: bool) -> None:
    if isinstance(source, (bytes, bytearray)):
        text, ext.encoding = decode_html(bytes(source), charset, partial=ext.truncated)
    else:
        text, ext.encoding = str(source), "str"
    if len(text) > MAX_BODY_BYTES:
        text, ext.truncated = text[:MAX_BODY_BYTES], True
    text = text.lstrip("\ufeff")
    if re.search(r"<title\b", text, re.IGNORECASE) and not re.search(r"</title\s*>", text, re.IGNORECASE):
        # 닫는 </title> 이 없으면 파이썬 버전에 따라 문서 전체가 제목으로 읽힌다 — 첫 태그나 줄바꿈에서 제목을 닫아 버전과 상관없이 같게 읽는다
        text = re.sub(r"(<title\b[^>]{0,200}>[^<\n]*)", r"\1</title>", text, count=1, flags=re.IGNORECASE)
    if plain_text_ok and not _looks_like_markup(text):
        text, ext.plain_text = _text_to_html(text), True
    rules: list = []
    for css in _style_blocks(text):
        rules.extend(_parse_stylesheet(css[:MAX_STYLE_BYTES]))
    parser = _PageParser(url, rules)
    try:
        parser.feed(text)
        parser.close()
    except _Stop:
        ext.truncated = True
        ext.notes.append("태그가 너무 많거나 너무 깊게 중첩되어 일부만 읽었습니다.")
    parser.finish()

    base = parser.base_href if re.match(r"^https?://", parser.base_href or "", re.IGNORECASE) else url
    hits: list[InjectionHit] = []
    seen_hits: set[tuple[str, str]] = set()

    def add_hit(where: str, text: str, rules_: list[str]) -> None:
        key = (where, text[:80])
        if key not in seen_hits and len(hits) < 40:
            seen_hits.add(key)
            hits.append(InjectionHit(where=where, text=clean_text(text, 200), rules=rules_))   # type: ignore[arg-type]

    # 메타·제목: 한 번에 검사해서 걸린 것은 쓰지 않는다
    fields: list[tuple[str, str, str, int]] = [("title", "", parser.title, 300), ("h1", "", parser.h1s[0] if parser.h1s else "", 300)]
    fields += [("meta", k, v, 600) for k, vals in parser.meta.items() for v in vals]
    cleaned = [clean_text(v, lim * 4) for _, _, v, lim in fields]
    verdicts = _flag_texts([c for c in cleaned if c])
    it = iter(verdicts)
    og: dict[str, list[str]] = {}
    for (kind, key, _raw, lim), c in zip(fields, cleaned):
        if not c:
            continue
        flag = next(it)
        if flag:
            add_hit("visible" if kind == "h1" else "meta", c, flag)
            continue
        value = clean_text(c, lim)
        if kind == "title":
            ext.title = value
        elif kind == "h1":
            ext.h1 = value
        elif key == "description":
            ext.meta_description = ext.meta_description or value
        else:
            og.setdefault(key, []).append(value)
    ext.og = og

    # JSON-LD
    products, jmeta, invalid = _parse_jsonld(parser.jsonld_raw)
    ext.jsonld_blocks, ext.jsonld_invalid = len(parser.jsonld_raw), invalid
    clean_products: list[JsonLdProduct] = []
    for p in products:
        fields = {"name": p.name, "description": p.description, "brand": p.brand, "category": p.category, "color": p.color, "material": p.material}
        flags = _flag_texts([clean_text(v, 2000) for v in fields.values()] + [clean_text(f"{k}: {v}", 400) for k, v in p.properties])
        keys = list(fields)
        for i, flag in enumerate(flags[: len(keys)]):
            if flag:
                add_hit("jsonld", clean_text(fields[keys[i]], 200), flag)
                setattr(p, keys[i], "")
            else:
                setattr(p, keys[i], clean_text(fields[keys[i]], 1500 if keys[i] == "description" else 400))
        kept_props = []
        for (k, v), flag in zip(p.properties, flags[len(keys):]):
            if flag:
                add_hit("jsonld", f"{k}: {v}", flag)
            else:
                kept_props.append((clean_text(k, 80), clean_text(v, 160)))
        p.properties = kept_props
        clean_products.append(p)
    ext.products = clean_products

    # 숨은 글 · 속성 글: 검사해서 표시만 한다 (사실로는 쓰지 않는다)
    flags_hidden = _flag_texts([h.text for h in parser.hidden])
    for h, flag in zip(parser.hidden, flags_hidden):
        h.text = clean_text(h.text, HIDDEN_SNIPPET_MAX)
        if flag:
            h.injection = True
            add_hit("hidden", h.text, flag)
    ext.hidden_text = [h for h in parser.hidden if h.text]
    for t, flag in zip(parser.attr_texts, _flag_texts(parser.attr_texts)):
        if flag:
            add_hit("attribute", t, flag)
    for t, flag in zip(parser.chrome_lines, _flag_texts(parser.chrome_lines)):
        if flag:
            add_hit("visible", t, flag)

    # 보이는 본문: 문장으로 쪼개고, 인젝션이 걸린 문장은 뺀다
    pieces: list[tuple[str, str]] = []     # (문장, 근거가 되는 줄)
    last = None
    for line in parser.lines:
        if line == last:
            continue
        last = line
        for s in _split_sentences(line):
            pieces.append((s, line))
    flags_body = _flag_texts([s for s, _ in pieces])
    seen_sent: set[str] = set()
    for (s, line), flag in zip(pieces, flags_body):
        s = clean_text(s, FACT_MAX)
        if not s:
            continue
        if flag:
            add_hit("visible", s, flag)
            continue
        key = re.sub(r"\W+", "", s).lower()
        if key and key not in seen_sent:
            seen_sent.add(key)
            ext.sentences.append(Sentence(text=s, evidence=clean_text(line, EVIDENCE_MAX)))
    ext.injection_hits = hits
    ext.visible_chars = parser.visible_chars
    ext.body_prices = parser.body_prices

    # 이미지: og:image 먼저, 그다음 JSON-LD, 본문
    imgs: list[str] = []
    for cand in [*ext.og.get("og:image", []), *ext.og.get("og:image:url", []), *[i for p in ext.products for i in p.images], *parser.images]:
        u = _absolutize(cand, base)
        if u and len(u) <= MAX_IMAGE_URL and u not in imgs and not (cand not in ext.og.get("og:image", []) and _IMG_SKIP.search(u)):
            imgs.append(u)
        if len(imgs) >= MAX_IMAGES:
            break
    ext.image_urls = imgs

    sig = ext.signals = parser.signals
    sig.og_type = (ext.og.get("og:type") or [""])[0].lower()
    sig.article_hint = bool(jmeta.get("article")) or sig.og_type in ("article", "blog", "video.other", "music.song")
    sig.jsonld_list_items = int(jmeta.get("list_items", 0))
    sig.price_cards = parser.cards_done
    body_blob = " ".join(s.text for s in ext.sentences)
    ui_blob = " ".join(parser.ui_lines)
    sig.purchase_words = len(_PURCHASE.findall(body_blob + " " + ui_blob))
    sig.login_words = bool(_LOGIN_WORDS.search(ext.title) or _LOGIN_WORDS.search(ext.h1) or _LOGIN_WORDS.search(body_blob[:600]) or _LOGIN_WORDS.search(ui_blob))
    sig.empty_app_root = bool(parser.app_root_ids) and parser.visible_chars < SPA_MAX_CHARS


# ---------------------------------------------------------------------------
# 10) 사실 초안
# ---------------------------------------------------------------------------
class DraftFlag(str, Enum):
    INJECTION_SUSPECTED = "injection_suspected"
    HIDDEN_TEXT_FOUND = "hidden_text_found"
    PRICE_CONFLICT = "price_conflict"
    TRUNCATED = "truncated"
    AMBIGUOUS_PRODUCT = "ambiguous_product"
    UNREADABLE = "unreadable"
    LOW_CONTENT = "low_content"


class ReadStatus(str, Enum):
    OK = "ok"                    # 상품 하나를 읽었다 (그래도 사실은 후보일 뿐)
    AMBIGUOUS = "ambiguous"      # 여러 상품이라 어느 것인지 모호하다 — 억지로 고르지 않는다
    UNREADABLE = "unreadable"    # 읽지 못했다 (껍데기·로그인 벽·상품이 아닌 글 …)


class DraftFact(BaseModel):
    """페이지에서 읽은 사실 **후보**. `confirmed` 는 언제나 False — 확정은 `confirm()` 이 `Fact` 를 만드는 것으로만 일어난다."""

    model_config = ConfigDict(frozen=True)

    id: str                                   # D1, D2 … (확정 때 F1, F2 … 로 새로 매긴다)
    text: str
    source: Literal["jsonld", "og", "body"]
    kind: Literal["price", "condition", "description", "attribute"] = "description"
    evidence_snippet: str = ""                # 페이지의 어디에서 읽었는지 (쉬운 말 + 원문 발췌)
    confirmed: Literal[False] = False


class RejectedText(BaseModel):
    """쓰지 않은 글 — 사용자에게 무엇을 뺐는지 보여 주기 위한 것. 프롬프트에 넣지 말 것 (신뢰할 수 없는 글)."""

    text: str
    why: Literal["hidden", "instruction"]     # hidden: 보이지 않는 글(지시문 아님) · instruction: AI 에게 내리는 지시처럼 보이는 문장
    where: Literal["hidden", "visible", "meta", "jsonld", "attribute"] = "hidden"


class ProductDraft(BaseModel):
    url: str = ""
    status: ReadStatus
    reason_code: str = ""                     # spa_shell · login_wall · not_product · empty · parse_error · multiple_products
    reason: str = ""                          # 사용자에게 보일 쉬운 말
    name: str = ""
    category_guess: ProductCategory = ProductCategory.OTHER
    needs_category_confirmation: bool = True  # 업종 추정에 확신이 낮으면 True (OTHER + 되묻기)
    price: Optional[PriceInfo] = None
    price_candidates: list[PriceInfo] = Field(default_factory=list)
    facts: list[DraftFact] = Field(default_factory=list)
    landing_text: str = ""
    image_urls: list[str] = Field(default_factory=list)
    flags: list[DraftFlag] = Field(default_factory=list)
    candidates: list[str] = Field(default_factory=list)       # 여러 상품일 때 보인 상품 이름들
    rejected_text: list[RejectedText] = Field(default_factory=list)
    encoding: str = ""

    @property
    def readable(self) -> bool:
        return self.status == ReadStatus.OK


_REGULATED = (ProductCategory.LOAN, ProductCategory.CARD, ProductCategory.INSURANCE, ProductCategory.INVESTMENT, ProductCategory.SAVINGS, ProductCategory.CRYPTO, ProductCategory.HEALTH)
_CATEGORY_CUES: dict[ProductCategory, tuple[str, ...]] = {
    ProductCategory.LOAN: ("대출", "마이너스통장", "한도 조회", "한도조회", "대부", "상환", "중도상환", "신용대출"),
    ProductCategory.CARD: ("신용카드", "체크카드", "카드 발급", "연회비", "카드 혜택"),
    ProductCategory.INSURANCE: ("보험", "보험료", "보험금", "특약", "피보험자"),
    ProductCategory.INVESTMENT: ("펀드", "ETF", "주식", "증권", "수익률", "자산운용", "원금 손실"),
    ProductCategory.SAVINGS: ("적금", "예금", "정기예금", "저축", "우대금리"),
    ProductCategory.CRYPTO: ("가상자산", "암호화폐", "비트코인", "코인"),
    ProductCategory.ECOMMERCE: ("장바구니", "무료배송", "배송비", "택배", "바로구매", "구매하기", "옵션 선택", "교환", "반품", "add to cart", "쿠폰", "재고"),
    ProductCategory.APP: ("앱 다운로드", "앱 설치", "앱스토어", "구글 플레이", "app store", "google play", "구독", "앱으로", "다운로드"),
    ProductCategory.EDUCATION: ("수강", "강의", "커리큘럼", "클래스", "학원", "자격증", "교육과정"),
    ProductCategory.HEALTH: ("건강기능식품", "영양제", "비타민", "프로바이오틱스", "유산균", "오메가", "홍삼", "식약처", "진료", "의료"),
    ProductCategory.REALESTATE: ("분양", "아파트", "부동산", "월세", "전세", "매물", "입주"),
}
_TYPE_CATEGORY = {"creditcard": ProductCategory.CARD, "loanorcredit": ProductCategory.LOAN, "mortgageloan": ProductCategory.LOAN, "financialproduct": ProductCategory.OTHER,
                  "bankaccount": ProductCategory.SAVINGS, "depositaccount": ProductCategory.SAVINGS, "softwareapplication": ProductCategory.APP,
                  "mobileapplication": ProductCategory.APP, "webapplication": ProductCategory.APP, "course": ProductCategory.EDUCATION,
                  "dietarysupplement": ProductCategory.HEALTH, "drug": ProductCategory.HEALTH, "insuranceproduct": ProductCategory.INSURANCE}


def guess_category(ext: PageExtract, name: str = "") -> tuple[ProductCategory, bool, dict[str, int]]:
    """키워드 투표. (추정, 되묻기 필요, 점수). 확신이 낮으면 OTHER + 되묻기.
    규제 업종(금융·건강)은 신호가 어느 정도 있으면 커머스보다 우선한다 — 금융 상품을 커머스로 잘못 읽어 필요한 고지 검사가 빠지는 쪽이 더 위험하다."""
    strong = " ".join([name, ext.title, ext.h1, ext.meta_description, *[v for vals in ext.og.values() for v in vals],
                       *[f"{p.name} {p.description} {p.category} {p.brand}" for p in ext.products]]).lower()
    weak = " ".join(s.text for s in ext.sentences)[:6000].lower()
    scores: dict[ProductCategory, int] = {}
    for cat, cues in _CATEGORY_CUES.items():
        sc = 0
        for cue in cues:
            c = cue.lower()
            sc += min(strong.count(c), 3) * 3 + min(weak.count(c), 3)
        if sc:
            scores[cat] = sc
    if ext.signals.purchase_words:
        scores[ProductCategory.ECOMMERCE] = scores.get(ProductCategory.ECOMMERCE, 0) + 4
    if any(pi.kind in ("sale", "unknown") for pi in ext.body_prices):
        scores[ProductCategory.ECOMMERCE] = scores.get(ProductCategory.ECOMMERCE, 0) + 1
    for p in ext.products:
        for t in p.types:
            cat = _TYPE_CATEGORY.get(t.lower())
            if cat is not None and cat != ProductCategory.OTHER:
                scores[cat] = scores.get(cat, 0) + 6
        if any(o.price is not None for o in p.offers):
            scores[ProductCategory.ECOMMERCE] = scores.get(ProductCategory.ECOMMERCE, 0) + 2
    if not scores:
        return ProductCategory.OTHER, True, {}
    ranked = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0].value))
    best, best_s = ranked[0]
    second_s = ranked[1][1] if len(ranked) > 1 else 0
    reg = [(c, s) for c, s in ranked if c in _REGULATED and s >= 6]
    if reg and best not in _REGULATED and reg[0][1] * 2 >= best_s:
        best, best_s = reg[0]
        second_s = max([s for c, s in ranked if c != best] or [0])
    confident = best_s >= 5 and best_s - second_s >= 3
    labels = {c.value: s for c, s in ranked}
    return (best, False, labels) if confident else (ProductCategory.OTHER, True, labels)


def _strip_site(title: str, site_name: str, h1: str) -> str:
    t = title
    if site_name:
        stripped = re.sub(r"\s*[|｜\-–—:·]\s*" + re.escape(site_name) + r"\s*$", "", t)
        if stripped != t:
            return stripped          # 사이트 이름을 알고 떼어 냈다면 나머지는 상품 이름이다 — 이름 안의 '-' 는 건드리지 않는다
    if h1 and h1 != t and h1 in t and (t.startswith(h1) or t.endswith(h1)):
        return h1
    parts = re.split(r"\s+[|｜–—·]\s+|\s+-\s+", t)
    return parts[0] if len(parts) > 1 and len(parts[0]) >= 2 else t


def _first(values: list[str]) -> str:
    return values[0] if values else ""


def _pick_name(ext: PageExtract) -> str:
    for p in ext.products:
        if p.name:
            return clean_text(p.name, TITLE_MAX)
    site = _first(ext.og.get("og:site_name", []))
    h1 = ext.h1 if (ext.h1 and len(ext.h1) <= 80 and not _WON.search(ext.h1)) else ""    # 닫히지 않은 <h1> 이 본문 전체를 삼킨 경우는 이름으로 쓰지 않는다
    og_title = _first(ext.og.get("og:title", [])) or _first(ext.og.get("twitter:title", []))
    if og_title:
        return clean_text(_strip_site(og_title, site, h1), TITLE_MAX)
    if h1:
        return clean_text(h1, TITLE_MAX)
    return clean_text(_strip_site(ext.title, site, h1), TITLE_MAX) if ext.title else ""


def _structured_prices(ext: PageExtract) -> tuple[list[PriceInfo], Optional[PriceInfo]]:
    """(JSON-LD 가격들, 공유 미리보기(og) 가격)."""
    js: list[PriceInfo] = []
    for p in ext.products:
        for o in p.offers:
            for pi in (o.price, o.high_price):
                if pi is not None and not any(x.text == pi.text for x in js):
                    js.append(pi)
    raw = _first(ext.og.get("product:price:amount", []) or ext.og.get("og:price:amount", []) or ext.og.get("product:sale_price:amount", []))
    cur = _first(ext.og.get("product:price:currency", []) or ext.og.get("og:price:currency", []) or ext.og.get("product:sale_price:currency", []))
    og_p = _price_from_value(raw, cur or "KRW", "og", "sale") if raw else None
    return js, og_p


def _decide_price(ext: PageExtract) -> tuple[Optional[PriceInfo], list[PriceInfo], bool]:
    """(대표 가격, 후보들, 가격 충돌 여부). 서로 다른 값이 나오면 하나를 고르지 않고 대표 가격을 비운다 — 가격은 광고에 쓰인다."""
    js, og_p = _structured_prices(ext)
    body = ext.body_prices
    body_amounts = {p.amount for p in body if p.kind in ("sale", "unknown", "list", "conditional") and p.amount}
    list_prices = sorted({p.amount for p in body if p.kind == "list" and p.amount})
    cands: list[PriceInfo] = []

    def add(pi: Optional[PriceInfo]) -> None:
        if pi is not None and not any(c.text == pi.text and c.kind == pi.kind for c in cands) and len(cands) < 6:
            cands.append(pi)

    if js or og_p:
        krw_js = [p for p in js if p.amount]
        if len({p.amount for p in krw_js}) >= 2:                          # 옵션별로 값이 다른 상품: 범위를 보여 주고 하나를 고르지 않는다
            for p in krw_js:
                add(p)
            return None, cands, False
        struct = (krw_js[0] if krw_js else None) or (js[0] if js else None) or og_p
        assert struct is not None
        add(struct)
        conflict = False
        if og_p is not None and struct.amount and og_p.amount and og_p.amount != struct.amount:
            add(og_p)
            conflict = True
        if struct.amount and body_amounts and struct.amount not in body_amounts:
            conflict = True
            for p in body:
                if p.kind in ("sale", "unknown") and p.amount:
                    add(p)
                    break
        if conflict:
            return None, cands, True
        for a in list_prices:
            if struct.amount and a > struct.amount:
                add(PriceInfo(amount=a, text=_fmt_krw(a), source="body", kind="list"))
        return struct, cands, False

    sale = [p for p in body if p.kind == "sale"]
    unknown = [p for p in body if p.kind == "unknown"]
    pool = sale or unknown
    distinct = []
    for p in pool:
        if p.amount not in [d.amount for d in distinct]:
            distinct.append(p)
    for p in distinct:
        add(p)
    for a in list_prices:
        add(PriceInfo(amount=a, text=_fmt_krw(a), source="body", kind="list"))
    if len(distinct) == 1:
        return distinct[0], cands, False
    return None, cands, False


def _is_fact_like(s: str) -> bool:
    return 6 <= len(s) <= FACT_MAX and bool(_FACT_CUE.search(s)) and not _BOILER.search(s)


_PRICE_WORDS = re.compile(r"정가|소비자가|정상가|원가|시중가|할인가|특가|세일가|혜택가|판매가|최종가|가격|price|sale|할인율|할인|\d+\s*%|[()\[\]:：·|,/→>\-\s]+", re.IGNORECASE)
_DISCOUNT = re.compile(r"\d{1,2}\s*%\s*할인")


def _is_price_only(s: str) -> bool:
    """가격과 가격 이름표만 있는 문장 (이미 가격 후보로 따로 올라간다)."""
    rest = _MAN.sub("", s)
    rest = _CHEON.sub("", rest)
    rest = _WON.sub("", rest)
    return len(_PRICE_WORDS.sub("", rest)) <= 1 and rest != s


def _norm_key(s: str) -> str:
    return re.sub(r"[\W_]+", "", unicodedata.normalize("NFKC", s)).lower()


def _price_label(p: PriceInfo) -> str:
    return {"sale": "판매가", "list": "정가", "conditional": "조건부 가격", "other": "가격"}.get(p.kind, "가격")


_SRC_PLAIN = {"jsonld": "상품 정보 데이터", "og": "페이지 소개 정보", "body": "본문"}


def _build_facts(ext: PageExtract, price: Optional[PriceInfo], price_cands: list[PriceInfo], name: str = "") -> list[DraftFact]:
    out: list[tuple[str, str, str, str]] = []     # (text, source, kind, evidence)
    keys: list[str] = []

    def add(text: str, source: str, kind: str, evidence: str) -> None:
        text = clean_text(text, FACT_MAX)
        key = _norm_key(text)
        if not text or not key or len(out) >= MAX_FACTS:
            return
        if any(key == k or (len(key) >= 12 and (key in k)) for k in keys):
            return
        keys.append(key)
        out.append((text, source, kind, clean_text(evidence, EVIDENCE_MAX)))

    for p in price_cands:
        src = "jsonld" if p.source == "jsonld" else ("og" if p.source == "og" else "body")
        where = {"jsonld": "상품 정보 데이터에 적힌 가격", "og": "페이지 소개 정보에 적힌 가격", "body": "본문에서 읽은 가격"}[src]
        add(f"{_price_label(p)} {p.text}", src, "price", f"[{_SRC_PLAIN[src]}] {where}: {p.text}")
    for p in ext.products[:1]:
        if p.brand:
            add(f"브랜드: {p.brand}", "jsonld", "attribute", f"[상품 정보 데이터] 브랜드: {p.brand}")
        if p.color:
            add(f"색상: {p.color}", "jsonld", "attribute", f"[상품 정보 데이터] 색상: {p.color}")
        if p.material:
            add(f"소재: {p.material}", "jsonld", "attribute", f"[상품 정보 데이터] 소재: {p.material}")
        for k, v in p.properties[:8]:
            add(f"{k}: {v}", "jsonld", "attribute", f"[상품 정보 데이터] {k}: {v}")
        for o in p.offers[:1]:
            if o.valid_until:
                add(f"가격 유효 기한: {o.valid_until}", "jsonld", "condition", f"[상품 정보 데이터] 가격 유효 기한: {o.valid_until}")
        for s in _split_sentences(p.description)[:4]:
            add(s, "jsonld", "condition" if _COND_CUE.search(s) else "description", f"[상품 정보 데이터] 상품 설명: {p.description}")
    og_desc = _first(ext.og.get("og:description", [])) or _first(ext.og.get("twitter:description", []))
    for src_text, label in ((og_desc, "페이지 소개 설명"), (ext.meta_description, "페이지 요약")):
        for s in _split_sentences(src_text)[:3]:
            add(s, "og", "condition" if _COND_CUE.search(s) else "description", f"[페이지 소개 정보] {label}: {src_text}")
    body_added = 0
    own_names = {_norm_key(x) for x in (name, ext.h1, ext.title, *(p.name for p in ext.products)) if x}
    for sent in ext.sentences:
        if body_added >= MAX_BODY_FACTS:
            break
        if _norm_key(sent.text) in own_names:
            continue
        if _is_price_only(sent.text):
            m = _DISCOUNT.search(sent.text)       # 가격은 가격 후보로 올라가 있다. 문장에 든 할인율만 따로 후보로 둔다
            if m:
                add(m.group(0), "body", "description", f"[본문] {sent.evidence}")
            continue
        if _is_fact_like(sent.text):
            before = len(out)
            add(sent.text, "body", "condition" if _COND_CUE.search(sent.text) else "description", f"[본문] {sent.evidence}")
            body_added += len(out) - before
    if len(out) < 3:                              # 단서 낱말이 없는 설명형 본문: 앞쪽 문장 몇 개만이라도 후보로 둔다
        for sent in ext.sentences:
            if len(out) >= 6:
                break
            if 10 <= len(sent.text) <= FACT_MAX and not _BOILER.search(sent.text):
                add(sent.text, "body", "description", f"[본문] {sent.evidence}")
    # 마지막 방어선: 어떤 경로로 들어왔든 인젝션으로 보이는 후보는 한 번 더 걸러 낸다
    verdicts = _flag_texts([t for t, *_ in out])
    facts: list[DraftFact] = []
    for (text, source, kind, evidence), flag in zip(out, verdicts):
        if flag:
            continue
        facts.append(DraftFact(id=f"D{len(facts) + 1}", text=text, source=source, kind=kind, evidence_snippet=evidence))   # type: ignore[arg-type]
    return facts


def _landing_text(ext: PageExtract, facts: list[DraftFact]) -> str:
    parts: list[str] = []
    total = 0
    for s in ext.sentences:
        if _BOILER.search(s.text):
            continue
        if total + len(s.text) + 1 > LANDING_MAX:
            break
        parts.append(s.text)
        total += len(s.text) + 1
    if not parts:
        for f in facts:
            if f.kind != "price" and total + len(f.text) + 1 <= LANDING_MAX:
                parts.append(f.text)
                total += len(f.text) + 1
    return "\n".join(parts)


_READ_MSG = {
    "spa_shell": "이 페이지는 브라우저에서 그려지는 방식이라 본문을 읽지 못했습니다. 상세 설명을 직접 붙여 넣어 주세요.",
    "empty": "읽을 수 있는 글이 거의 없는 페이지입니다. 상세 설명을 직접 붙여 넣어 주세요.",
    "login_wall": "로그인이 있어야 내용이 보이는 페이지입니다. 상세 설명을 직접 붙여 넣어 주세요.",
    "not_product": "상품 페이지가 아닌 것 같아 읽지 않았습니다 (후기·소개 글 등). 상품 설명을 직접 붙여 넣어 주세요.",
    "parse_error": "페이지를 읽는 중 문제가 생겼습니다. 상세 설명을 직접 붙여 넣어 주세요.",
    "multiple_products": "상품이 여러 개 들어 있는 페이지라 어느 상품인지 정할 수 없습니다. 상품 하나만 있는 페이지 주소를 쓰거나, 그 상품의 설명을 직접 붙여 넣어 주세요.",
}


def _judge(ext: PageExtract) -> tuple[ReadStatus, str, list[str]]:
    """(상태, 이유 코드, 후보 이름들). 억지로 읽지 않는다: 껍데기·로그인 벽·여러 상품·상품이 아닌 글은 사실을 만들지 않는다."""
    sig = ext.signals
    if ext.parse_error:
        return ReadStatus.UNREADABLE, "parse_error", []
    js, og_p = _structured_prices(ext)
    og_type = sig.og_type
    has_product_data = bool(ext.products) or og_type.startswith(("product", "og:product")) or bool(js) or og_p is not None
    og_summary = bool(ext.og.get("og:title") and (ext.og.get("og:description") or ext.og.get("og:image")))
    body_price = any(p.kind in ("sale", "unknown", "list") for p in ext.body_prices)
    if ext.plain_text:
        return (ReadStatus.OK, "", []) if ext.sentences else (ReadStatus.UNREADABLE, "empty", [])
    no_structure = not has_product_data and not og_summary
    if (sig.has_password_input or (sig.login_words and ext.visible_chars < 500)) and no_structure and not body_price:
        return ReadStatus.UNREADABLE, "login_wall", []
    names = []
    for p in ext.products:
        n = clean_text(p.name, TITLE_MAX)
        if n and _norm_key(n) not in [_norm_key(x) for x in names]:
            names.append(n)
    if len(names) >= 2 or sig.jsonld_list_items >= 2 or (sig.price_cards >= 3 and not ext.products and not og_type.startswith("product")):
        return ReadStatus.AMBIGUOUS, "multiple_products", names[:8]
    if ext.visible_chars < SPA_MAX_CHARS and no_structure:
        return ReadStatus.UNREADABLE, ("spa_shell" if (sig.script_count or sig.empty_app_root) else "empty"), []
    score = 0
    score += 3 if ext.products else 0
    score += 2 if og_type.startswith("product") else 0
    score += 2 if (js or og_p is not None) else 0
    score += 1 if body_price else 0
    score += 1 if sig.purchase_words else 0
    score -= 3 if sig.article_hint else 0
    if score < 2:         # 가격·구매 버튼이 없는 금융·서비스 상품 소개: 규제 업종 단서가 뚜렷하면 상품 페이지로 본다
        _cat, _ask, cat_scores = guess_category(ext, _pick_name(ext))
        if any(cat_scores.get(c.value, 0) >= 8 for c in _REGULATED if c != ProductCategory.HEALTH):
            score += 2
    if score < 2:
        return ReadStatus.UNREADABLE, "not_product", []
    return ReadStatus.OK, "", []


def to_draft(ext: PageExtract) -> ProductDraft:
    """읽은 결과 → 사실 초안. 모든 사실은 `confirmed=False` 후보이고, 읽지 못했거나 모호하면 사실을 만들지 않는다."""
    status, code, candidates = _judge(ext)
    flags: set[DraftFlag] = set()
    if ext.injection_hits:
        flags.add(DraftFlag.INJECTION_SUSPECTED)
    meaningful = [h for h in ext.hidden_text if h.injection or (len(re.sub(r"\s", "", h.text)) >= MIN_HIDDEN_CHARS_FOR_FLAG and not (h.reason == "noscript" and _JS_NOTICE.search(h.text)))]
    if meaningful:
        flags.add(DraftFlag.HIDDEN_TEXT_FOUND)
    if ext.truncated:
        flags.add(DraftFlag.TRUNCATED)
    rejected: list[RejectedText] = []
    for h in meaningful:
        rejected.append(RejectedText(text=clean_text(h.text, 160), why="instruction" if h.injection else "hidden", where="hidden"))
    for hit in ext.injection_hits:
        if hit.where != "hidden" and hit.text:
            rejected.append(RejectedText(text=clean_text(hit.text, 160), why="instruction", where=hit.where))
    rejected = rejected[:8]
    base = dict(url=ext.url, encoding=ext.encoding, rejected_text=rejected)
    if status != ReadStatus.OK:
        flags.add(DraftFlag.AMBIGUOUS_PRODUCT if status == ReadStatus.AMBIGUOUS else DraftFlag.UNREADABLE)
        return ProductDraft(status=status, reason_code=code, reason=_READ_MSG.get(code, ""), candidates=candidates, flags=_ordered(flags), **base)   # type: ignore[arg-type]
    name = _pick_name(ext)
    price, cands, conflict = _decide_price(ext)
    if conflict:
        flags.add(DraftFlag.PRICE_CONFLICT)
    if ext.visible_chars < LOW_CONTENT_CHARS:
        flags.add(DraftFlag.LOW_CONTENT)
    cat, ask, _scores = guess_category(ext, name)
    facts = _build_facts(ext, price, cands, name)
    return ProductDraft(status=ReadStatus.OK, name=name, category_guess=cat, needs_category_confirmation=ask, price=price, price_candidates=cands, facts=facts,
                        landing_text=_landing_text(ext, facts), image_urls=list(ext.image_urls), flags=_ordered(flags), **base)   # type: ignore[arg-type]


def _ordered(flags: set[DraftFlag]) -> list[DraftFlag]:
    return [f for f in DraftFlag if f in flags]


# ---------------------------------------------------------------------------
# 11) 확정 — 초안이 Fact 가 되는 유일한 경로
# ---------------------------------------------------------------------------
def confirm(draft: ProductDraft, selected_ids: Iterable[str], edited_texts: Optional[dict[str, str]] = None, *,
            name: Optional[str] = None, category: Optional[ProductCategory] = None, landing_text: Optional[str] = None) -> ProductFacts:
    """사용자가 고른 후보만 `Fact`(F1, F2 …)로 만든다. 이 함수 밖에서는 초안이 사실이 되지 않는다.

    - 읽기 가능(OK) 상태가 아닌 초안, 선택이 없음, 알 수 없는 id 는 거부한다.
    - 고친 글(`edited_texts`)도 정리·길이 상한·인젝션 검사를 다시 거친다 (초안 객체를 직접 만들거나 화면 상태가 바뀐 경우에도 새어 들어가지 않게).
    - 업종 추정에 확신이 없던 초안(`needs_category_confirmation`)은 사용자가 업종을 직접 정해야 한다 (`category=`).
    - 랜딩 문구는 읽은 본문 요약을 쓰되 사용자가 고친 글(`landing_text=`)이 있으면 그것을 쓴다. 어느 쪽이든 정리·길이 상한·지시문 줄 제거를 다시 거친다.
    """
    if draft.status != ReadStatus.OK:
        raise DraftError("읽지 못했거나 모호한 초안은 사실로 확정할 수 없습니다.")
    edited_texts = edited_texts or {}
    wanted = list(dict.fromkeys(selected_ids))
    if not wanted:
        raise DraftError("선택한 사실이 없습니다.")
    by_id = {f.id: f for f in draft.facts}
    unknown = [i for i in wanted if i not in by_id]
    if unknown:
        raise DraftError("초안에 없는 항목을 골랐습니다.")
    if category is None and draft.needs_category_confirmation:
        raise DraftError("업종을 직접 골라 확인해 주세요.")
    texts: list[str] = []
    for fid in (f.id for f in draft.facts if f.id in wanted):
        raw = edited_texts.get(fid, by_id[fid].text)
        text = clean_text(raw, FACT_MAX + 1)
        if not text:
            raise DraftError("비어 있는 사실은 쓸 수 없습니다.")
        if len(text) > FACT_MAX:
            raise DraftError(f"사실 한 문장은 {FACT_MAX}자 이하로 줄여 주세요.")
        texts.append(text)
    for text, flag in zip(texts, _flag_texts(texts)):
        if flag:
            raise DraftError("AI 에게 내리는 지시처럼 보이는 문장은 사실로 쓸 수 없습니다.")
    final_name = clean_text(name if name is not None else draft.name, TITLE_MAX)
    if not final_name:
        raise DraftError("상품 이름이 필요합니다.")
    if _flag_texts([final_name])[0]:
        raise DraftError("상품 이름에 AI 에게 내리는 지시처럼 보이는 문장이 있습니다.")
    cat = category if category is not None else draft.category_guess
    landing_src = draft.landing_text if landing_text is None else landing_text
    landing_lines = clean_block(landing_src, LANDING_MAX).split("\n") if landing_src else []
    landing = "\n".join(line for line, flag in zip(landing_lines, _flag_texts(landing_lines)) if not flag)      # 어떤 경로로 들어왔든 지시문 줄은 뺀다
    facts = [Fact(id=f"F{i}", text=t) for i, t in enumerate(texts, start=1)]
    return ProductFacts(name=final_name, category=cat, facts=facts, landing_url=draft.url if re.match(r"^https?://", draft.url or "", re.IGNORECASE) else None,
                        raw_description=landing)


# ---------------------------------------------------------------------------
# 12) 한 번에
# ---------------------------------------------------------------------------
def ingest_html(source: str | bytes, url: str = "", *, charset: Optional[str] = None, truncated: bool = False, plain_text_ok: bool = False) -> ProductDraft:
    return to_draft(parse_product_page(source, url, charset=charset, truncated=truncated, plain_text_ok=plain_text_ok))


def ingest_url(url: str, fetcher: Optional[Fetcher] = None) -> ProductDraft:
    """주소 → 가져오기 → 읽기 → 사실 초안. 가져오기에 실패하면 `FetchError`(화면용 쉬운 말 `user_message` 포함)."""
    res = fetch_page(url, fetcher)
    return ingest_html(res.body, res.url, charset=res.charset, truncated=res.truncated)
