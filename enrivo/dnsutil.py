"""DNS via DNS-over-HTTPS (no dependency on the OS resolver, no dnspython needed).

Falls back to dnspython when installed, then to `nslookup` if both fail.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import threading
import urllib.parse

DOH_ENDPOINTS = [
    ("https://cloudflare-dns.com/dns-query", {"Accept": "application/dns-json"}),
    ("https://dns.google/resolve", {"Accept": "application/dns-json"}),
]

MX_CACHE: dict = {}
_CACHE_LOCK = threading.Lock()


def _cache_get(key):
    with _CACHE_LOCK:
        return MX_CACHE.get(key)


def _cache_put(key, value):
    with _CACHE_LOCK:
        MX_CACHE[key] = value
    return value


def _doh_query(name: str, rtype: str, http) -> "dict | None":
    last = None
    for base, headers in DOH_ENDPOINTS:
        try:
            data = http.get_json(base, params={"name": name, "type": rtype}, headers=headers, retries=1)
            if isinstance(data, dict):
                return data
        except Exception as exc:  # noqa: BLE001 - fall through to next provider
            last = exc
            continue
    if last:
        raise last
    return None


def _parse_mx(answer) -> "list[tuple[int, str]]":
    """Turn DoH Answer records into [(preference, host)] sorted by preference."""
    out = []
    for rr in answer or []:
        if not isinstance(rr, dict) or rr.get("type") not in (15, "MX"):
            continue
        data = str(rr.get("data") or "").strip()
        parts = data.split()
        if len(parts) < 2:
            continue
        try:
            pref = int(parts[0])
        except ValueError:
            pref = 10
        host = parts[1].rstrip(".").lower()
        if not host or host == ".":      # RFC 7505 null MX: domain accepts no mail
            continue
        if host in {h for _, h in out}:
            continue
        out.append((pref, host))
    out.sort(key=lambda x: x[0])
    return out


def mx_records(domain: str, http, use_dns_python: bool = True) -> "list[tuple[int, str]]":
    """MX hosts for a domain, best (lowest preference) first. Empty list when no mail."""
    key = ("mx", domain)
    cached = _cache_get(key)
    if cached is not None:
        return list(cached)

    records: "list[tuple[int, str]]" = []
    if use_dns_python:
        try:
            import dns.resolver  # type: ignore

            answers = dns.resolver.resolve(domain, "MX", lifetime=8)
            records = sorted(
                ((int(r.preference), str(r.exchange).rstrip(".").lower()) for r in answers),
                key=lambda x: x[0],
            )
        except Exception:  # noqa: BLE001
            records = []

    if not records:
        try:
            data = _doh_query(domain, "MX", http)
            if isinstance(data, dict):
                if data.get("Status") == 3:  # NXDOMAIN
                    return _cache_put(key, [])
                records = _parse_mx(data.get("Answer"))
        except Exception:  # noqa: BLE001
            records = []

    if not records and shutil.which("nslookup"):
        records = _nslookup_mx(domain)

    return _cache_put(key, records)


def _nslookup_mx(domain: str) -> "list[tuple[int, str]]":
    try:
        proc = subprocess.run(
            ["nslookup", "-type=MX", domain],
            capture_output=True, text=True, timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    out = []
    for line in (proc.stdout or "").splitlines():
        if "mail exchanger" not in line.lower():
            continue
        # "example.com MX preference = 10, mail exchanger = mx1.example.com"
        host = line.split("=")[-1].strip().rstrip(".").lower()
        pref = 10
        if "preference" in line.lower():
            try:
                pref = int(line.lower().split("preference")[1].split("=")[1].split(",")[0].strip())
            except (IndexError, ValueError):
                pref = 10
        if host:
            out.append((pref, host))
    out.sort(key=lambda x: x[0])
    return out


def txt_records(domain: str, http) -> "list[str]":
    key = ("txt", domain)
    cached = _cache_get(key)
    if cached is not None:
        return list(cached)
    try:
        data = _doh_query(domain, "TXT", http)
        answers = _parse_txt(data.get("Answer")) if isinstance(data, dict) else []
    except Exception:  # noqa: BLE001
        answers = []
    return _cache_put(key, answers)


def _parse_txt(answer) -> "list[str]":
    out = []
    for rr in answer or []:
        if not isinstance(rr, dict) or rr.get("type") not in (16, "TXT"):
            continue
        data = str(rr.get("data") or "")
        if data.startswith('"') and data.endswith('"'):
            data = data[1:-1]
        out.append(data.replace('" "', ""))
    return out


def mail_posture(domain: str, http) -> dict:
    """Quick sanity check that the domain actually runs mail (MX + SPF hints)."""
    mx = mx_records(domain, http)
    txt = txt_records(domain, http)
    spf = next((t for t in txt if t.lower().startswith("v=spf1")), "")
    providers = []
    lower_mx = " ".join(h for _, h in mx)
    for needle, label in [
        ("google", "Google Workspace"), ("outlook", "Microsoft 365"),
        ("protection.outlook", "Microsoft 365"), ("zoho", "Zoho"),
        ("yandex", "Yandex"), ("protonmail", "Proton"), ("mimecast", "Mimecast"),
        ("proofpoint", "Proofpoint"), ("barracuda", "Barracuda"),
        ("amazonaws", "Amazon WorkMail"), ("messagingengine", "Fastmail"),
        ("qq.com", "Tencent"), ("netease", "NetEase"), ("mail.ru", "Mail.ru"),
    ]:
        if needle in lower_mx:
            providers.append(label)
    return {"mx": mx, "spf": bool(spf), "providers": sorted(set(providers))}
