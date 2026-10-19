"""상품 URL 가져오기 — 주소 검사(SSRF)·이름 확인·연결·리다이렉트·응답 제한.

실제 소켓은 한 번도 열지 않는다: 이름 확인기와 연결기를 가짜로 넣고, '연결 시도 자체가 일어나지 않았음' 을 기록으로 확인한다.
autouse 픽스처가 실제 socket 연결·이름 확인을 막아 두므로, 코드가 가짜를 우회해 진짜 소켓을 쓰면 테스트가 실패한다.
"""
from __future__ import annotations

import gzip
import io
import socket
import subprocess
import sys
import zlib
from pathlib import Path

import pytest

from core import config
from core import url_ingest as ui

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _no_real_network(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("실제 네트워크를 열면 안 됩니다")

    monkeypatch.setattr(socket, "create_connection", boom)
    monkeypatch.setattr(socket, "getaddrinfo", boom)
    monkeypatch.setattr(socket.socket, "connect", boom)


# --- 가짜 이름 확인기 · 연결기 -----------------------------------------------------------
class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


class FakeSocket:
    def __init__(self, response: bytes, clock: Clock | None = None, delay: float = 0.0, chunk: int = 4096):
        self.buf = io.BytesIO(response)
        self.sent = bytearray()
        self.closed = False
        self.clock, self.delay, self.chunk = clock, delay, chunk
        self.timeouts: list[float] = []

    def sendall(self, data: bytes) -> None:
        self.sent += data

    def recv(self, n: int) -> bytes:
        if self.clock is not None:
            self.clock.t += self.delay
        return self.buf.read(min(n, self.chunk))

    def settimeout(self, t) -> None:
        self.timeouts.append(t)

    def close(self) -> None:
        self.closed = True


def http(body: bytes = b"<html><body>ok</body></html>", status: int = 200, ctype: str | None = "text/html; charset=utf-8", headers: dict | None = None,
         encoding: str | None = None, chunked: bool = False) -> bytes:
    head = [f"HTTP/1.1 {status} X"]
    if ctype is not None:
        head.append(f"Content-Type: {ctype}")
    if encoding:
        head.append(f"Content-Encoding: {encoding}")
    for k, v in (headers or {}).items():
        head.append(f"{k}: {v}")
    head.append("Connection: close")
    if chunked:
        head.append("Transfer-Encoding: chunked")
        payload = b"".join(f"{len(body[i:i + 1000]):x}\r\n".encode() + body[i:i + 1000] + b"\r\n" for i in range(0, len(body), 1000)) + b"0\r\n\r\n"
    else:
        head.append(f"Content-Length: {len(body)}")
        payload = body
    return ("\r\n".join(head) + "\r\n\r\n").encode() + payload


def redirect(location: str, status: int = 302) -> bytes:
    return http(b"", status=status, ctype=None, headers={"Location": location})


class Net:
    """dns: 호스트 → 답(목록 또는 호출마다 다른 답을 주는 함수). script: 연결 순서대로 돌려줄 응답 바이트."""

    def __init__(self, dns: dict, script: list[bytes], clock: Clock | None = None, delay: float = 0.0):
        self.dns, self.script = dns, list(script)
        self.resolved: list[tuple[str, int]] = []
        self.connects: list[dict] = []
        self.sockets: list[FakeSocket] = []
        self.clock, self.delay = clock, delay

    def resolver(self, host: str, port: int):
        self.resolved.append((host, port))
        ans = self.dns[host]
        return ans(len(self.resolved)) if callable(ans) else list(ans)

    def connector(self, ip: str, port: int, *, tls: bool, server_hostname: str, timeout: float):
        self.connects.append({"ip": ip, "port": port, "tls": tls, "sni": server_hostname, "timeout": timeout})
        sock = FakeSocket(self.script.pop(0), self.clock, self.delay)
        self.sockets.append(sock)
        return sock

    def fetcher(self, **kw) -> ui.HttpFetcher:
        return ui.HttpFetcher(resolver=self.resolver, connector=self.connector, clock=self.clock, **kw)


PUBLIC = "93.184.216.34"


# =============================================================================
# 1) 주소 검사
# =============================================================================
BAD_URLS = [
    "http://127.0.0.1/", "http://127.1/", "http://2130706433/", "http://0x7f.0.0.1/", "http://0x7f000001/", "http://017700000001/", "http://0177.0.0.1/", "http://0/",
    "http://0.0.0.0/", "http://[::1]/", "http://[0:0:0:0:0:0:0:1]/", "http://[::]/", "http://[::ffff:127.0.0.1]/", "http://[::ffff:7f00:1]/", "http://[::127.0.0.1]/",
    "http://[64:ff9b::7f00:1]/", "http://[2002:7f00:1::]/", "http://[fe80::1]/", "http://[fd00::1]/", "http://[ff02::1]/", "http://[2001:db8::1]/",
    "http://169.254.1.1/", "http://[fe80::1ff:fe23:4567:890a]/", "http://10.0.0.1/", "http://172.16.5.4/", "http://172.31.255.255/", "http://192.168.0.1/", "http://169.254.169.254/latest/meta-data/", "http://100.64.0.1/", "http://100.127.255.254/",
    "http://224.0.0.1/", "http://240.0.0.1/", "http://255.255.255.255/", "http://192.0.2.1/", "http://198.18.0.1/",
    "http://localhost/", "http://LOCALHOST/", "http://localhost./", "http://foo.localhost/", "http://printer.local/", "http://db.internal/", "http://metadata.google.internal/",
    "http://metadata/", "http://intranet/", "http://router.lan/", "http://svc.cluster.local/",
    "http://user:pass@example.com/", "http://user@example.com/", "http://example.com:8080/", "http://example.com:22/", "http://example.com:0/", "http://example.com:99999/",
    "ftp://example.com/", "file:///etc/passwd", "gopher://example.com/", "javascript:alert(1)", "data:text/html,hi", "//example.com/", "example.com",
    "http://exa mple.com/", "http://example.com/a b", "http://example.com/\n", "http://example.com/\x00", " http://example.com/", "http://example.com/ ",
    "http://example.com\\@127.0.0.1/", "http://１２７.０.０.１/", "http://127。0。0。1/", "http://%31%32%37.0.0.1/", "http://1.2.3.4.5/", "http://256.1.1.1/", "http://[fe80::1%25eth0]/",
    "http://[::1", "http://::1/", "http://", "http:///x", "http://" + "a" * 2100 + ".com/",
    "http://①②⑦.0.0.1/", "http://ｌｏｃａｌｈｏｓｔ/", "http://０x7f.0.0.1/", "http://[::ffff:127.0.0.1]:80/", "http://0x7f.1/", "http://0300.0250.0.1/",
]


@pytest.mark.parametrize("url", BAD_URLS)
def test_validate_url_rejects_dangerous_or_malformed_addresses(url):
    with pytest.raises(ui.UrlRejected) as ei:
        ui.validate_url(url)
    assert ei.value.user_message            # 화면에 보일 쉬운 말이 있다


GOOD_URLS = {
    "https://example.com/a/b?x=1#frag": "https://example.com/a/b?x=1",
    "http://Example.COM:80/": "http://example.com/",
    "https://shop.co.kr/상품/123?q=한글": "https://shop.co.kr/%EC%83%81%ED%92%88/123?q=%ED%95%9C%EA%B8%80",
    "https://[2606:4700::1111]/": "https://[2606:4700::1111]/",
    "https://8.8.8.8:443/": "https://8.8.8.8/",
    "https://상점.한국/": "https://xn--hg4bs7k.xn--3e0b707e/",
    "https://example.com": "https://example.com/",
    "https://example.com?a=1": "https://example.com/?a=1",
    "http://example.com:/@x": "http://example.com/@x",
}


@pytest.mark.parametrize("url,normalized", list(GOOD_URLS.items()))
def test_validate_url_accepts_public_addresses_and_normalizes(url, normalized):
    assert ui.validate_url(url).url == normalized


@pytest.mark.parametrize("url,code", [
    ("http://user:pass@example.com/", "userinfo"), ("http://user@example.com/", "userinfo"), ("http://example.com@127.0.0.1/", "userinfo"),
    ("http://example.com\\@127.0.0.1/", "backslash"), ("http://example.com\\x/", "backslash"), ("ftp://example.com/", "scheme"), ("javascript:alert(1)", "scheme"),
    ("http://example.com:8080/", "port"), ("http://example.com/a b", "control"), ("http://127.0.0.1/", "private_ip"), ("http://169.254.169.254/", "private_ip"),
    ("http://localhost/", "blocked_host"), ("http://intranet/", "blocked_host"), ("http://x.internal/", "blocked_host"), ("", "empty"),
])
def test_each_rejection_names_its_own_reason_so_one_check_cannot_hide_behind_another(url, code):
    with pytest.raises(ui.UrlRejected) as ei:
        ui.validate_url(url)
    assert ei.value.code == code


def test_decimal_hex_octal_and_short_ipv4_forms_all_read_as_loopback():
    for form in ("2130706433", "0x7f.0.0.1", "0x7f000001", "017700000001", "0177.0.0.1", "127.1", "127.0.1", "0177.0.0.01"):
        assert str(ui.parse_ipv4_loose(form)) == "127.0.0.1", form
    with pytest.raises(ValueError):
        ui.parse_ipv4_loose("1.2.3.4.5")
    with pytest.raises(ValueError):
        ui.parse_ipv4_loose("999.1.1.1")


def test_request_target_never_contains_whitespace_or_control_characters():
    for url in ("https://example.com/a%20b?q=한글 값".replace(" 값", "값"), "https://example.com/'\"<>`{}|^", "https://example.com/?a=b&c=%0d%0aSet-Cookie:x", "https://example.com/\u202e\u200b"):
        try:
            target = ui.validate_url(url).target
        except ui.UrlRejected:
            continue
        assert target.isascii() and not any(c in target for c in " \r\n\t\x00") and target.startswith("/"), (url, target)


def test_url_length_limit_is_2048_characters():
    ok = "https://example.com/" + "a" * (2048 - len("https://example.com/"))
    assert len(ok) == 2048 and ui.validate_url(ok)
    with pytest.raises(ui.UrlRejected):
        ui.validate_url(ok + "a")


def test_only_ports_80_and_443_are_allowed():
    assert ui.validate_url("http://example.com:443/").port == 443
    assert ui.validate_url("https://example.com:80/").port == 80
    for port in (21, 22, 25, 3306, 6379, 8080, 8443, 9200):
        with pytest.raises(ui.UrlRejected):
            ui.validate_url(f"http://example.com:{port}/")


@pytest.mark.parametrize("addr", ["10.1.2.3", "127.0.0.1", "169.254.169.254", "169.254.1.1", "100.64.9.9", "224.0.0.251", "0.0.0.0", "192.168.1.1", "172.20.0.1", "::1", "fe80::1", "fc00::1",
                                 "::ffff:10.0.0.1", "::ffff:169.254.169.254", "64:ff9b::a00:1", "2002:a00:1::", "2001:db8::2", "ff02::1"])
def test_every_blocked_address_class_is_rejected_after_name_resolution(addr):
    net = Net({"shop.example.com": [addr]}, [])
    with pytest.raises(ui.UrlRejected):
        net.fetcher().fetch("https://shop.example.com/")
    assert net.connects == []                       # 연결을 시도하지 않았다


def test_a_single_private_answer_among_public_ones_rejects_the_whole_name():
    net = Net({"shop.example.com": [PUBLIC, "10.0.0.7"]}, [])
    with pytest.raises(ui.UrlRejected):
        net.fetcher().fetch("https://shop.example.com/")
    assert net.connects == [] and len(net.resolved) == 1


def test_resolution_with_no_answer_or_error_fails_without_connecting():
    net = Net({"shop.example.com": []}, [])
    with pytest.raises(ui.FetchError) as ei:
        net.fetcher().fetch("https://shop.example.com/")
    assert ei.value.code == "dns" and net.connects == []

    def failing(host, port):
        raise socket.gaierror("no such host")

    f = ui.HttpFetcher(resolver=failing, connector=lambda *a, **k: pytest.fail("연결하면 안 됨"))
    with pytest.raises(ui.FetchError) as ei:
        f.fetch("https://nope.example.com/")
    assert ei.value.code == "dns"


def test_ip_literal_urls_are_judged_without_any_name_resolution():
    net = Net({}, [])
    with pytest.raises(ui.UrlRejected):
        net.fetcher().fetch("http://[::ffff:10.0.0.1]/")
    assert net.resolved == [] and net.connects == []


def test_rebinding_is_blocked_by_connecting_to_the_checked_ip_without_resolving_again():
    """첫 이름 확인은 공개 주소, 이후에 물으면 루프백을 주는 서버(DNS 리바인딩). 확인한 IP 에만 연결하고 다시 묻지 않아야 한다."""
    net = Net({"shop.example.com": lambda n: [PUBLIC] if n == 1 else ["127.0.0.1"]}, [http()])
    res = net.fetcher().fetch("https://shop.example.com/p/1")
    assert net.connects[0]["ip"] == PUBLIC                      # 연결 대상은 검사한 IP
    assert net.resolved == [("shop.example.com", 443)]          # 연결 시점에 이름을 다시 확인하지 않았다
    assert res.remote_ip == PUBLIC and res.status == 200


def test_connect_uses_original_host_for_sni_and_host_header_not_the_ip():
    net = Net({"shop.example.com": [PUBLIC]}, [http()])
    net.fetcher().fetch("https://shop.example.com/p/1?x=한글")
    c = net.connects[0]
    assert c["ip"] == PUBLIC and c["tls"] is True and c["sni"] == "shop.example.com" and c["port"] == 443
    sent = bytes(net.sockets[0].sent).decode("ascii")
    assert sent.startswith("GET /p/1?x=%ED%95%9C%EA%B8%80 HTTP/1.1\r\n")
    assert "Host: shop.example.com\r\n" in sent and PUBLIC not in sent


def test_request_has_fixed_user_agent_and_no_cookie_or_auth_headers():
    net = Net({"shop.example.com": [PUBLIC]}, [http()])
    net.fetcher().fetch("http://shop.example.com/")
    sent = bytes(net.sockets[0].sent).decode("ascii").lower()
    assert f"user-agent: {ui.USER_AGENT.lower()}" in sent
    for forbidden in ("\r\ncookie:", "\r\nauthorization:", "\r\nproxy-authorization:", "\r\nx-api-key"):
        assert forbidden not in sent
    assert "accept-encoding: gzip, deflate" in sent


def test_set_cookie_from_a_response_is_never_sent_back_on_redirect():
    net = Net({"a.example.com": [PUBLIC], "b.example.com": [PUBLIC]},
              [http(b"", status=302, ctype=None, headers={"Location": "https://b.example.com/x", "Set-Cookie": "sid=SECRET"}), http()])
    net.fetcher().fetch("https://a.example.com/")
    second = bytes(net.sockets[1].sent).lower()
    assert b"secret" not in second and b"\r\ncookie:" not in second


# --- 리다이렉트 ------------------------------------------------------------------------
def test_redirect_target_is_resolved_and_checked_again_before_any_second_connection():
    net = Net({"a.example.com": [PUBLIC], "b.example.com": ["10.9.9.9"]}, [redirect("https://b.example.com/admin")])
    with pytest.raises(ui.UrlRejected):
        net.fetcher().fetch("https://a.example.com/")
    assert [c["ip"] for c in net.connects] == [PUBLIC]          # 첫 번째만 연결, 두 번째는 연결 시도 없음
    assert [h for h, _ in net.resolved] == ["a.example.com", "b.example.com"]


def test_redirect_to_a_private_ip_literal_is_rejected_by_address_check_before_resolving():
    for target in ("http://169.254.169.254/latest/meta-data/", "http://127.0.0.1:80/", "http://[::1]/", "http://2130706433/", "//localhost/x"):
        net = Net({"a.example.com": [PUBLIC]}, [redirect(target)])
        with pytest.raises(ui.UrlRejected):
            net.fetcher().fetch("http://a.example.com/")
        assert len(net.connects) == 1 and len(net.resolved) == 1


def test_redirect_to_a_non_http_scheme_is_rejected():
    for target in ("file:///etc/passwd", "ftp://a.example.com/x", "gopher://a.example.com/"):
        net = Net({"a.example.com": [PUBLIC]}, [redirect(target)])
        with pytest.raises(ui.UrlRejected):
            net.fetcher().fetch("http://a.example.com/")
        assert len(net.connects) == 1


def test_up_to_three_redirects_are_followed_and_the_fourth_is_refused():
    dns = {f"h{i}.example.com": [PUBLIC] for i in range(5)}
    ok = Net(dns, [redirect("http://h1.example.com/"), redirect("http://h2.example.com/", 301), redirect("http://h3.example.com/", 307), http()])
    res = ok.fetcher().fetch("http://h0.example.com/")
    assert res.url == "http://h3.example.com/" and len(res.redirects) == 3 and len(ok.connects) == 4
    bad = Net(dns, [redirect("http://h1.example.com/"), redirect("http://h2.example.com/"), redirect("http://h3.example.com/"), redirect("http://h4.example.com/")])
    with pytest.raises(ui.FetchError) as ei:
        bad.fetcher().fetch("http://h0.example.com/")
    assert ei.value.code == "redirect_limit" and len(bad.connects) == 4


def test_https_to_http_downgrade_is_refused_but_http_to_https_is_fine():
    down = Net({"a.example.com": [PUBLIC], "b.example.com": [PUBLIC]}, [redirect("http://b.example.com/")])
    with pytest.raises(ui.FetchError) as ei:
        down.fetcher().fetch("https://a.example.com/")
    assert ei.value.code == "https_downgrade" and len(down.connects) == 1
    up = Net({"a.example.com": [PUBLIC], "b.example.com": [PUBLIC]}, [redirect("https://b.example.com/"), http()])
    assert up.fetcher().fetch("http://a.example.com/").url == "https://b.example.com/"


def test_relative_redirects_are_resolved_against_the_current_address():
    net = Net({"a.example.com": [PUBLIC]}, [redirect("/new/path?x=1"), http()])
    res = net.fetcher().fetch("https://a.example.com/old")
    assert res.url == "https://a.example.com/new/path?x=1"


def test_redirect_without_location_is_an_error():
    net = Net({"a.example.com": [PUBLIC]}, [http(b"", status=302, ctype=None)])
    with pytest.raises(ui.FetchError):
        net.fetcher().fetch("http://a.example.com/")


# --- 응답 ------------------------------------------------------------------------------
def test_only_html_content_types_are_accepted():
    for ctype in ("application/json", "image/png", "application/pdf", "text/plain", "application/octet-stream", None):
        net = Net({"a.example.com": [PUBLIC]}, [http(b"x", ctype=ctype)])
        with pytest.raises(ui.FetchError) as ei:
            net.fetcher().fetch("http://a.example.com/")
        assert ei.value.code == "content_type", ctype
    for ctype in ("text/html", "TEXT/HTML; charset=euc-kr", "application/xhtml+xml"):
        net = Net({"a.example.com": [PUBLIC]}, [http(b"<p>x</p>", ctype=ctype)])
        assert net.fetcher().fetch("http://a.example.com/").body == b"<p>x</p>"


def test_charset_from_content_type_header_is_passed_along():
    net = Net({"a.example.com": [PUBLIC]}, [http(b"x", ctype="text/html; charset=EUC-KR")])
    assert net.fetcher().fetch("http://a.example.com/").charset == "EUC-KR"


@pytest.mark.parametrize("status", [204, 400, 401, 403, 404, 429, 500, 503])
def test_non_success_statuses_are_refused_with_a_plain_message(status):
    net = Net({"a.example.com": [PUBLIC]}, [http(b"<p>x</p>", status=status)])
    with pytest.raises(ui.FetchError) as ei:
        net.fetcher().fetch("http://a.example.com/")
    assert ei.value.code == "status" and str(status) in ei.value.user_message


def test_body_over_1_5mb_is_cut_there_and_marked_truncated():
    big = b"a" * (ui.MAX_BODY_BYTES + 700_000)
    net = Net({"a.example.com": [PUBLIC]}, [http(big)])
    res = net.fetcher().fetch("http://a.example.com/")
    assert len(res.body) == ui.MAX_BODY_BYTES == int(1.5 * 1024 * 1024) and res.truncated is True


def test_body_exactly_at_the_limit_is_not_truncated():
    net = Net({"a.example.com": [PUBLIC]}, [http(b"b" * 5000)])
    res = net.fetcher(max_body=5000).fetch("http://a.example.com/")
    assert len(res.body) == 5000 and res.truncated is False


def test_chunked_responses_are_read_and_capped_too():
    net = Net({"a.example.com": [PUBLIC]}, [http(b"c" * 300_000, chunked=True)])
    res = net.fetcher(max_body=100_000).fetch("http://a.example.com/")
    assert len(res.body) == 100_000 and res.truncated is True


def test_gzip_and_deflate_bodies_are_decoded():
    html = ("<html><body>" + "상품 설명 " * 200 + "</body></html>").encode("utf-8")
    for enc, payload in (("gzip", gzip.compress(html)), ("deflate", zlib.compress(html)), ("deflate", zlib.compress(html)[2:-4])):   # 마지막은 raw deflate
        net = Net({"a.example.com": [PUBLIC]}, [http(payload, encoding=enc)])
        res = net.fetcher().fetch("http://a.example.com/")
        assert res.body == html and res.truncated is False, enc


def test_compression_bomb_is_capped_after_decompression():
    """압축된 크기는 작지만 풀면 수백 MB 가 되는 응답. 푼 뒤 크기로 상한을 다시 확인해 잘라야 한다."""
    comp = zlib.compressobj(9, zlib.DEFLATED, 31)
    bomb = comp.compress(b"\x00" * (200 * 1024 * 1024)) + comp.flush()
    assert len(bomb) < ui.MAX_BODY_BYTES           # 압축 상태로는 상한 안
    net = Net({"a.example.com": [PUBLIC]}, [http(bomb, encoding="gzip")])
    res = net.fetcher().fetch("http://a.example.com/")
    assert len(res.body) == ui.MAX_BODY_BYTES and res.truncated is True


def test_decompression_never_expands_more_than_the_cap_in_a_single_step(monkeypatch):
    """결과 크기만 보면 '풀고 나서 자르는' 구현도 통과한다. 하지만 그러면 압축 폭탄이 메모리를 먼저 채운다 — 한 번에 푸는 양에 상한이 걸렸는지 직접 본다."""
    comp = zlib.compressobj(9, zlib.DEFLATED, 31)
    bomb = comp.compress(b"\x00" * (120 * 1024 * 1024)) + comp.flush()
    sizes: list[int] = []
    real = zlib.decompressobj

    class Spy:
        def __init__(self, *a, **k):
            self._d = real(*a, **k)

        def decompress(self, data, max_length=0):
            out = self._d.decompress(data, max_length)
            sizes.append(len(out))
            return out

        @property
        def unconsumed_tail(self):
            return self._d.unconsumed_tail

        @property
        def eof(self):
            return self._d.eof

    monkeypatch.setattr(ui.zlib, "decompressobj", Spy)
    net = Net({"a.example.com": [PUBLIC]}, [http(bomb, encoding="gzip")])
    res = net.fetcher().fetch("http://a.example.com/")
    assert sizes and max(sizes) <= ui.MAX_BODY_BYTES + 1 and res.truncated is True


def test_unsupported_or_corrupt_encodings_are_refused():
    for enc in ("br", "zstd", "compress"):
        net = Net({"a.example.com": [PUBLIC]}, [http(b"xx", encoding=enc)])
        with pytest.raises(ui.FetchError) as ei:
            net.fetcher().fetch("http://a.example.com/")
        assert ei.value.code == "encoding"
    net = Net({"a.example.com": [PUBLIC]}, [http(b"this is not gzip data at all", encoding="gzip")])
    with pytest.raises(ui.FetchError) as ei:
        net.fetcher().fetch("http://a.example.com/")
    assert ei.value.code == "encoding"


def test_total_time_limit_applies_even_when_the_server_drips_bytes_slowly():
    clock = Clock()
    net = Net({"a.example.com": [PUBLIC]}, [http(b"x" * 200_000)], clock=clock, delay=2.0)    # recv 한 번에 2초 — 수신 하나하나는 제한 안이지만 합계는 넘는다
    with pytest.raises(ui.FetchError) as ei:
        net.fetcher().fetch("http://a.example.com/")
    assert ei.value.code == "timeout" and clock.t <= ui.TOTAL_TIMEOUT_S + 2.0
    assert net.sockets[0].closed                    # 실패해도 소켓은 닫힌다


def test_remaining_time_is_given_to_the_socket_and_shrinks():
    clock = Clock()
    net = Net({"a.example.com": [PUBLIC]}, [http(b"x" * 80_000)], clock=clock, delay=1.0)
    with pytest.raises(ui.FetchError):
        net.fetcher().fetch("http://a.example.com/")
    t = net.sockets[0].timeouts
    assert t and all(0 < x <= ui.TOTAL_TIMEOUT_S for x in t) and t == sorted(t, reverse=True)
    assert net.connects[0]["timeout"] <= ui.TOTAL_TIMEOUT_S


def test_connection_failure_is_a_plain_error_and_tries_only_checked_addresses():
    tried = []

    def connector(ip, port, **kw):
        tried.append(ip)
        raise ConnectionRefusedError()

    f = ui.HttpFetcher(resolver=lambda h, p: [PUBLIC, "93.184.216.35"], connector=connector)
    with pytest.raises(ui.FetchError) as ei:
        f.fetch("http://a.example.com/")
    assert ei.value.code == "connect" and tried == [PUBLIC, "93.184.216.35"]


def test_garbage_response_is_a_plain_error():
    net = Net({"a.example.com": [PUBLIC]}, [b"NOT HTTP AT ALL\r\n\r\n"])
    with pytest.raises(ui.FetchError) as ei:
        net.fetcher().fetch("http://a.example.com/")
    assert ei.value.code == "connect"


# --- fetch_page · 예시 페이지 · 브라우저 ---------------------------------------------------
def test_fetch_page_uses_the_injected_fetcher_for_http_addresses():
    class Fake:
        def fetch(self, url):
            return ui.FetchResult(url=url, body=b"<p>hi</p>")

    assert ui.fetch_page("https://example.com/x", Fake()).body == b"<p>hi</p>"


def test_in_the_browser_build_real_fetching_is_disabled_but_samples_still_work(monkeypatch):
    monkeypatch.setattr(config, "IN_BROWSER", True)
    called = []

    class Fake:
        def fetch(self, url):
            called.append(url)

    with pytest.raises(ui.FetchError) as ei:
        ui.fetch_page("https://example.com/", Fake())
    assert ei.value.code == "unavailable" and called == []
    assert ui.fetch_page("sample://p01_jsonld_clean").status == 200


def test_sample_fetcher_cannot_read_html_files_outside_its_folder(tmp_path):
    root = tmp_path / "pages"
    root.mkdir()
    (root / "ok.html").write_bytes(b"<p>ok</p>")
    (tmp_path / "secret.html").write_bytes(b"<p>SECRET</p>")              # 폴더 바로 바깥의 html — 경로 탈출이 되면 읽힌다
    f = ui.SampleFetcher(root)
    assert f.fetch("sample://ok").body == b"<p>ok</p>"
    for bad in ("sample://../secret", "sample://./../secret", "sample://sub/../../secret", "sample://%2e%2e/secret", "sample://..\\secret", "sample://ok.html/../../secret"):
        with pytest.raises(ui.FetchError):
            f.fetch(bad)


def test_sample_fetcher_reads_only_fixture_names_and_cannot_escape_the_folder(tmp_path):
    f = ui.SampleFetcher()
    for bad in ("sample://../core/url_ingest", "sample://..%2Fx", "sample://a/b", "sample://", "sample://" + "a" * 100, "sample://p01 jsonld", "sample:///etc/passwd", "https://example.com/"):
        with pytest.raises(ui.FetchError):
            f.fetch(bad)
    with pytest.raises(ui.FetchError):
        f.fetch("sample://no_such_page")


def test_sample_fetcher_expands_repeat_blocks_and_applies_the_same_body_cap(tmp_path):
    (tmp_path / "big.html").write_bytes(b"<p>head</p><!--pf:repeat times=1000--><p>0123456789</p><!--pf:end--><p>tail</p>")
    small = ui.SampleFetcher(tmp_path, max_body=5000).fetch("sample://big")
    assert len(small.body) == 5000 and small.truncated is True
    full = ui.SampleFetcher(tmp_path, max_body=10**6).fetch("sample://big")
    assert full.truncated is False and full.body.count(b"0123456789") == 1000 and full.body.endswith(b"<p>tail</p>")


def test_sample_pages_listing_hides_final_check_pages_by_default():
    f = ui.SampleFetcher()
    names = {n for n, _ in f.list_samples()}
    assert "p01_jsonld_clean" in names and not any(n.startswith("h") and n[1:3].isdigit() for n in names)
    assert {n for n, _ in f.list_samples("all")} >= names


# --- 가져오기 모듈은 import 시점에 네트워크 모듈을 끌어오지 않는다 ------------------------------
def test_importing_the_module_does_not_load_socket_ssl_or_http_client():
    # pydantic 은 (Python 3.11 에서) 모델을 만들 때 importlib.metadata → email.utils → socket 을 끌어온다. 우리 코드의 import 가 아니므로 기준선에 넣고 잰다.
    code = (f"import sys\nsys.path.insert(0, {str(ROOT)!r})\nfrom pydantic import BaseModel\n\nclass _Baseline(BaseModel):\n    x: int = 0\n\nbefore=set(sys.modules)\nimport core.url_ingest\nnew=set(sys.modules)-before\n"
            "bad=[m for m in ('socket','ssl','http.client','urllib.request') if m in new]\nprint(','.join(bad))\n")
    out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "", f"import 시점에 불러온 네트워크 모듈: {out.stdout}"


def test_default_resolver_and_connector_are_only_touched_when_actually_fetching():
    f = ui.HttpFetcher()                       # 만들기만 해서는 어떤 소켓 호출도 없다 (autouse 픽스처가 호출 시 실패시킨다)
    assert f.max_body == ui.MAX_BODY_BYTES and f.timeout_s == ui.TOTAL_TIMEOUT_S
