# osint-tool-v1
Educational purposes only


## OSINT Tool: Username / Phone / Email Investigation (Ethical Use Only)
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

Notes & limitations (be read before running)

Heuristic results: Most public websites and messaging endpoints do not give a definitive, API-style answer to existence. This tool uses conservative heuristics and marks many results as "manual verification required" where appropriate.

Rate-limiting: The tool includes delays and retries, but hitting many sites quickly will still trigger rate limits. Use responsibly.

Privacy & legality: Only investigate accounts you own or have explicit permission to test.

HIBP: You need a HIBP API key for automated breach checks. Without it the tool will not attempt to scrape or otherwise bypass the API — it will instruct you to provide the key.

# Quick usage examples

## Check a username:

```python3 osint_tool.py -u alice --workers 20 --verbose```


Check phone and email (with HIBP key):

```python3 osint_tool.py -p "+447700900000" -e "me@example.com" --hibp-key MY_KEY --proxies-file proxies.txt -o out.json```


Proxy file format (proxies.txt): one proxy per line, e.g.

```http://user:pass@1.2.3.4:8080```
```5.6.7.8:312```
