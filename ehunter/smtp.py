"""SMTP mailbox probing.

Port 25 verification is best-effort: big providers (Google, Microsoft) usually answer
truthfully for unknown mailboxes, catch-all domains answer 250 to everything, and
greylisting or anti-spam tarpits answer 4xx. Results are labelled accordingly instead
of being presented as ground truth.
"""

from __future__ import annotations

import re
import smtplib
import socket
import struct
import threading
import time
import urllib.parse
import uuid
from dataclasses import dataclass, field

from . import dnsutil
from .config import env

ACCEPT = {250, 251, 252}
MAILBOX_FULL = {552}
SOFT_FAIL = {450, 451, 452}
HARD_FAIL = {550, 551, 553, 554, 555, 501, 503}
TEMPORARY = {421, 420}

# A 5xx caused by the receiver distrusting *us* says nothing about the mailbox.
POLICY_HINTS = re.compile(
    r"5\.7\.|sender verification|verify sender|blocked|block-?list|black-?list|"
    r"spam|policy|denied|reputation|not permitted|no access|reverse dns|ptr|"
    r"spf|dkim|dmarc|greylist",
    re.I,
)

_STATE_LOCK = threading.Lock()
_DOMAIN_LOCKS: dict = {}


# --------------------------------------------------------------- SOCKS5 tunnel
# Many home/mobile networks drop outbound port 25. SMTP_PROXY=socks5://127.0.0.1:1080
# (for example `ssh -D 1080 user@vps` to a host that allows port 25) sends only the SMTP
# probes through that host. Unset, nothing here costs anything: connections go direct.
_SOCKS_ERRORS = {
    1: "general failure", 2: "not allowed by ruleset", 3: "network unreachable",
    4: "host unreachable", 5: "connection refused", 6: "TTL expired",
    7: "command not supported", 8: "address type not supported",
}


def proxy_settings() -> "tuple | None":
    """(host, port, user, password) from SMTP_PROXY, or None for a direct connection."""
    raw = env("SMTP_PROXY")
    if not raw:
        return None
    parsed = urllib.parse.urlparse(raw if "://" in raw else "socks5://" + raw)
    if parsed.scheme not in ("socks5", "socks5h") or not parsed.hostname:
        raise ValueError(f"SMTP_PROXY must look like socks5://host:port, got {raw!r}")
    return (parsed.hostname, parsed.port or 1080,
            urllib.parse.unquote(parsed.username or ""),
            urllib.parse.unquote(parsed.password or ""))


def _recv_exact(sock: socket.socket, count: int) -> bytes:
    data = b""
    while len(data) < count:
        chunk = sock.recv(count - len(data))
        if not chunk:
            raise OSError("SOCKS5 proxy closed the connection")
        data += chunk
    return data


def socks5_connect(proxy: tuple, host: str, port: int, timeout: float) -> socket.socket:
    """TCP connection to host:port through a SOCKS5 proxy (RFC 1928, no extra packages).

    The hostname is sent to the proxy, so the MX name is resolved on the far side.
    """
    proxy_host, proxy_port, user, password = proxy
    sock = socket.create_connection((proxy_host, proxy_port), timeout)
    try:
        sock.settimeout(timeout)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        sock.sendall(b"\x05\x02\x00\x02" if user else b"\x05\x01\x00")
        version, method = _recv_exact(sock, 2)
        if version != 5 or method == 0xFF:
            raise OSError("SOCKS5 proxy refused our authentication methods")
        if method == 2:
            u, p = user.encode(), password.encode()
            sock.sendall(b"\x01" + bytes([len(u)]) + u + bytes([len(p)]) + p)
            if _recv_exact(sock, 2)[1] != 0:
                raise OSError("SOCKS5 proxy rejected the username/password")
        name = host.encode("idna")
        sock.sendall(b"\x05\x01\x00\x03" + bytes([len(name)]) + name + struct.pack(">H", port))
        _version, reply, _rsv, atyp = _recv_exact(sock, 4)
        if reply != 0:
            raise OSError(f"SOCKS5 proxy could not reach {host}:{port} "
                          f"({_SOCKS_ERRORS.get(reply, reply)})")
        skip = {1: 4, 4: 16}.get(atyp)
        if skip is None:
            skip = _recv_exact(sock, 1)[0]
        _recv_exact(sock, skip + 2)             # bound address + port, unused
        return sock
    except BaseException:
        sock.close()
        raise


