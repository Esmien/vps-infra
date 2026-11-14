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
