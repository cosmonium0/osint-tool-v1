import argparse
import requests
import time
import random
import re
import json
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import quote
from requests.adapters import HTTPAdapter, Retry
from datetime import datetime
from typing import List, Dict, Optional

# -----------------------------
# Configuration / Platform lists
# -----------------------------
DEFAULT_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 " \
                     "(KHTML, like Gecko) Chrome/120.0 Safari/537.36 OSINTTool/1.0"

# Wide (extensible) platform templates. Add or remove entries as needed.
PLATFORMS = {
    # mainstream social
    "twitter": "https://twitter.com/{username}",
    "instagram": "https://www.instagram.com/{username}/",
    "facebook": "https://www.facebook.com/{username}",
    "linkedin": "https://www.linkedin.com/in/{username}",
    "github": "https://github.com/{username}",
    "reddit": "https://www.reddit.com/user/{username}",
    "youtube": "https://www.youtube.com/{username}",
    "tiktok": "https://www.tiktok.com/@{username}",
    "pinterest": "https://www.pinterest.com/{username}/",

    # streaming / gaming
    "steam": "https://steamcommunity.com/id/{username}",
    "twitch": "https://www.twitch.tv/{username}",
    "itchio": "https://{username}.itch.io/",
    "roblox": "https://www.roblox.com/users/{username}/profile",

    # forums / developer / work
    "hackernews": "https://news.ycombinator.com/user?id={username}",
    "stackoverflow": "https://stackoverflow.com/users/{username}",
    "medium": "https://medium.com/@{username}",
    "keybase": "https://keybase.io/{username}",
    "crunchbase": "https://www.crunchbase.com/person/{username}",

    # image / portfolio
    "imgur": "https://imgur.com/user/{username}",
    "deviantart": "https://www.deviantart.com/{username}",
    "behance": "https://www.behance.net/{username}",
    "dribbble": "https://dribbble.com/{username}",

    # misc
    "vk": "https://vk.com/{username}",
    "mastodon": "https://{username}.social",  # user provided instance may be required
}

# messaging endpoints (note: many of these are not reliably queryable via HTTP)
MESSAGING_ENDPOINTS = {
    "whatsapp": "https://wa.me/{phone}",          # returns a page for valid link; not definitive for existence
    "telegram": "https://t.me/{phone_or_username}",
    "signal": "https://signal.me/#p/{phone}",
    "skype": "https://api.skype.com/users/{phone_or_username}",  # legacy API - may not work
    # NOTE: scheme-based URIs (viber://, weixin://) are excluded from automated HTTP checks
}

# -----------------------------
# Utilities
# -----------------------------
def now_ts():
    return datetime.utcnow().isoformat() + "Z"

def sanitize_phone(phone: str) -> str:
    """Keep digits and plus sign only, returns digits only for endpoints that require it."""
    if not phone:
        return ""
    # allow leading plus but core is digits
    cleaned = re.sub(r"[^\d\+]", "", phone.strip())
    return cleaned

def build_session(timeout: int = 15, proxies: Optional[dict] = None) -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": DEFAULT_USER_AGENT})
    retries = Retry(total=3, backoff_factor=0.8, status_forcelist=(429, 500, 502, 503, 504))
    s.mount("https://", HTTPAdapter(max_retries=retries))
    s.mount("http://", HTTPAdapter(max_retries=retries))
    s.timeout = timeout
    if proxies:
        s.proxies.update(proxies)
    return s

