from __future__ import annotations
"""Open WebUI file memory tool. Staged for isolated tests; not deployed."""
import json
import os
import re
import stat
import uuid

STATE = '/sandbox-state/state.json'
ROOT = '/sandbox-users'
HOST_ROOT = '/home/ai_service/sandbox/users'
FILES = {'MEMORY': 'MEMORY.md', 'PROFILE': 'PROFILE.md', 'AGENTS': 'AGENTS.md'}
LIMIT = 10000
RECORD_LIMIT = 400

class MemoryErrorSafe(Exception):
    pass

def _uid(user):
    uid = user.get('id') if isinstance(user, dict) else None
    try:
        if not isinstance(uid, str) or str(uuid.UUID(uid)) != uid:
            raise ValueError
    except (ValueError, AttributeError):
        raise MemoryErrorSafe('Пользователь не подтверждён')
    return uid

def _open_dir(user):
    uid = _uid(user)
    with open(STATE, encoding='utf-8') as f:
        state = json.load(f)
    entry = state.get(uid)
    if not isinstance(entry, dict) or entry.get('user_id') != uid or entry.get('active') is not True:
        raise MemoryErrorSafe('Персональная память недоступна')
    slug = entry.get('slug')
    if not isinstance(slug, str) or not re.fullmatch(r'[a-z0-9]+(?:-[a-z0-9]+)*', slug):
        raise MemoryErrorSafe('Некорректная привязка памяти')
    if entry.get('dir') != os.path.join(HOST_ROOT, slug):
        raise MemoryErrorSafe('Некорректная привязка памяти')
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    root = os.open(ROOT, flags)
    try:
        workspace = os.open(slug, flags, dir_fd=root)
        try:
            agents = os.open('.agents', flags, dir_fd=workspace)
        finally:
            os.close(workspace)
    finally:
        os.close(root)
    if os.fstat(agents).st_uid != 0:
        os.close(agents)
        raise MemoryErrorSafe('Нарушена защита памяти')
    return agents

def _read(dfd, name):
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dfd)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_nlink != 1:
            raise MemoryErrorSafe('Небезопасный файл памяти')
        with os.fdopen(fd, 'r', encoding='utf-8', closefd=False) as stream:
            return stream.read()
    finally:
        os.close(fd)

class _Span:
    def __init__(self, text, start):
        self._text = text
        self._start = start
        self._end = start + len(text)
    def group(self):
        return self._text
    def start(self):
        return self._start
    def end(self):
        return self._end

def _records(text, name):
    prefix = '# ' + name + '\n\n'
    if not text.startswith(prefix):
        raise MemoryErrorSafe('Неизвестный формат файла памяти')
    body = text[len(prefix):]
    spans = []
    for block in re.finditer(r'[^\n]+(?:\n(?!\n)[^\n]+)*', body):
        chunk = block.group()
        if chunk.startswith(('## ', '<!--')):
            continue
        if chunk.startswith('- '):
            # Пункт списка — отдельная запись, даже если пункты идут подряд.
            offset = block.start()
            for line in chunk.splitlines():
                if line.startswith('- '):
                    spans.append(_Span(line, offset))
                offset += len(line) + 1
            continue
        spans.append(_Span(chunk, block.start()))
    return prefix, body, spans

def _store(dfd, name, text):
    temp = '.write-' + uuid.uuid4().hex
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644, dir_fd=dfd)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp, name, src_dir_fd=dfd, dst_dir_fd=dfd)
        os.fsync(dfd)
    finally:
        try:
            os.unlink(temp, dir_fd=dfd)
        except FileNotFoundError:
            pass

def _load_state(dfd):
    try:
        state = json.loads(_read(dfd, '.memory-state.json'))
    except FileNotFoundError:
        return {'version': 1, 'overflow': {}}
    if not isinstance(state, dict) or state.get('version') != 1 or not isinstance(state.get('overflow'), dict):
        raise MemoryErrorSafe('Некорректное состояние памяти')
    return state

def _save_state(dfd, state):
    _store(dfd, '.memory-state.json', json.dumps(state, ensure_ascii=False, sort_keys=True))

RECORD_FORMAT = re.compile(r'- \*\*[^*\n]{1,80}\*\*\s+\S.*')