class _ProxiedSMTP(smtplib.SMTP):
    """smtplib.SMTP whose TCP connection goes through a SOCKS5 proxy."""

    def __init__(self, host, port, timeout, local_hostname, proxy):
        self._proxy = proxy
        super().__init__(host, port, timeout=timeout, local_hostname=local_hostname)

    def _get_socket(self, host, port, timeout):
        return socks5_connect(self._proxy, host, port, timeout)


# Networks often block port 25 over IPv4 but not IPv6 (or the reverse). Remember which
# family last connected so a blocked one is not retried first - and not waited on for the
# whole timeout - on every single connection.
_PREFERRED_FAMILY: "int | None" = None


def connect_direct(host: str, port: int, timeout: float) -> socket.socket:
    """TCP connect trying every address (IPv6 and IPv4), working family first."""
    global _PREFERRED_FAMILY
    infos = socket.getaddrinfo(host, port, 0, socket.SOCK_STREAM)
    if _PREFERRED_FAMILY is not None:
        infos.sort(key=lambda info: info[0] != _PREFERRED_FAMILY)      # stable
    last: "OSError | None" = None
    for family, socktype, proto, _canon, address in infos:
        sock = socket.socket(family, socktype, proto)
        try:
            sock.settimeout(timeout)
            sock.connect(address)
            _PREFERRED_FAMILY = family
            return sock
        except OSError as exc:
            sock.close()
            last = exc
    raise last or OSError(f"no addresses for {host}")


_DIRECT_CLASSES: dict = {}


def _direct_class(base):
    cls = _DIRECT_CLASSES.get(base)
    if cls is None:
        class Direct(base):
            def _get_socket(self, host, port, timeout):
                return connect_direct(host, port, timeout)
        _DIRECT_CLASSES[base] = cls = Direct
    return cls


def open_smtp(host: str, timeout: float, from_addr: str) -> smtplib.SMTP:
    """The one place SMTP connections are made: direct, or via SMTP_PROXY when set."""
    name = helo_name(from_addr)
    proxy = proxy_settings()
    if proxy:
        try:
            return _ProxiedSMTP(host, 25, timeout, name, proxy)
        except OSError as exc:
            # Name the tunnel: "connection refused" against an MX is misleading when the
            # refused connection is actually the local SOCKS port.
            raise OSError(
                f"SOCKS5 tunnel {proxy[0]}:{proxy[1]} could not reach {host}:25 ({exc})"
            ) from exc
    return _direct_class(smtplib.SMTP)(host, 25, timeout=timeout, local_hostname=name)


def route_status(timeout: float = 3.0) -> "tuple[bool, str]":
    """Is the SMTP path usable at all? Only a configured tunnel is checked.

    Direct connections are not pre-probed: whether a given MX answers is decided per
    domain by verify_domain(), and a global probe would only add three seconds.
    """
    try:
        proxy = proxy_settings()
    except ValueError as exc:
        return False, str(exc)
    if not proxy:
        return True, ""
    host, port, _user, _password = proxy
    try:
        with socket.create_connection((host, port), timeout):
            return True, ""
    except OSError as exc:
        return False, (
            f"SMTP_PROXY {host}:{port} is not reachable ({type(exc).__name__}). "
            f"Start the tunnel (ssh -N -D {port} user@your-vps) or unset SMTP_PROXY "
            f"to connect directly."
        )


