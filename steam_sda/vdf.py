"""Bounded KeyValues reader and lossless updates of Steam's account selector."""

from dataclasses import dataclass, field
import re

from .errors import SteamError

STEAM_ID_BASE = 76561197960265728


def valid_id(value):
    return isinstance(value, str) and value.isascii() and value.isdigit() and 0 < int(value) - STEAM_ID_BASE <= 0xFFFFFFFF


@dataclass
class Token:
    value: str
    start: int
    end: int


@dataclass
class Node:
    key: Token
    value: Token | None = None
    children: list = field(default_factory=list)
    closing: int = 0

    def get(self, name):
        values = [n for n in self.children if n.key.value.casefold() == name.casefold()]
        if len(values) > 1:
            raise SteamError("В файле Steam есть повторяющиеся поля. Перезапустите Steam и обновите список.")
        return values[0] if values else None

    def text(self, name, default=""):
        node = self.get(name)
        return node.value.value if node and node.value else default


def _tokens(text):
    if len(text) > 4 * 1024 * 1024:
        raise SteamError("Файл аккаунтов Steam слишком велик.")
    pos = count = 0
    while pos < len(text):
        if text[pos].isspace() or text[pos] == "\ufeff":
            pos += 1
            continue
        if text.startswith("//", pos):
            end = text.find("\n", pos)
            pos = len(text) if end < 0 else end + 1
            continue
        start = pos
        if text[pos] in "{}":
            pos += 1
            value = text[start:pos]
        elif text[pos] == '"':
            pos += 1
            chars = []
            while pos < len(text) and text[pos] != '"':
                if text[pos] == "\\" and pos + 1 < len(text) and text[pos + 1] in ('"', "\\"):
                    pos += 1
                chars.append(text[pos])
                pos += 1
            if pos >= len(text):
                raise SteamError("Не удалось прочитать файл аккаунтов Steam.")
            pos += 1
            value = "".join(chars)
        else:
            while pos < len(text) and not text[pos].isspace() and text[pos] not in '{}"':
                pos += 1
            if start == pos:
                raise SteamError("Не удалось прочитать файл аккаунтов Steam.")
            value = text[start:pos]
        count += 1
        if count > 100000:
            raise SteamError("В файле Steam слишком много записей.")
        yield Token(value, start, pos)


def parse(raw, root_name="users"):
    try:
        text = raw.decode("utf-8") if isinstance(raw, bytes) else raw
        tokens = list(_tokens(text))
        index = 0

        def block(depth=0, nested=False):
            nonlocal index
            if depth > 16:
                raise ValueError()
            nodes = []
            while index < len(tokens):
                key = tokens[index]
                index += 1
                if key.value == "}":
                    if not nested:
                        raise ValueError()
                    return nodes, key.start
                if key.value == "{" or index >= len(tokens):
                    raise ValueError()
                token = tokens[index]
                index += 1
                if token.value == "{":
                    children, closing = block(depth + 1, True)
                    nodes.append(Node(key, children=children, closing=closing))
                elif token.value == "}":
                    raise ValueError()
                else:
                    nodes.append(Node(key, token))
            if nested:
                raise ValueError()
            return nodes, len(text)

        roots, _ = block()
        users = [n for n in roots if n.key.value.casefold() == root_name.casefold() and n.value is None]
        if len(users) != 1:
            raise ValueError()
        return text, users[0]
    except (ValueError, UnicodeError, TypeError):
        raise SteamError("Не удалось прочитать loginusers.vdf. Откройте Steam и войдите в аккаунт.") from None


def accounts(raw):
    _, users = parse(raw)
    result, seen = [], set()
    for node in users.children:
        steam_id = node.key.value
        if not valid_id(steam_id) or steam_id in seen or node.value is not None:
            raise SteamError("Steam вернул некорректный список аккаунтов.")
        seen.add(steam_id)
        login = node.text("AccountName")
        if not login or len(login) > 128 or any(ord(c) < 32 for c in login):
            raise SteamError("В аккаунте Steam отсутствует имя входа.")
        stamp = node.text("Timestamp", "0")
        result.append({"steam_id": steam_id, "login": login,
                       "name": node.text("PersonaName") or login,
                       "remembered": node.text("RememberPassword") == "1" and node.text("AllowAutoLogin", "1") == "1",
                       "recent": node.text("MostRecent") == "1",
                       "last_login": int(stamp) if re.fullmatch(r"\d{1,12}", stamp) else 0})
    return result