def _check_entry(name, value, strict=True):
    if not isinstance(value, str) or not value.strip() or value != value.strip() or '\n' in value or '\r' in value:
        raise MemoryErrorSafe('Нужна одна непустая запись в одну строку, без переносов и пустых строк')
    if strict and not RECORD_FORMAT.fullmatch(value):
        raise MemoryErrorSafe('Запись оформляется пунктом Markdown: "- **Ключ:** текст"')
    if name == 'MEMORY' and len(value) > RECORD_LIMIT:
        raise MemoryErrorSafe(f'Запись превышает {RECORD_LIMIT} символов')

def operate(user, action, name, value='', selected=None, strict=True):
    if name not in FILES:
        raise MemoryErrorSafe('Неизвестный файл')
    if action not in ('list', 'append', 'delete', 'replace'):
        raise MemoryErrorSafe('Неизвестная операция')
    dfd = _open_dir(user)
    try:
        text = _read(dfd, FILES[name])
        prefix, body, spans = _records(text, name)
        entries = [m.group() for m in spans]
        if action == 'list':
            return {'ok': True, 'entries': entries, 'characters': len(text), 'limit': LIMIT if name == 'MEMORY' else None}
        if action in ('append', 'replace'):
            _check_entry(name, value, strict)
        if action == 'append':
            new = text + ('' if not body else ('\n' if text.endswith('\n') else '\n\n')) + value + '\n'
        else:
            if not isinstance(selected, list) or not selected:
                raise MemoryErrorSafe('Нужны номера и точные тексты выбранных записей')
            indices = []
            for item in selected:
                if not isinstance(item, dict) or type(item.get('number')) is not int or not isinstance(item.get('text'), str):
                    raise MemoryErrorSafe('Некорректный выбор')
                idx = item['number'] - 1
                if idx < 0 or idx >= len(entries) or entries[idx] != item['text'] or idx in indices:
                    raise MemoryErrorSafe('Записи изменились или выбор неоднозначен; перечитайте файл')
                indices.append(idx)
            if action == 'replace' and len(indices) != 1:
                raise MemoryErrorSafe('Для замены выберите одну запись')
            new_body = body
            for idx in sorted(indices, reverse=True):
                m = spans[idx]
                end = m.end()
                while end < len(new_body) and new_body[end] == '\n':
                    end += 1
                new_body = new_body[:m.start()] + new_body[end:]
            new_body = new_body.rstrip('\n')
            if action == 'replace':
                # Правка — это новая запись плюс удаление старой, а не правка текста на месте.
                new_body = (new_body + '\n\n' if new_body else '') + value
            new = prefix + new_body + ('\n' if new_body else '')
        limited = name == 'MEMORY'
        if limited and len(text) >= LIMIT and len(new) > len(text):
            state = _load_state(dfd)
            state['overflow'].setdefault(name, {'stage': 'offer'})
            _save_state(dfd, state)
            return {'ok': False, 'reason': 'memory_full', 'entries': entries, 'suggested_remove': len(entries) // 4, 'characters': len(text), 'limit': LIMIT}
        state = _load_state(dfd) if limited else None
        _store(dfd, FILES[name], new)
        state_error = False
        if limited:
            try:
                if len(new) > LIMIT:
                    state['overflow']['MEMORY'] = {'stage': 'offer'}
                    _save_state(dfd, state)
                elif action in ('delete', 'replace') and name in state['overflow']:
                    state['overflow'][name] = {'stage': 'confirm'}
                    _save_state(dfd, state)
            except (OSError, ValueError, MemoryErrorSafe):
                state_error = True
        return {'ok': True, 'entries': [m.group() for m in _records(new, name)[2]], 'characters': len(new), 'limit': LIMIT if limited else None, 'overflow': limited and len(new) > LIMIT, 'state_warning': state_error}
    finally:
        os.close(dfd)

class Tools:
    def memory(self, action: str, file: str, value: str = '', selected: list[dict] | None = None, __user__: dict = None) -> dict:
        """Read or update personal memory. action: list, append, delete, replace. file: AGENTS, MEMORY or PROFILE. Each value must be one single-line markdown bullet with a bold key, e.g. "- **Key:** text". For delete/replace pass selected as [{"number": 1, "text": "exact current text"}]."""
        try:
            return operate(__user__, action, file, value, selected)
        except MemoryErrorSafe as exc:
            return {'ok': False, 'reason': str(exc)}
        except (OSError, ValueError, UnicodeError, json.JSONDecodeError):
            return {'ok': False, 'reason': 'operation_failed'}

"""Read-only personal-skill catalogue prototype; not connected to Open WebUI.

Caller must supply an already-authorized directory descriptor. This module does
not authenticate a user and must not be exposed directly as a user tool.
"""

import os
import re
import stat
from dataclasses import dataclass
from urllib.parse import unquote, urlsplit


class CatalogError(Exception):
    """Fail closed on malformed content or unsafe filesystem state."""


@dataclass(frozen=True)
class Entry:
    name: str
    trigger: str
    result: str
    path: str


NAME = re.compile(r"[a-z0-9]+(?:[a-z0-9-]*[a-z0-9])?\Z")
FIELD = re.compile(r"- \*\*([^*\n]+):\*\* (\S.*)\Z")
STEP = re.compile(r"  ([1-9][0-9]*)\. (\S.*)\Z")
LINK = re.compile(r"(?<!!)\[[^\]\n]+\]\(([^)\n]+)\)")
TARGET = re.compile(r"\bшаг(?:у|а|ом|е)?\s+(\d+)\b", re.IGNORECASE)
DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC


def _fail(path: str, message: str) -> None:
    raise CatalogError(f"{path}: {message}")


def _fingerprint(info: os.stat_result) -> tuple[int, ...]:
    return (info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_gid,
            info.st_nlink, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _trusted(info: os.stat_result, path: str, directory: bool) -> None:
    kind_ok = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
    if not kind_ok or info.st_uid != 0 or info.st_mode & 0o022 or (not directory and info.st_nlink != 1):
        _fail(path, "небезопасный владелец, права или тип")


def _directory(parent: int, name: str) -> int:
    fd = os.open(name, DIR_FLAGS, dir_fd=parent)
    try:
        _trusted(os.fstat(fd), name, True)
        return fd
    except BaseException:
        os.close(fd)
        raise


def _file(parent: int, name: str, path: str) -> str:
    fd = os.open(name, FILE_FLAGS, dir_fd=parent)
    try:
        before = os.fstat(fd)
        _trusted(before, path, False)
        with os.fdopen(fd, "r", encoding="utf-8", closefd=False, newline="") as stream:
            text = stream.read()
        after = os.fstat(fd)
        linked = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if _fingerprint(before) != _fingerprint(after) or _fingerprint(after) != _fingerprint(linked):
            _fail(path, "файл изменён при чтении")
        return text
    finally:
        os.close(fd)


def _validate_steps(lines: list[str], path: str) -> None:
    if not lines or lines[0] != "- **Шаги:**":
        _fail(path, "нет блока шагов")
    lines = lines[1:]
    pos, count = 0, 0
    transitions: list[int] = []
    while pos < len(lines):
        if not lines[pos]:
            pos += 1
            continue
        match = STEP.fullmatch(lines[pos])
        if not match or int(match[1]) != count + 1:
            _fail(path, f"неверный номер или отступ шага: строка блока {pos + 2}")
        count += 1
        pad = " " * (len(match[1]) + 4)
        pos += 1
        for key in ("Действие", "Проверка", "Дальше", "При ошибке"):
            if pos >= len(lines) or not lines[pos].startswith(pad):
                _fail(path, f"шаг {count}: отсутствует поле {key}")
            match_field = FIELD.fullmatch(lines[pos][len(pad):])
            if not match_field or match_field[1] != key:
                _fail(path, f"шаг {count}: неверное поле {key}")
            value = match_field[2]
            pos += 1
            while pos < len(lines) and lines[pos].startswith(pad + "  ") and lines[pos].strip():
                value += " " + lines[pos].strip()
                pos += 1
            if key in ("Дальше", "При ошибке"):
                transitions.extend(int(n) for n in TARGET.findall(value))
    if not count:
        _fail(path, "пустой блок шагов")
    if any(n < 1 or n > count for n in transitions):
        _fail(path, "переход на несуществующий шаг")


def _sections(text: str, path: str) -> tuple[str, list[tuple[str, list[str]]]]:
    if "\r" in text or "\x00" in text or "\t" in text:
        _fail(path, "недопустимые управляющие символы или переносы")
    lines = text.split("\n")
    if not lines or not lines[0].startswith("# ") or not lines[0][2:].strip():
        _fail(path, "нет заголовка файла")
    title = lines[0]
    sections: list[tuple[str, list[str]]] = []
    for line in lines[1:]:
        if line.startswith("## "):
            if not line[3:].strip():
                _fail(path, "пустой заголовок раздела")
            sections.append((line, []))
        elif not sections:
            if line:
                _fail(path, "текст вне раздела")
        else:
            sections[-1][1].append(line)
    for _, content in sections:
        while content and not content[0]:
            content.pop(0)
        while content and not content[-1]:
            content.pop()
    return title, sections


def _records(lines: list[str], path: str) -> list[tuple[str, str]]:
    result = []
    for line in lines:
        if not line:
            continue
        match = FIELD.fullmatch(line)
        if not match:
            _fail(path, "неверная запись")
        result.append((match[1], match[2]))
    if not result:
        _fail(path, "пустой раздел")
    return result


def _skill(text: str, name: str, path: str) -> Entry:
    title, sections = _sections(text, path)
    headings = [s[0] for s in sections]
    if title != "# SKILL" or headings not in (
        ["## Описание", "## Порядок работы"],
        ["## Описание", "## Порядок работы", "## Дополнительно"],
    ):
        _fail(path, "неверные или повторные разделы")
    records = _records(sections[0][1], path)
    if [r[0] for r in records] != ["Имя", "Когда применять", "Результат"]:
        _fail(path, "неверные поля описания")
    if records[0][1] != name:
        _fail(path, "имя не совпадает с каталогом")
    _validate_steps(sections[1][1], path)
    if len(sections) == 3:
        _records(sections[2][1], path)
    return Entry(name, records[1][1], records[2][1], f"{name}/SKILL.md")


def _reference(text: str, path: str) -> None:
    _, sections = _sections(text, path)
    if not sections or len({h for h, _ in sections}) != len(sections):
        _fail(path, "отсутствуют или повторяются разделы")
    for _, content in sections:
        if "- **Шаги:**" in content:
            at = content.index("- **Шаги:**")
            if at:
                _records(content[:at], path)
            _validate_steps(content[at:], path)
        else:
            _records(content, path)


def _links(skillfd: int, rel: str, text: str) -> None:
    # Conservatively reject ambiguous Markdown links rather than miss one.
    matches = list(LINK.finditer(text))
    remainder = LINK.sub("", text)
    if re.search(r"!?\[[^\]\n]*\]\s*(?:\(|\[)", remainder):
        _fail(rel, "неподдерживаемый синтаксис ссылки")
    for match in matches:
        raw = match[1]
        decoded = unquote(raw)
        parsed = urlsplit(decoded)
        if (decoded != raw or parsed.scheme or parsed.netloc or parsed.query or parsed.fragment
                or decoded.startswith("/") or "\\" in decoded or "\x00" in decoded):
            _fail(rel, "недопустимая ссылка")
        parts = decoded.split("/")
        if not parts or any(p in ("", ".", "..") for p in parts):
            _fail(rel, "небезопасный путь ссылки")
        base = rel.split("/")[:-1]
        current = os.dup(skillfd)
        try:
            all_parts = base + parts
            for component in all_parts[:-1]:
                nxt = _directory(current, component)
                os.close(current)
                current = nxt
            fd = os.open(all_parts[-1], FILE_FLAGS, dir_fd=current)
            try:
                st = os.fstat(fd)
                _trusted(st, rel, False)
                if _fingerprint(st) != _fingerprint(os.stat(all_parts[-1], dir_fd=current, follow_symlinks=False)):
                    _fail(rel, "цель ссылки изменена при проверке")
            finally:
                os.close(fd)
        finally:
            os.close(current)


def scan(skills_fd: int) -> list[Entry]:
    """Return catalogue only if every entry validates. Missing root is caller policy."""
    result: list[Entry] = []
    try:
        initial = os.fstat(skills_fd)
        _trusted(initial, "skills/", True)
        names = sorted(os.listdir(skills_fd))
    except OSError as exc:
        raise CatalogError(f"skills/: ошибка чтения каталога ({type(exc).__name__})") from exc
    for name in names:
        if not NAME.fullmatch(name):
            _fail(name, "недопустимое имя каталога")
        try:
            skillfd = _directory(skills_fd, name)
            try:
                skill_start = os.fstat(skillfd)
                files = os.listdir(skillfd)
                if any(item not in ("SKILL.md", "references") for item in files):
                    _fail(name, "неизвестный элемент каталога")
                source = _file(skillfd, "SKILL.md", f"{name}/SKILL.md")
                entry = _skill(source, name, f"{name}/SKILL.md")
                _links(skillfd, "SKILL.md", source)
                if "references" in files:
                    ref_fd = _directory(skillfd, "references")
                    try:
                        ref_start = os.fstat(ref_fd)
                        for ref in sorted(os.listdir(ref_fd)):
                            rel = f"references/{ref}"
                            if not re.fullmatch(r"[a-z0-9][a-z0-9-]*\.md", ref):
                                _fail(f"{name}/{rel}", "недопустимое имя справочника")
                            text = _file(ref_fd, ref, f"{name}/{rel}")
                            _reference(text, f"{name}/{rel}")
                            _links(skillfd, rel, text)
                        if (_fingerprint(ref_start) != _fingerprint(os.fstat(ref_fd)) or
                                _fingerprint(ref_start) != _fingerprint(os.stat('references', dir_fd=skillfd, follow_symlinks=False))):
                            _fail(name, "каталог references изменён при проверке")
                    finally:
                        os.close(ref_fd)
                if (_fingerprint(skill_start) != _fingerprint(os.fstat(skillfd)) or
                        _fingerprint(skill_start) != _fingerprint(os.stat(name, dir_fd=skills_fd, follow_symlinks=False))):
                    _fail(name, "каталог скилла изменён при проверке")
                result.append(entry)
            finally:
                os.close(skillfd)
        except (OSError, UnicodeError, ValueError) as exc:
            raise CatalogError(f"{name}: ошибка доступа или кодировки ({type(exc).__name__})") from exc
    if _fingerprint(initial) != _fingerprint(os.fstat(skills_fd)):
        _fail('skills/', 'каталог изменён при проверке')
    return result

"""Staged adapter to the existing file-memory identity binding; not deployed."""
import fcntl
import os



def scan_user(user, memory):
    """User must be server-injected; memory is the trusted memory module, not input.

    An absent skills directory means an empty personal catalogue. All other
    access errors fail closed. No user-supplied path or slug is accepted.
    """
    try:
        agents = memory._open_dir(user)
    except Exception as exc:
        raise CatalogError('Личная память недоступна или пользователь не подтверждён') from exc
    try:
        # Future writes must take LOCK_EX on this very directory descriptor.
        # flock is advisory: non-cooperating writers are not blocked.
        fcntl.flock(agents, fcntl.LOCK_SH)
        before = os.fstat(agents)
        _trusted(before, '.agents', True)
        try:
            skills = _directory(agents, 'skills')
        except FileNotFoundError:
            if _fingerprint(before) != _fingerprint(os.fstat(agents)):
                raise CatalogError('.agents изменён при проверке')
            return []
        try:
            original = os.fstat(skills)
            result = scan(skills)
            linked = os.stat('skills', dir_fd=agents, follow_symlinks=False)
            if (_fingerprint(original) != _fingerprint(os.fstat(skills)) or
                    _fingerprint(original) != _fingerprint(linked) or
                    _fingerprint(before) != _fingerprint(os.fstat(agents))):
                raise CatalogError('Личный каталог изменён при проверке')
            return result
        finally:
            os.close(skills)
    except OSError as exc:
        raise CatalogError(f'Личный каталог недоступен ({type(exc).__name__})') from exc
    finally:
        os.close(agents)

import types
_pilot_memory = types.SimpleNamespace(_open_dir=_open_dir)

class Tools:
    def list_personal_skills(self, __user__: dict = None) -> str:
        """List validated personal skill descriptions for the current user."""
        try:
            entries = scan_user(__user__, _pilot_memory)
            return json.dumps({"skills": [{"name": e.name, "trigger": e.trigger, "result": e.result} for e in entries]}, ensure_ascii=False)
        except Exception:
            return json.dumps({"error": "Личный каталог скиллов недоступен или некорректен"}, ensure_ascii=False)
