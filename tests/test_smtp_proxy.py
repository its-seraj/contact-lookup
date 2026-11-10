"""SMTP over a SOCKS5 tunnel: real handshake + real smtplib against a fake proxy."""

from __future__ import annotations

import os
import socket
import socketserver
import struct
import threading
import unittest
from unittest import mock

from ehunter import smtp


class FakeSocksSmtp(socketserver.ThreadingTCPServer):
    """Speaks SOCKS5, then plays the destination mail server itself."""

    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, user="", password="", reply_code=0, mailbox_ok=True):
        super().__init__(("127.0.0.1", 0), self.Handler)
        self.user, self.password = user, password
        self.reply_code = reply_code        # non-zero: refuse the CONNECT with this code
        self.mailbox_ok = mailbox_ok
        self.targets: list = []
        self.connections = 0
        self.log: list = []

    @property
    def url(self) -> str:
        auth = f"{self.user}:{self.password}@" if self.user else ""
        return f"socks5://{auth}127.0.0.1:{self.server_address[1]}"

    class Handler(socketserver.StreamRequestHandler):
        def handle(self):
            srv = self.server
            srv.connections += 1
            read = self.rfile.read
            ver, nmethods = read(2)
            methods = read(nmethods)
            if srv.user:
                self.wfile.write(b"\x05\x02")
                read(1)
                ulen = read(1)[0]
                user = read(ulen).decode()
                plen = read(1)[0]
                pwd = read(plen).decode()
                ok = (user, pwd) == (srv.user, srv.password)
                self.wfile.write(b"\x01" + (b"\x00" if ok else b"\x01"))
                if not ok:
                    return
            else:
                self.wfile.write(b"\x05\x00")
            _v, _cmd, _rsv, atyp = read(4)
            assert atyp == 3, "hostname must be sent to the proxy, not resolved locally"
            host = read(read(1)[0]).decode()
            (port,) = struct.unpack(">H", read(2))
            srv.targets.append((host, port))
            if srv.reply_code:
                self.wfile.write(b"\x05" + bytes([srv.reply_code]) + b"\x00\x01" + bytes(6))
                return
            self.wfile.write(b"\x05\x00\x00\x01" + bytes(4) + struct.pack(">H", 0))
            self.wfile.flush()
            self.wfile.write(b"220 fake.mx ESMTP\r\n")
            self.wfile.flush()
            for raw in self.rfile:
                line = raw.decode().strip()
                srv.log.append(line)
                verb = line.split(" ", 1)[0].upper()
                if verb == "EHLO":
                    out = "250 fake.mx\r\n"
                elif verb == "RCPT":
                    ok = srv.mailbox_ok and "nobody" not in line
                    out = "250 ok\r\n" if ok else "550 5.1.1 no such user\r\n"
                elif verb == "QUIT":
                    self.wfile.write(b"221 bye\r\n")
                    return
                else:
                    out = "250 ok\r\n"
                self.wfile.write(out.encode())
                self.wfile.flush()

    def handle_error(self, request, client_address):
        pass                                # clients hang up abruptly; that is fine here

    def __enter__(self):
        threading.Thread(target=self.serve_forever, kwargs={'poll_interval': 0.01},
                         daemon=True).start()
        return self

    def __exit__(self, *exc):
        self.shutdown()
        self.server_close()


def with_proxy(url):
    return mock.patch.dict(os.environ, {"SMTP_PROXY": url} if url else {}, clear=False)