def check_port25(host: str, timeout: float = 8.0) -> "tuple[bool, str]":
    """Can we reach `host`:25 (through SMTP_PROXY if set)? Returns (ok, reason).

    Uses the same connect path as real probes, so IPv6-only access counts as open.
    """
    try:
        proxy = proxy_settings()
        if proxy:
            socks5_connect(proxy, host, 25, timeout).close()
        else:
            connect_direct(host, 25, timeout).close()
        return True, ""
    except ValueError as exc:
        return False, str(exc)
    except OSError as exc:
        return False, str(exc) or type(exc).__name__


@dataclass
class DomainState:
    domain: str
    mx: list = field(default_factory=list)
    catch_all: "bool | None" = None
    reason: str = ""

    @property
    def usable(self) -> bool:
        return bool(self.mx) and not self.reason


@dataclass
class VerifyResult:
    email: str
    status: str = "unknown"     # valid | invalid | greylisted | catch_all | unknown
    code: int = 0
    message: str = ""
    mx: str = ""

    @property
    def note(self) -> str:
        if self.status == "valid":
            return "smtp ok"
        if self.status == "invalid":
            return f"smtp rejected ({self.message})" if self.message else "smtp rejected"
        if self.status == "greylisted":
            return (f"smtp temp-fail ({self.message})" if self.message
                    else "smtp temp-fail (greylisting?)")
        if self.status == "catch_all":
            return "catch-all domain: acceptance proves nothing"
        if self.message:
            return f"smtp inconclusive: {self.message}"
        return "smtp unknown"


def _domain_lock(domain: str) -> threading.Lock:
    with _STATE_LOCK:
        lock = _DOMAIN_LOCKS.get(domain)
        if lock is None:
            lock = threading.Lock()
            _DOMAIN_LOCKS[domain] = lock
        return lock


def domain_state(domain: str, http, from_addr: str, timeout: float, log=None) -> DomainState:
    """Resolve MX once per domain and detect catch-all once per domain."""
    lock = _domain_lock(domain)
    with lock:
        cached = dnsutil._cache_get(("state", domain))
        if cached is not None:
            return cached
        state = DomainState(domain=domain)
        state.mx = [h for _, h in dnsutil.mx_records(domain, http)]
        if not state.mx:
            state.reason = "no MX record (domain does not receive mail)"
            dnsutil._cache_put(("state", domain), state)
            return state

        probe = f"eh-probe-{uuid.uuid4().hex[:10]}@{domain}"
        code, msg, used = _rcpt_once(state.mx, probe, from_addr, timeout)
        if code in ACCEPT:
            state.catch_all = True
            if log:
                log(f"    {domain}: catch-all MX ({used}), verification is uncorroborated")
        elif code in HARD_FAIL:
            # If the server rejected *us* rather than the address, catch-all is unknown.
            state.catch_all = None if POLICY_HINTS.search(msg or "") else False
        else:
            state.catch_all = None  # tarpit/greylist/unreachable
        dnsutil._cache_put(("state", domain), state)
        return state


def helo_name(from_addr: str) -> str:
    """Name to announce in EHLO: the sender's domain, never the machine's hostname.

    smtplib defaults to socket.getfqdn() (e.g. 'LAPTOP-TSD8MGGB'), which is not a domain
    name; receivers such as Google treat that as a reason to throttle or distrust probes.
    """
    domain = from_addr.rsplit("@", 1)[1].strip().lower() if "@" in (from_addr or "") else ""
    return domain if "." in domain else "localhost.localdomain"


def _rcpt_once(mx_hosts, address: str, from_addr: str, timeout: float):
    """One-shot RCPT check against the first reachable MX. Returns (code, msg, host)."""
    last_msg = "connection failed"
    for host in mx_hosts[:3]:
        try:
            with open_smtp(host, timeout, from_addr) as smtp:
                smtp.ehlo_or_helo_if_needed()
                code, msg = smtp.mail(from_addr)
                if code not in ACCEPT:
                    last_msg = f"MAIL FROM rejected: {code} {_short(msg)}"
                    continue
                code, msg = smtp.rcpt(address)
                try:
                    smtp.rset()
                except (smtplib.SMTPException, OSError):
                    pass
                return int(code), _short(msg), host
        except (smtplib.SMTPException, OSError, socket.timeout) as exc:
            last_msg = f"{host}: {exc}"
            continue
    return 0, last_msg, ""