# -----------------------------
# Core OSINTTool class
# -----------------------------
class OSINTTool:
    def __init__(self, workers: int = 15, min_delay: float = 0.3, max_delay: float = 1.5,
                 timeout: int = 15, proxies_list: Optional[List[str]] = None, verbose: bool = False):
        self.workers = max(1, workers)
        self.min_delay = min_delay
        self.max_delay = max_delay
        self.timeout = timeout
        self.verbose = verbose
        self.proxies_list = proxies_list or []
        self._next_proxy_index = 0

    def _get_next_proxy(self) -> Optional[dict]:
        """Return next proxy dict for requests or None."""
        if not self.proxies_list:
            return None
        # rotate in round-robin
        proxy_url = self.proxies_list[self._next_proxy_index % len(self.proxies_list)]
        self._next_proxy_index += 1
        # Support proxy_url format: http://user:pass@host:port or host:port
        if not proxy_url.startswith("http"):
            proxy_url = "http://" + proxy_url
        return {"http": proxy_url, "https": proxy_url}

    def _random_delay(self):
        time.sleep(random.uniform(self.min_delay, self.max_delay))

    # ---------- Username checks ----------
    def check_username(self, username: str, platforms: Optional[Dict[str, str]] = None) -> Dict:
        username = username.strip()
        results = {"username": username, "checked": {}, "timestamp": now_ts()}

        all_platforms = platforms or PLATFORMS

        def _check(name, template):
            local_result = {"exists": None, "http_status": None, "url": None, "note": None}
            try:
                url = template.format(username=quote(username))
                local_result["url"] = url

                # choose a proxy for this request if configured
                proxy = self._get_next_proxy()
                session = build_session(timeout=self.timeout, proxies=proxy)

                # HEAD first (faster, many sites reply to HEAD). If HEAD not allowed, fallback to GET.
                try:
                    r = session.head(url, allow_redirects=True, timeout=self.timeout)
                    status = r.status_code
                    content = r.text.lower() if r.text else ""
                except Exception:
                    r = session.get(url, allow_redirects=True, timeout=self.timeout)
                    status = r.status_code
                    content = r.text.lower() if r.text else ""

                local_result["http_status"] = status

                # heuristics:
                if status == 200:
                    # look for obvious "not found" text that some platforms include
                    if any(k in content for k in ["not found", "page not found", "user not found", "doesn't exist", "404"]):
                        local_result["exists"] = False
                    else:
                        local_result["exists"] = True
                elif status in (301, 302):
                    # follow redirects usually indicates page/placeholder — treat as possibly existing
                    local_result["exists"] = True
                elif status == 404:
                    local_result["exists"] = False
                elif status == 429:
                    local_result["exists"] = None
                    local_result["note"] = "Rate limited (429)"
                else:
                    local_result["exists"] = None
                    local_result["note"] = f"HTTP {status}"

                if self.verbose:
                    print(f"[username:{name}] {url} -> {local_result['http_status']} exists={local_result['exists']}")

            except Exception as e:
                local_result["note"] = f"error: {e}"
                local_result["exists"] = None

            # random delay to be polite
            self._random_delay()
            return name, local_result

        with ThreadPoolExecutor(max_workers=self.workers) as exe:
            futures = [exe.submit(_check, n, t) for n, t in all_platforms.items()]
            for fut in as_completed(futures):
                name, res = fut.result()
                results["checked"][name] = res

        return results

    # ---------- Phone checks ----------
    def check_phone(self, phone: str) -> Dict:
        phone_raw = phone.strip()
        cleaned = sanitize_phone(phone_raw)
        results = {"phone": phone_raw, "cleaned": cleaned, "checked": {}, "timestamp": now_ts()}

        def _check(app_name, template):
            res = {"url": None, "http_status": None, "exists": None, "note": None}
            try:
                # Some templates expect the phone in international form without plus
                phone_payload = cleaned.lstrip("+")
                url = template.format(phone=quote(cleaned), phone_or_username=quote(phone_payload))
                res["url"] = url

                # Many messaging apps do not expose existence via public HTTP endpoints.
                # This tool will attempt a conservative HTTP GET where sensible, otherwise mark manual.
                if url.startswith("http"):
                    proxy = self._get_next_proxy()
                    session = build_session(timeout=self.timeout, proxies=proxy)
                    r = session.get(url, allow_redirects=True, timeout=self.timeout)
                    res["http_status"] = r.status_code
                    # heuristics: 200 might indicate a reachable page, but not necessarily an account
                    if r.status_code == 200:
                        res["exists"] = None
                        res["note"] = "Page reachable — manual verification required (cannot assert existence)"
                    elif r.status_code == 404:
                        res["exists"] = False
                    elif r.status_code in (301, 302):
                        res["exists"] = None
                        res["note"] = "Redirected — manual verification recommended"
                    else:
                        res["exists"] = None
                        res["note"] = f"HTTP {r.status_code}"
                else:
                    # Non-http schemes (viber:// etc.) are not checked automatically
                    res["note"] = "Non-HTTP scheme or manual check required"
                    res["exists"] = None

            except Exception as e:
                res["note"] = f"error: {e}"
                res["exists"] = None

            self._random_delay()
            return app_name, res

        with ThreadPoolExecutor(max_workers=max(3, self.workers // 3)) as exe:
            futures = [exe.submit(_check, n, t) for n, t in MESSAGING_ENDPOINTS.items()]
            for fut in as_completed(futures):
                name, res = fut.result()
                results["checked"][name] = res

        return results

    # ---------- Email breach checks ----------
    def check_email_breaches(self, email: str, hibp_api_key: Optional[str] = None) -> Dict:
        """
        Check email against HaveIBeenPwned if api key provided. If not, returns guidance string.
        NOTE: HIBP requires a valid API key for breachedaccount endpoint. We do not include an API key.
        """
        results = {"email": email, "breaches": [], "errors": [], "timestamp": now_ts()}
        email = email.strip()
        if not email or "@" not in email:
            results["errors"].append("Invalid email address format")
            return results

        if not hibp_api_key:
            results["errors"].append("No HIBP API key provided. To check breaches, pass --hibp-key <KEY>.")
            results["note"] = ("If you have a HaveIBeenPwned API key, rerun with --hibp-key. "
                               "Without an API key this tool cannot perform automated breach checks.")
            return results

        # Call HIBP /breachedaccount/{account}
        try:
            url = f"https://haveibeenpwned.com/api/v3/breachedaccount/{quote(email)}"
            headers = {
                "User-Agent": DEFAULT_USER_AGENT,
                "hibp-api-key": hibp_api_key,
                "Accept": "application/json"
            }
            proxy = self._get_next_proxy()
            session = build_session(timeout=self.timeout, proxies=proxy)
            r = session.get(url, headers=headers, timeout=self.timeout, params={"truncateResponse": "false"})
            if r.status_code == 200:
                data = r.json()
                for b in data:
                    # Keep minimal info to avoid leaking PII; user asked about this email so it's fine to show Name & Date
                    results["breaches"].append({"name": b.get("Name"), "date": b.get("BreachDate")})
            elif r.status_code == 404:
                results["note"] = "No breaches found for this email according to HIBP"
            elif r.status_code == 401:
                results["errors"].append("Unauthorized (invalid HIBP API key)")
            else:
                results["errors"].append(f"HIBP returned HTTP {r.status_code}")
        except Exception as e:
            results["errors"].append(f"error: {e}")

        return results

# -----------------------------
# CLI / Runner
# -----------------------------
def load_proxies_from_file(path: str) -> List[str]:
    proxies = []
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                ln = line.strip()
                if ln:
                    proxies.append(ln)
    except Exception as e:
        print(f"Warning: could not load proxy file: {e}", file=sys.stderr)
    return proxies

def main():
    parser = argparse.ArgumentParser(description="OSINT Tool - username/phone/email investigation (ethical use only)")
    parser.add_argument("--username", "-u", help="username to check", type=str)
    parser.add_argument("--phone", "-p", help="phone number to check (international format recommended)", type=str)
    parser.add_argument("--email", "-e", help="email to check for breaches (requires HIBP API key)", type=str)
    parser.add_argument("--hibp-key", help="HaveIBeenPwned API key (optional)", type=str, default=None)
    parser.add_argument("--workers", help="max concurrency workers (default 15)", type=int, default=15)
    parser.add_argument("--min-delay", help="min random delay between requests (seconds)", type=float, default=0.3)
    parser.add_argument("--max-delay", help="max random delay between requests (seconds)", type=float, default=1.5)
    parser.add_argument("--timeout", help="request timeout seconds", type=int, default=15)
    parser.add_argument("--proxies-file", help="file with proxy URLs (one per line) for rotation (optional)", type=str)
    parser.add_argument("--output", "-o", help="output json file (default stdout)", type=str)
    parser.add_argument("--verbose", "-v", help="verbose logging", action="store_true")

    args = parser.parse_args()

    # Safety: require at least one target
    if not (args.username or args.phone or args.email):
        parser.error("Provide at least one of --username, --phone, or --email")

    proxies = load_proxies_from_file(args.proxies_file) if args.proxies_file else None

    tool = OSINTTool(
        workers=args.workers,
        min_delay=args.min_delay,
        max_delay=args.max_delay,
        timeout=args.timeout,
        proxies_list=proxies or [],
        verbose=args.verbose
    )

    aggregated = {"meta": {"started": now_ts(), "args": vars(args)}, "results": {}}

    try:
        if args.username:
            if args.verbose:
                print(f"[+] Checking username: {args.username}")
            aggregated["results"]["username_check"] = tool.check_username(args.username)

        if args.phone:
            if args.verbose:
                print(f"[+] Checking phone: {args.phone}")
            aggregated["results"]["phone_check"] = tool.check_phone(args.phone)

        if args.email:
            if args.verbose:
                print(f"[+] Checking email breaches: {args.email}")
            aggregated["results"]["email_b#!/usr/bin/env python3
"""
OSINT Tool: Username / Phone / Email Investigation (Ethical Use Only)
Author: OSINT Tool (improved)
Purpose:
  - Check usernames across many public platforms (non-invasive)
  - Check phone numbers against messaging URL endpoints (non-invasive, many require manual verification)
  - Query breach APIs (HaveIBeenPwned) if API key provided (optional)
Features:
  - CLI: username / phone / email modes (any combination)
  - Concurrency with ThreadPoolExecutor
  - Requests session pooling + retries
  - Proxy rotation from file (optional)
  - Rate limiting / random delays and exponential backoff
  - Safe defaults and explicit ethical usage notice
IMPORTANT:
  - This tool is intended for legitimate OSINT, security research, or account recovery on accounts you own or are authorized to test.
  - Do NOT use this tool for harassment, stalking, doxxing, account takeover, or any illegal activity.
  - The author / distributor is not responsible for misuse.
"""
import argparse
import requests
import time
import random
import re
import json
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import quote
from requests.adapters import HTTPAdapter, Retry
from datetime import datetime
from typing import List, Dict, Optional

# -----------------------------
# Configuration / Platform lists
# -----------------------------
DEFAULT_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 " \
                     "(KHTML, like Gecko) Chrome/120.0 Safari/537.36 OSINTTool/1.0"

# Wide (extensible) platform templates. Add or remove entries as needed.
PLATFORMS = {
    # mainstream social
    "twitter": "https://twitter.com/{username}",
    "instagram": "https://www.instagram.com/{username}/",
    "facebook": "https://www.facebook.com/{username}",
    "linkedin": "https://www.linkedin.com/in/{username}",
    "github": "https://github.com/{username}",
    "reddit": "https://www.reddit.com/user/{username}",
    "youtube": "https://www.youtube.com/{username}",
    "tiktok": "https://www.tiktok.com/@{username}",
    "pinterest": "https://www.pinterest.com/{username}/",

    # streaming / gaming
    "steam": "https://steamcommunity.com/id/{username}",
    "twitch": "https://www.twitch.tv/{username}",
    "itchio": "https://{username}.itch.io/",
    "roblox": "https://www.roblox.com/users/{username}/profile",

    # forums / developer / work
    "hackernews": "https://news.ycombinator.com/user?id={username}",
    "stackoverflow": "https://stackoverflow.com/users/{username}",
    "medium": "https://medium.com/@{username}",
    "keybase": "https://keybase.io/{username}",
    "crunchbase": "https://www.crunchbase.com/person/{username}",

    # image / portfolio
    "imgur": "https://imgur.com/user/{username}",
    "deviantart": "https://www.deviantart.com/{username}",
    "behance": "https://www.behance.net/{username}",
    "dribbble": "https://dribbble.com/{username}",

    # misc
    "vk": "https://vk.com/{username}",
    "mastodon": "https://{username}.social",  # user provided instance may be required
}

# messaging endpoints (note: many of these are not reliably queryable via HTTP)
MESSAGING_ENDPOINTS = {
    "whatsapp": "https://wa.me/{phone}",          # returns a page for valid link; not definitive for existence
    "telegram": "https://t.me/{phone_or_username}",
    "signal": "https://signal.me/#p/{phone}",
    "skype": "https://api.skype.com/users/{phone_or_username}",  # legacy API - may not work
    # NOTE: scheme-based URIs (viber://, weixin://) are excluded from automated HTTP checks
}

# -----------------------------
# Utilities
# -----------------------------
def now_ts():
    return datetime.utcnow().isoformat() + "Z"

def sanitize_phone(phone: str) -> str:
    """Keep digits and plus sign only, returns digits only for endpoints that require it."""
    if not phone:
        return ""
    # allow leading plus but core is digits
    cleaned = re.sub(r"[^\d\+]", "", phone.strip())
    return cleaned

def build_session(timeout: int = 15, proxies: Optional[dict] = None) -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": DEFAULT_USER_AGENT})
    retries = Retry(total=3, backoff_factor=0.8, status_forcelist=(429, 500, 502, 503, 504))
    s.mount("https://", HTTPAdapter(max_retries=retries))
    s.mount("http://", HTTPAdapter(max_retries=retries))
    s.timeout = timeout
    if proxies:
        s.proxies.update(proxies)
    return s

# -----------------------------
# Core OSINTTool class
# -----------------------------
class OSINTTool:
    def __init__(self, workers: int = 15, min_delay: float = 0.3, max_delay: float = 1.5,
                 timeout: int = 15, proxies_list: Optional[List[str]] = None, verbose: bool = False):
        self.workers = max(1, workers)
        self.min_delay = min_delay
        self.max_delay = max_delay
        self.timeout = timeout
        self.verbose = verbose
        self.proxies_list = proxies_list or []
        self._next_proxy_index = 0

    def _get_next_proxy(self) -> Optional[dict]:
        """Return next proxy dict for requests or None."""
        if not self.proxies_list:
            return None
        # rotate in round-robin
        proxy_url = self.proxies_list[self._next_proxy_index % len(self.proxies_list)]
        self._next_proxy_index += 1
        # Support proxy_url format: http://user:pass@host:port or host:port
        if not proxy_url.startswith("http"):
            proxy_url = "http://" + proxy_url
        return {"http": proxy_url, "https": proxy_url}

    def _random_delay(self):
        time.sleep(random.uniform(self.min_delay, self.max_delay))

    # ---------- Username checks ----------
    def check_username(self, username: str, platforms: Optional[Dict[str, str]] = None) -> Dict:
        username = username.strip()
        results = {"username": username, "checked": {}, "timestamp": now_ts()}

        all_platforms = platforms or PLATFORMS

        def _check(name, template):
            local_result = {"exists": None, "http_status": None, "url": None, "note": None}
            try:
                url = template.format(username=quote(username))
                local_result["url"] = url

                # choose a proxy for this request if configured
                proxy = self._get_next_proxy()
                session = build_session(timeout=self.timeout, proxies=proxy)

                # HEAD first (faster, many sites reply to HEAD). If HEAD not allowed, fallback to GET.
                try:
                    r = session.head(url, allow_redirects=True, timeout=self.timeout)
                    status = r.status_code
                    content = r.text.lower() if r.text else ""
                except Exception:
                    r = session.get(url, allow_redirects=True, timeout=self.timeout)
                    status = r.status_code
                    content = r.text.lower() if r.text else ""

                local_result["http_status"] = status

                # heuristics:
                if status == 200:
                    # look for obvious "not found" text that some platforms include
                    if any(k in content for k in ["not found", "page not found", "user not found", "doesn't exist", "404"]):
                        local_result["exists"] = False
                    else:
                        local_result["exists"] = True
                elif status in (301, 302):
                    # follow redirects usually indicates page/placeholder — treat as possibly existing
                    local_result["exists"] = True
                elif status == 404:
                    local_result["exists"] = False
                elif status == 429:
                    local_result["exists"] = None
                    local_result["note"] = "Rate limited (429)"
                else:
                    local_result["exists"] = None
                    local_result["note"] = f"HTTP {status}"

                if self.verbose:
                    print(f"[username:{name}] {url} -> {local_result['http_status']} exists={local_result['exists']}")

            except Exception as e:
                local_result["note"] = f"error: {e}"
                local_result["exists"] = None

            # random delay to be polite
            self._random_delay()
            return name, local_result

        with ThreadPoolExecutor(max_workers=self.workers) as exe:
            futures = [exe.submit(_check, n, t) for n, t in all_platforms.items()]
            for fut in as_completed(futures):
                name, res = fut.result()
                results["checked"][name] = res

        return results

    # ---------- Phone checks ----------
    def check_phone(self, phone: str) -> Dict:
        phone_raw = phone.strip()
        cleaned = sanitize_phone(phone_raw)
        results = {"phone": phone_raw, "cleaned": cleaned, "checked": {}, "timestamp": now_ts()}

        def _check(app_name, template):
            res = {"url": None, "http_status": None, "exists": None, "note": None}
            try:
                # Some templates expect the phone in international form without plus
                phone_payload = cleaned.lstrip("+")
                url = template.format(phone=quote(cleaned), phone_or_username=quote(phone_payload))
                res["url"] = url

                # Many messaging apps do not expose existence via public HTTP endpoints.
                # This tool will attempt a conservative HTTP GET where sensible, otherwise mark manual.
                if url.startswith("http"):
                    proxy = self._get_next_proxy()
                    session = build_session(timeout=self.timeout, proxies=proxy)
                    r = session.get(url, allow_redirects=True, timeout=self.timeout)
                    res["http_status"] = r.status_code
                    # heuristics: 200 might indicate a reachable page, but not necessarily an account
                    if r.status_code == 200:
                        res["exists"] = None
                        res["note"] = "Page reachable — manual verification required (cannot assert existence)"
                    elif r.status_code == 404:
                        res["exists"] = False
                    elif r.status_code in (301, 302):
                        res["exists"] = None
                        res["note"] = "Redirected — manual verification recommended"
                    else:
                        res["exists"] = None
                        res["note"] = f"HTTP {r.status_code}"
                else:
                    # Non-http schemes (viber:// etc.) are not checked automatically
                    res["note"] = "Non-HTTP scheme or manual check required"
                    res["exists"] = None

            except Exception as e:
                res["note"] = f"error: {e}"
                res["exists"] = None

            self._random_delay()
            return app_name, res

        with ThreadPoolExecutor(max_workers=max(3, self.workers // 3)) as exe:
            futures = [exe.submit(_check, n, t) for n, t in MESSAGING_ENDPOINTS.items()]
            for fut in as_completed(futures):
                name, res = fut.result()
                results["checked"][name] = res

        return results

    # ---------- Email breach checks ----------
    def check_email_breaches(self, email: str, hibp_api_key: Optional[str] = None) -> Dict:
        """
        Check email against HaveIBeenPwned if api key provided. If not, returns guidance string.
        NOTE: HIBP requires a valid API key for breachedaccount endpoint. We do not include an API key.
        """
        results = {"email": email, "breaches": [], "errors": [], "timestamp": now_ts()}
        email = email.strip()
        if not email or "@" not in email:
            results["errors"].append("Invalid email address format")
            return results

        if not hibp_api_key:
            results["errors"].append("No HIBP API key provided. To check breaches, pass --hibp-key <KEY>.")
            results["note"] = ("If you have a HaveIBeenPwned API key, rerun with --hibp-key. "
                               "Without an API key this tool cannot perform automated breach checks.")
            return results

        # Call HIBP /breachedaccount/{account}
        try:
            url = f"https://haveibeenpwned.com/api/v3/breachedaccount/{quote(email)}"
            headers = {
                "User-Agent": DEFAULT_USER_AGENT,
                "hibp-api-key": hibp_api_key,
                "Accept": "application/json"
            }
            proxy = self._get_next_proxy()
            session = build_session(timeout=self.timeout, proxies=proxy)
            r = session.get(url, headers=headers, timeout=self.timeout, params={"truncateResponse": "false"})
            if r.status_code == 200:
                data = r.json()
                for b in data:
                    # Keep minimal info to avoid leaking PII; user asked about this email so it's fine to show Name & Date
                    results["breaches"].append({"name": b.get("Name"), "date": b.get("BreachDate")})
            elif r.status_code == 404:
                results["note"] = "No breaches found for this email according to HIBP"
            elif r.status_code == 401:
                results["errors"].append("Unauthorized (invalid HIBP API key)")
            else:
                results["errors"].append(f"HIBP returned HTTP {r.status_code}")
        except Exception as e:
            results["errors"].append(f"error: {e}")

        return results

# -----------------------------
# CLI / Runner
# -----------------------------
def load_proxies_from_file(path: str) -> List[str]:
    proxies = []
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                ln = line.strip()
                if ln:
                    proxies.append(ln)
    except Exception as e:
        print(f"Warning: could not load proxy file: {e}", file=sys.stderr)
    return proxies

def main():
    parser = argparse.ArgumentParser(description="OSINT Tool - username/phone/email investigation (ethical use only)")
    parser.add_argument("--username", "-u", help="username to check", type=str)
    parser.add_argument("--phone", "-p", help="phone number to check (international format recommended)", type=str)
    parser.add_argument("--email", "-e", help="email to check for breaches (requires HIBP API key)", type=str)
    parser.add_argument("--hibp-key", help="HaveIBeenPwned API key (optional)", type=str, default=None)
    parser.add_argument("--workers", help="max concurrency workers (default 15)", type=int, default=15)
    parser.add_argument("--min-delay", help="min random delay between requests (seconds)", type=float, default=0.3)
    parser.add_argument("--max-delay", help="max random delay between requests (seconds)", type=float, default=1.5)
    parser.add_argument("--timeout", help="request timeout seconds", type=int, default=15)
    parser.add_argument("--proxies-file", help="file with proxy URLs (one per line) for rotation (optional)", type=str)
    parser.add_argument("--output", "-o", help="output json file (default stdout)", type=str)
    parser.add_argument("--verbose", "-v", help="verbose logging", action="store_true")

    args = parser.parse_args()

    # Safety: require at least one target
    if not (args.username or args.phone or args.email):
        parser.error("Provide at least one of --username, --phone, or --email")

    proxies = load_proxies_from_file(args.proxies_file) if args.proxies_file else None

    tool = OSINTTool(
        workers=args.workers,
        min_delay=args.min_delay,
        max_delay=args.max_delay,
        timeout=args.timeout,
        proxies_list=proxies or [],
        verbose=args.verbose
    )

    aggregated = {"meta": {"started": now_ts(), "args": vars(args)}, "results": {}}

    try:
        if args.username:
            if args.verbose:
                print(f"[+] Checking username: {args.username}")
            aggregated["results"]["username_check"] = tool.check_username(args.username)

        if args.phone:
            if args.verbose:
                print(f"[+] Checking phone: {args.phone}")
            aggregated["results"]["phone_check"] = tool.check_phone(args.phone)

        if args.email:
            if args.verbose:
                print(f"[+] Checking email breaches: {args.email}")
            aggregated["results"]["email_breaches"] = tool.check_email_breaches(args.email, hibp_api_key=args.hibp_key)

        aggregated["meta"]["finished"] = now_ts()

        output_json = json.dumps(aggregated, indent=2)
        if args.output:
            with open(args.output, "w", encoding="utf-8") as fh:
                fh.write(output_json)
            print(f"[+] Results written to {args.output}")
        else:
            print(output_json)

    except KeyboardInterrupt:
        print("\n[!] Interrupted by user", file=sys.stderr)
    except Exception as e:
        print(f"[!] Unexpected error: {e}", file=sys.stderr)

if __name__ == "__main__":
    main()
reaches"] = tool.check_email_breaches(args.email, hibp_api_key=args.hibp_key)

        aggregated["meta"]["finished"] = now_ts()

        output_json = json.dumps(aggregated, indent=2)
        if args.output:
            with open(args.output, "w", encoding="utf-8") as fh:
                fh.write(output_json)
            print(f"[+] Results written to {args.output}")
        else:
            print(output_json)

    except KeyboardInterrupt:
        print("\n[!] Interrupted by user", file=sys.stderr)
    except Exception as e:
        print(f"[!] Unexpected error: {e}", file=sys.stderr)

if __name__ == "__main__":
    main()