class ProxySettingsTests(unittest.TestCase):
    def test_unset_means_direct(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("SMTP_PROXY", None)
            self.assertIsNone(smtp.proxy_settings())

    def test_parses_url_with_credentials(self):
        with with_proxy("socks5h://bob:p%40ss@10.0.0.5:9050"):
            self.assertEqual(smtp.proxy_settings(), ("10.0.0.5", 9050, "bob", "p@ss"))

    def test_bare_host_gets_defaults(self):
        with with_proxy("localhost"):
            self.assertEqual(smtp.proxy_settings(), ("localhost", 1080, "", ""))

    def test_http_proxy_is_rejected(self):
        with with_proxy("http://proxy:3128"):
            with self.assertRaises(ValueError):
                smtp.proxy_settings()


class PacingTests(unittest.TestCase):
    def test_direct_connections_keep_the_full_delay(self):
        self.assertEqual(smtp.probe_pause(0.2, 0.0, 0), 0.2)

    def test_tunnel_latency_counts_towards_the_delay(self):
        self.assertAlmostEqual(smtp.probe_pause(0.2, 0.15, 0), 0.05)

    def test_slow_tunnel_needs_no_extra_wait(self):
        self.assertEqual(smtp.probe_pause(0.2, 0.4, 0), 0.0)

    def test_backoff_still_applies_through_a_tunnel(self):
        self.assertEqual(smtp.probe_pause(0.2, 0.4, 3), 3 * smtp.BACKOFF_STEP)
        self.assertEqual(smtp.probe_pause(0.0, 0.0, 99), smtp.BACKOFF_MAX)


class TunnelTests(unittest.TestCase):
    def test_rcpt_through_the_tunnel_sends_the_mx_hostname(self):
        with FakeSocksSmtp() as proxy, with_proxy(proxy.url):
            code, msg, host = smtp._rcpt_once(["aspmx.l.google.com"], "a@acme.com",
                                              "seraj@gmail.com", 5)
        self.assertEqual((code, host), (250, "aspmx.l.google.com"))
        self.assertEqual(proxy.targets, [("aspmx.l.google.com", 25)])
        self.assertIn("ehlo gmail.com", [line.lower() for line in proxy.log])  # not the hostname

    def test_a_batch_reuses_one_tunnelled_connection(self):
        state = smtp.DomainState(domain="acme.com", mx=["mx.acme.com"], catch_all=False)
        emails = [f"u{i}@acme.com" for i in range(10)] + ["nobody@acme.com"]
        with FakeSocksSmtp() as proxy, with_proxy(proxy.url):
            out = smtp.verify_domain(emails, state, "seraj@gmail.com", 5, delay=0.0)
        self.assertEqual(proxy.connections, 1)               # no per-probe reconnect cost
        self.assertEqual(out["u3@acme.com"].status, "valid")
        self.assertEqual(out["nobody@acme.com"].status, "invalid")

    def test_username_password_auth(self):
        with FakeSocksSmtp(user="bob", password="secret") as proxy, with_proxy(proxy.url):
            code, _msg, _host = smtp._rcpt_once(["mx.acme.com"], "a@acme.com",
                                                "me@gmail.com", 5)
        self.assertEqual(code, 250)

    def test_wrong_password_fails_cleanly(self):
        with FakeSocksSmtp(user="bob", password="secret") as proxy:
            url = proxy.url.replace("secret", "wrong")
            with with_proxy(url):
                code, msg, _host = smtp._rcpt_once(["mx.acme.com"], "a@acme.com",
                                                   "me@gmail.com", 5)
        self.assertEqual(code, 0)
        self.assertIn("rejected the username/password", msg)

    def test_proxy_refusing_the_target_is_reported(self):
        with FakeSocksSmtp(reply_code=2) as proxy, with_proxy(proxy.url):
            ok, why = smtp.check_port25("mx.acme.com", timeout=3)
        self.assertFalse(ok)
        self.assertIn("not allowed by ruleset", why)

    def test_check_port25_ok_through_the_tunnel(self):
        with FakeSocksSmtp() as proxy, with_proxy(proxy.url):
            self.assertEqual(smtp.check_port25("mx.acme.com", timeout=3), (True, ""))

    def test_dead_tunnel_is_reported_quickly(self):
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()                                         # nothing listens here now
        with with_proxy(f"socks5://127.0.0.1:{port}"):
            ok, why = smtp.check_port25("mx.acme.com", timeout=3)
        self.assertFalse(ok)
        self.assertTrue(why)

    def test_no_proxy_connects_directly_and_announces_the_sender_domain(self):
        names = []

        class Recorder:
            def __init__(self, host, port, timeout=None, local_hostname=None):
                names.append((host, port, timeout, local_hostname))

        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("SMTP_PROXY", None)
            with mock.patch.object(smtp.smtplib, "SMTP", Recorder):
                smtp.open_smtp("mx.acme.com", 7, "me@gmail.com")
        self.assertEqual(names, [("mx.acme.com", 25, 7, "gmail.com")])


class _BlockedV6:
    """Stands in for an IPv6 socket on a network that drops port 25 over IPv6."""

    def __init__(self, tried):
        tried.append(socket.AF_INET6)

    def settimeout(self, _t):
        pass

    def connect(self, _addr):
        raise ConnectionRefusedError("blocked")

    def close(self):
        pass


class DirectConnectTests(unittest.TestCase):
    """Port 25 open over one IP family only (common on mobile / home networks)."""

    def setUp(self):
        self.server = socket.socket()
        self.server.bind(("127.0.0.1", 0))
        self.server.listen(5)
        self.port = self.server.getsockname()[1]
        self.addCleanup(self.server.close)
        self.addCleanup(setattr, smtp, "_PREFERRED_FAMILY", smtp._PREFERRED_FAMILY)
        smtp._PREFERRED_FAMILY = None
        self.tried: list = []
        real_socket = socket.socket

        def factory(family, *args, **kwargs):
            if family == socket.AF_INET6:
                return _BlockedV6(self.tried)
            self.tried.append(family)
            return real_socket(family, *args, **kwargs)

        patcher = mock.patch.object(smtp.socket, "socket", factory)
        patcher.start()
        self.addCleanup(patcher.stop)

    def infos(self, only_blocked=False):
        broken = (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("::1", 25, 0, 0))
        working = (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", self.port))
        return [broken] if only_blocked else [broken, working]

    def test_falls_back_to_the_family_that_works(self):
        with mock.patch.object(smtp.socket, "getaddrinfo", return_value=self.infos()):
            smtp.connect_direct("mx.example", 25, 3).close()
        self.assertEqual(self.tried, [socket.AF_INET6, socket.AF_INET])
        self.assertEqual(smtp._PREFERRED_FAMILY, socket.AF_INET)

    def test_working_family_is_tried_first_next_time(self):
        smtp._PREFERRED_FAMILY = socket.AF_INET
        with mock.patch.object(smtp.socket, "getaddrinfo", return_value=self.infos()):
            smtp.connect_direct("mx.example", 25, 3).close()
        self.assertEqual(self.tried, [socket.AF_INET])     # the blocked family cost nothing

    def test_every_address_failing_raises_the_last_error(self):
        with mock.patch.object(smtp.socket, "getaddrinfo",
                               return_value=self.infos(only_blocked=True)):
            with self.assertRaises(OSError):
                smtp.connect_direct("mx.example", 25, 3)

    def test_check_port25_counts_one_working_family_as_open(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("SMTP_PROXY", None)
            with mock.patch.object(smtp.socket, "getaddrinfo", return_value=self.infos()):
                self.assertEqual(smtp.check_port25("mx.example", 3), (True, ""))


if __name__ == "__main__":
    unittest.main()