def _short(msg) -> str:
    if isinstance(msg, bytes):
        msg = msg.decode("utf-8", "replace")
    return " ".join(str(msg).split())[:120]


def _classify(code: int, state: DomainState, message: str = "") -> str:
    if code == 0:
        return "unknown"
    if code in ACCEPT or code in MAILBOX_FULL:
        return "catch_all" if state.catch_all else "valid"
    if code in SOFT_FAIL or code in TEMPORARY:
        return "greylisted"
    if code in HARD_FAIL:
        # "550 Sender verification failed" is about our envelope, not the recipient.
        if POLICY_HINTS.search(message or ""):
            return "unknown"
        return "invalid"
    return "unknown"


def reply_text(result) -> str:
    """'550 5.1.1 no such user' for notes; empty when the server never answered."""
    code = getattr(result, "code", 0) or 0
    message = getattr(result, "message", "") or ""
    return f"{code} {message}".strip() if code else message


# Long sessions full of failed recipients look like address harvesting, so rotate often.
SESSION_PROBES = 15
BACKOFF_STEP = 2.0
BACKOFF_MAX = 10.0


def probe_pause(delay: float, round_trip: float, soft_streak: int) -> float:
    """Seconds to wait before the next probe.

    `delay` is the gap we want between probes. Through a tunnel the RCPT round-trip already
    spends part of that gap, so it is subtracted (direct connections pass 0 and behave as
    before): the probe rate stays what the user configured instead of slowing down by
    the tunnel's latency. Backoff for a server that keeps answering 4xx is added on top.
    """
    return max(0.0, delay - round_trip) + min(BACKOFF_STEP * soft_streak, BACKOFF_MAX)


def _audit_session(session, control: str, probed: "list[str]", results: dict, log=None) -> None:
    """Ask the server about an address known to exist; if it refuses, this session lied.

    A hard "no such user" for the *known-good* control means the 550s handed out in this
    session carry no information about mailboxes (throttling, or distrust of our sender).
    Those results are downgraded to `greylisted` so they get retried instead of trusted.
    """
    if not control or not probed or session is None:
        return
    try:
        code, msg = session.rcpt(control)
        code = int(code)
        text = _short(msg)
    except (smtplib.SMTPException, OSError, socket.timeout):
        return
    if code in ACCEPT or code in MAILBOX_FULL:
        return
    downgraded = 0
    for addr in probed:
        res = results.get(addr)
        if res is not None and res.status == "invalid":
            res.message = (f"unreliable: control address {control} was refused "
                           f"({code} {text}) after this probe; original reply {res.code} {res.message}")
            res.status = "greylisted"
            downgraded += 1
    if downgraded and log:
        log(f"    control {control} refused ({code}); {downgraded} rejection(s) not trusted")


