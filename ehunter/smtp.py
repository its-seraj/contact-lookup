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
import threading
import time
import uuid
from dataclasses import dataclass, field

from . import dnsutil

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
            return "smtp temp-fail (greylisting?)"
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


def _rcpt_once(mx_hosts, address: str, from_addr: str, timeout: float):
    """One-shot RCPT check against the first reachable MX. Returns (code, msg, host)."""
    last_msg = "connection failed"
    for host in mx_hosts[:3]:
        try:
            with smtplib.SMTP(host, 25, timeout=timeout) as smtp:
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


def verify_domain(emails, state: DomainState, from_addr: str, timeout: float,
                  delay: float = 0.2, log=None) -> "dict[str, VerifyResult]":
    """Verify a batch of addresses on one domain over a single reused SMTP session."""
    results = {e: VerifyResult(email=e, status="unknown", message=state.reason)
               for e in emails}
    if not state.usable:
        for r in results.values():
            r.status = "unknown"
        return results

    pending = list(dict.fromkeys(e.lower() for e in emails))
    host_idx = 0
    session = None
    per_session = 0
    try:
        while pending:
            if session is None:
                if host_idx >= len(state.mx):
                    for addr in pending:
                        results[addr].status = "unknown"
                        results[addr].message = "no reachable MX on port 25 (firewall/ISP block?)"
                    break
                host = state.mx[host_idx]
                try:
                    session = smtplib.SMTP(host, 25, timeout=timeout)
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
                code, msg = session.rcpt(addr)
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
                per_session += 1
            except (smtplib.SMTPException, OSError, socket.timeout):
                # Connection died mid-batch: reconnect and retry the same address.
                try:
                    session.close()
                except Exception:  # noqa: BLE001
                    pass
                session = None
                per_session = 0
                host_idx += 1
                time.sleep(0.5)
                continue

            if per_session >= 40:
                try:
                    session.rset()
                except (smtplib.SMTPException, OSError):
                    session = None
                per_session = 0
            if delay > 0:
                time.sleep(delay)
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
                threads: int = 4, delay: float = 0.2, log=None) -> "dict[str, VerifyResult]":
    """Verify addresses across domains, one worker per domain."""
    by_domain: dict = {}
    for email in dict.fromkeys(e.lower() for e in emails if "@" in e):
        by_domain.setdefault(email.rsplit("@", 1)[1], []).append(email)

    out: "dict[str, VerifyResult]" = {}
    out_lock = threading.Lock()
    sem = threading.Semaphore(max(1, threads))

    def worker(domain: str, addrs: "list[str]") -> None:
        with sem:
            state = domain_state(domain, http, from_addr, timeout, log=log)
            batch = verify_domain(addrs, state, from_addr, timeout, delay=delay, log=log)
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
