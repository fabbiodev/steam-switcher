"""Steam level from the official API or the account's public community page."""

from html.parser import HTMLParser
import json
import re
import time
from urllib.parse import urljoin, urlsplit

import requests

from .errors import SteamError, RateLimitError
from .vdf import valid_id


class _Page(HTMLParser):
    def __init__(self):
        super().__init__()
        self.identity_urls, self.values = [], []
        self.depth = 0
        self.profile_depth = None
        self.level_depth = None
        self.text = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "meta" and attrs.get("property") == "og:url":
            self.identity_urls.append(attrs.get("content", ""))
        if tag not in ("meta", "img", "link", "br", "input", "hr", "source", "wbr", "area", "base", "embed", "param", "track", "col"):
            self.depth += 1
        classes = attrs.get("class", "").split()
        if "profile_header_badgeinfo" in classes:
            self.profile_depth = self.depth
        if self.profile_depth is not None and "friendPlayerLevelNum" in classes:
            if self.level_depth is not None:
                raise SteamError("Steam вернул неоднозначный уровень.")
            self.level_depth, self.text = self.depth, []

    def handle_data(self, value):
        if self.level_depth is not None:
            self.text.append(value)

    def handle_endtag(self, tag):
        if tag in ("meta", "img", "link", "br", "input", "hr", "source", "wbr", "area", "base", "embed", "param", "track", "col"):
            return
        if self.level_depth == self.depth:
            self.values.append("".join(self.text).strip())
            self.level_depth = None
        if self.profile_depth == self.depth:
            self.profile_depth = None
        self.depth = max(0, self.depth - 1)


def parse_public_level(html, steam_id):
    if not valid_id(steam_id) or len(html) > 2 * 1024 * 1024:
        raise SteamError("Steam вернул некорректную страницу профиля.")
    page = _Page()
    try:
        page.feed(html)
        identity = None
        match = re.search(r"\bg_rgProfileData\s*=\s*", html)
        if match:
            profile, _ = json.JSONDecoder().raw_decode(html[match.end():])
            identity = profile.get("steamid") if isinstance(profile, dict) else None
        if identity is not None and str(identity) != steam_id:
            raise SteamError("Steam вернул страницу другого аккаунта.")
        verified = identity is not None or any(url.rstrip("/") == f"https://steamcommunity.com/profiles/{steam_id}" for url in page.identity_urls)
        if not verified or len(page.values) != 1 or not re.fullmatch(r"\d{1,6}", page.values[0]):
            raise SteamError("Уровень скрыт или страница Steam недоступна. Можно указать Steam Web API key в настройках.")
        return int(page.values[0])
    except (ValueError, TypeError, RecursionError):
        raise SteamError("Не удалось прочитать уровень Steam.") from None


def fetch_level(steam_id, api_key="", session_factory=requests.Session):
    if not valid_id(steam_id):
        raise SteamError("Некорректный SteamID.")
    try:
        with session_factory() as session:
            session.trust_env = False
            if api_key:
                response = session.get("https://api.steampowered.com/IPlayerService/GetSteamLevel/v1/",
                                       params={"key": api_key, "steamid": steam_id}, timeout=(5, 15), allow_redirects=False)
                if response.status_code in (401, 403):
                    raise SteamError("Steam отклонил API-ключ. Проверьте его в настройках.")
            else:
                url = f"https://steamcommunity.com/profiles/{steam_id}/?l=english"
                for _ in range(5):
                    parsed = urlsplit(url)
                    if (parsed.scheme != "https" or parsed.hostname != "steamcommunity.com" or parsed.username
                            or parsed.password or parsed.port not in (None, 443)
                            or not parsed.path.startswith(("/id/", "/profiles/"))):
                        raise SteamError("Steam перенаправил запрос за пределы страницы профиля.")
                    response = session.get(url, timeout=(5, 15), allow_redirects=False)
                    if response.status_code not in (301, 302, 303, 307, 308):
                        break
                    location = response.headers.get("Location")
                    if not location:
                        raise SteamError("Steam не предоставил адрес профиля.")
                    url = urljoin(url, location)
                else:
                    raise SteamError("Steam не завершил перенаправление профиля.")
            if response.status_code == 429:
                try:
                    delay = int(response.headers.get("Retry-After", "300"))
                except (TypeError, ValueError):
                    delay = 300
                raise RateLimitError(delay)
            if response.status_code != 200:
                raise SteamError("Steam временно не предоставил уровень аккаунта.")
            if len(response.content) > 2 * 1024 * 1024:
                raise SteamError("Ответ Steam слишком велик.")
            if api_key:
                data = response.json()
                level = data.get("response", {}).get("player_level")
                if type(level) is not int or not 0 <= level <= 999999:
                    raise SteamError("Steam API не предоставил уровень. Предыдущие данные сохранены.")
            else:
                level = parse_public_level(response.text, steam_id)
            return {"level": level, "source": "Steam Web API" if api_key else "Steam Community", "updated_at": time.time()}
    except (requests.RequestException, ValueError, AttributeError):
        raise SteamError("Не удалось связаться со Steam. Предыдущий уровень сохранён.") from None