def verify_domain(emails, state: DomainState, from_addr: str, timeout: float,
                  delay: float = 0.2, log=None, control: str = "") -> "dict[str, VerifyResult]":
    """Verify a batch of addresses on one domain, a few at a time per SMTP session.

    `control` is an address known to exist on this domain. It is probed at the end of
    every session to detect servers that answer "no such user" to everything.
    """
    results = {e: VerifyResult(email=e, status="unknown", message=state.reason)
               for e in emails}
    if not state.usable:
        for r in results.values():
            r.status = "unknown"
        return results

    control = (control or "").lower()
    if control and not control.endswith("@" + state.domain):
        control = ""
    pending = list(dict.fromkeys(e.lower() for e in emails))
    host_idx = 0
    session = None
    per_session = 0
    probed: "list[str]" = []
    soft_streak = 0
    tunnelled = proxy_settings() is not None
    try:
        while pending:
            if session is None:
                if host_idx >= len(state.mx):
                    for addr in pending:
                        results[addr].status = "unknown"
                        results[addr].message = (
                            "no reachable MX on port 25 (tunnel down or port 25 blocked)"
                            if proxy_settings()
                            else "no reachable MX on port 25 (firewall/ISP block?)"
                        )
                    break
                host = state.mx[host_idx]
                try:
                    session = open_smtp(host, timeout, from_addr)
                    session.ehlo_or_helo_if_needed()
                    code, msg = session.mail(from_addr)
                    if code not in ACCEPT:
                        if log:
                            log(f"    {host} refused MAIL FROM ({code}), trying next MX")
                        try:
                            session.quit()
                        except (smtplib.SMTPException, OSError):
                            pass
                        session = None
                        host_idx += 1
                        continue
                except (smtplib.SMTPException, OSError, socket.timeout) as exc:
                    if log:
                        log(f"    cannot open SMTP session to {host}: {exc}")
                    session = None
                    host_idx += 1
                    continue

            addr = pending[0]
            try:
                started = time.monotonic()
                code, msg = session.rcpt(addr)
                round_trip = time.monotonic() - started
                code = int(code)
                text = _short(msg)
                res = VerifyResult(
                    email=addr, status=_classify(code, state, text),
                    code=code, message=text, mx=state.mx[host_idx],
                )
                if code in MAILBOX_FULL:
                    res.message = "mailbox full but exists"
                results[addr] = res
                pending.pop(0)
                probed.append(addr)
                per_session += 1
            except (smtplib.SMTPException, OSError, socket.timeout):
                # Connection died mid-batch: reconnect and retry the same address.
                try:
                    session.close()
                except Exception:  # noqa: BLE001
                    pass
                session = None
                per_session = 0
                probed = []
                host_idx += 1
                time.sleep(0.5)
                continue

            # The server is pushing back: slow down instead of hammering it.
            if res.status == "greylisted":
                soft_streak += 1
            else:
                soft_streak = 0
            if per_session >= SESSION_PROBES or not pending:
                _audit_session(session, control, probed, results, log=log)
                probed = []
                if pending:
                    try:
                        session.quit()
                    except (smtplib.SMTPException, OSError):
                        pass
                    session = None
                    per_session = 0
            pause = probe_pause(delay, round_trip if tunnelled else 0.0, soft_streak)
            if pause > 0:
                time.sleep(pause)
    finally:
        if session is not None:
            try:
                session.quit()
            except (smtplib.SMTPException, OSError):
                try:
                    session.close()
                except Exception:  # noqa: BLE001
                    pass
    return results


def verify_many(emails, http, from_addr: str, timeout: float = 10.0,
                threads: int = 4, delay: float = 0.2, log=None,
                control: str = "") -> "dict[str, VerifyResult]":
    """Verify addresses across domains, one worker per domain.

    `control` is an address known to exist (used only for its own domain).
    """
    ready, reason = route_status()
    if not ready:
        if log:
            log(f"  [!] smtp: {reason}")
        return {
            email: VerifyResult(email=email, status="unknown", message=reason)
            for email in dict.fromkeys(e.lower() for e in emails if "@" in e)
        }

    by_domain: dict = {}
    for email in dict.fromkeys(e.lower() for e in emails if "@" in e):
        by_domain.setdefault(email.rsplit("@", 1)[1], []).append(email)

    out: "dict[str, VerifyResult]" = {}
    out_lock = threading.Lock()
    sem = threading.Semaphore(max(1, threads))

    def worker(domain: str, addrs: "list[str]") -> None:
        with sem:
            state = domain_state(domain, http, from_addr, timeout, log=log)
            batch = verify_domain(addrs, state, from_addr, timeout, delay=delay, log=log,
                                  control=control)
            with out_lock:
                out.update(batch)

    workers = []
    for domain, addrs in by_domain.items():
        t = threading.Thread(target=worker, args=(domain, addrs), daemon=True)
        t.start()
        workers.append(t)
    for t in workers:
        t.join()
    return out