def select_account(raw, steam_id):
    rows = accounts(raw)
    if not valid_id(steam_id) or not any(a["steam_id"] == steam_id for a in rows):
        raise SteamError("Аккаунт исчез из списка Steam. Обновите список.")
    text, users = parse(raw)
    newline = "\r\n" if "\r\n" in text else "\n"
    edits = []
    for node in users.children:
        value = "1" if node.key.value == steam_id else "0"
        auto = node.get("AutoLogin")
        if auto:
            if auto.value is None:
                raise SteamError("Некорректное поле AutoLogin в файле Steam.")
            edits.append((auto.value.start, auto.value.end, '"' + value + '"'))
        recent = node.get("MostRecent")
        if recent:
            if recent.value is None:
                raise SteamError("Некорректное поле MostRecent в файле Steam.")
            edits.append((recent.value.start, recent.value.end, '"' + value + '"'))
        else:
            # Insert before the closing brace without reserializing other fields.
            line_start = text.rfind("\n", 0, node.closing) + 1
            indent = text[line_start:node.closing]
            if indent.strip():
                line_start = node.closing
                indent = "\t"
                insertion = newline + indent + '\t"MostRecent"\t\t"' + value + '"' + newline + indent
            else:
                insertion = indent + '\t"MostRecent"\t\t"' + value + '"' + newline
            edits.append((line_start, line_start, insertion))
    for start, end, value in sorted(edits, reverse=True):
        text = text[:start] + value + text[end:]
    patched = text.encode("utf-8")
    result = accounts(patched)
    if [a["steam_id"] for a in result if a["recent"]] != [steam_id]:
        raise SteamError("Не удалось выбрать аккаунт в файле Steam.")
    return patched


def chooser_preference(raw, value=None):
    """Patch only Steam's existing account-chooser preference, losslessly."""
    text, root = parse(raw, "InstallConfigStore")
    node = root
    for name in ("WebStorage", "Auth", "AlwaysShowUserChooser"):
        node = node.get(name)
        if node is None:
            return None if value is None else raw
    if node.value is None or node.value.value not in ("0", "1"):
        raise SteamError("Неизвестный формат настройки выбора аккаунта Steam.")
    if value is None:
        return node.value.value
    if value not in ("0", "1"):
        raise SteamError("Некорректная настройка выбора аккаунта Steam.")
    return (text[:node.value.start] + '"' + value + '"' + text[node.value.end:]).encode("utf-8")


def quote(value):
    if not isinstance(value, str) or any(ord(c) < 32 for c in value):
        raise SteamError("Недопустимое значение настройки Steam.")
    return '"' + value.replace('\\', '\\\\').replace('"', '\\"') + '"'


def read_value(raw, root_name, path):
    _, node = parse(raw, root_name)
    for key in path:
        node = node.get(key)
        if node is None:
            return None
    if node.value is None:
        raise SteamError("Неизвестный формат настройки Steam.")
    return node.value.value


def write_value(raw, root_name, path, value):
    """Update one scalar or create its missing parents without reserialization."""
    text, node = parse(raw, root_name)
    for index, key in enumerate(path):
        child = node.get(key)
        if child is None:
            if value is None:
                return raw
            addition = quote(path[-1]) + '\t' + quote(value)
            for parent in reversed(path[index:-1]):
                addition = quote(parent) + '\n{\n' + addition + '\n}'
            return (text[:node.closing] + '\n' + addition + '\n' + text[node.closing:]).encode('utf-8')
        if index == len(path) - 1:
            if child.value is None:
                raise SteamError("Неизвестный формат настройки Steam.")
            if value is None:
                return (text[:child.key.start] + text[child.value.end:]).encode('utf-8')
            return (text[:child.value.start] + quote(value) + text[child.value.end:]).encode('utf-8')
        if child.value is not None:
            raise SteamError("Неизвестный формат раздела настроек Steam.")
        node = child
    raise SteamError("Не указан путь настройки Steam.")


def authenticated_account(raw, account):
    """Add/select only a SteamKit-verified account; preserve every other account."""
    from .accounts import metadata
    account = metadata(account)
    sid, login = account['steam_id'], account['login']
    rows = accounts(raw)
    if any(a['steam_id'] != sid and a['login'].casefold() == login.casefold() for a in rows):
        raise SteamError("Этот логин Steam связан с другим SteamID.")
    existing = next((a for a in rows if a['steam_id'] == sid), None)
    if existing and existing['login'].casefold() != login.casefold():
        raise SteamError("Логин Steam изменился во время входа.")
    fields = {'AccountName': login, 'PersonaName': account['name'], 'RememberPassword': '1',
              'AllowAutoLogin': '1', 'AutoLogin': '1', 'MostRecent': '1'}
    for name, value in fields.items():
        if name == 'PersonaName' and existing:
            continue
        raw = write_value(raw, 'users', [sid, name], value)
    return select_account(raw, sid)
