"""Tarayıcı bilgisinden (User-Agent) okunur cihaz adı — saf (Yazılım Dokümanı PLT-10).

Yalnız gösterim içindir; güvenlik kararı buna dayanmaz (başlık istemcinin elindedir).
"""

# Sıra önemli: Edge ve Opera başlıkta "Chrome", Chrome da "Safari" geçirir.
_BROWSERS = (
    ("Edg/", "Edge"),
    ("OPR/", "Opera"),
    ("SamsungBrowser/", "Samsung Internet"),
    ("Firefox/", "Firefox"),
    ("CriOS/", "Chrome"),
    ("Chrome/", "Chrome"),
    ("Safari/", "Safari"),
)
_SYSTEMS = (
    ("iPhone", "iPhone"),
    ("iPad", "iPad"),
    ("Android", "Android"),
    ("Windows", "Windows"),
    ("Mac OS X", "macOS"),
    ("CrOS", "ChromeOS"),
    ("Linux", "Linux"),
)
UNKNOWN = "Bilinmeyen cihaz"


def describe_device(user_agent: str | None) -> str:
    """describe_device("Mozilla/5.0 (Windows NT 10.0; …) Chrome/128.0 Safari/537.36")
    → "Chrome · Windows"."""
    if not user_agent:
        return UNKNOWN
    browser = next((name for token, name in _BROWSERS if token in user_agent), None)
    system = next((name for token, name in _SYSTEMS if token in user_agent), None)
    parts = [p for p in (browser, system) if p]
    return " · ".join(parts) if parts else UNKNOWN
