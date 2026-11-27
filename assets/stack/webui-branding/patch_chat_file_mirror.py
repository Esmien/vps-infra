#!/usr/bin/env python3
"""Патч Open WebUI: вложения чата становятся реальными файлами в песочнице.

Проблема: upload лежит в DATA_DIR/uploads внутри контейнера open-webui, а
агент работает в отдельном контейнере песочницы, куда смонтирован только
/home/ai_service/sandbox/users/<slug> -> /home/user/workspace. Модель получает
картинку в base64 и текст PDF, но не сам файл — «не могу получить исходник».

Что делает патч:
  1) проверяет выбранную машину, её штатную авторизацию и принадлежность upload;
  2) копирует файл пользователя в его <sandbox>/files/input без перехода по
     симлинкам, с UUID в имени; существующие изменённые файлы не затирает;
  3) возвращает путь глазами выбранного терминала: для личной песочницы —
     /home/user/workspace/files/input, для Root Terminal — тот же каталог
     через /mnt/host. Для прочих машин или при сомнении путь не выдаётся.

Данные пользователя и conn_id берутся из /sandbox-state/state.json;
обычному пользователю доступ к Root Terminal этим патчем не выдаётся.
user в pipeline — pydantic UserModel, а не dict: читаем и атрибуты, и ключи.

Fail closed: если апстрим переименует функции — сборка падает.
"""

from pathlib import Path

TARGET = Path('/app/backend/open_webui/utils/middleware.py')
MARKER = 'SANDBOX_INPUT_SUBDIR'

OLD_HEAD = '''def add_file_context(messages: list) -> list:
    """
    Replay attachment references beside their original user messages for native tools.
    """

    def format_file_tag(file):
        attrs = {'type': file.get('type', 'file'), 'id': file.get('id') or file.get('url')}
        attrs.update({key: file[key] for key in ('url', 'content_type', 'name') if file.get(key)})
        return (
            '<file ' + ' '.join(f'{key}="{html.escape(str(value), quote=True)}"' for key, value in attrs.items()) + '/>'
        )
'''

NEW_HEAD = '''SANDBOX_USERS_ROOT = '/sandbox-users'
SANDBOX_INPUT_SUBDIR = 'files/input'
SANDBOX_AGENT_INPUT = '/home/user/workspace/files/input'


def _user_field(user, key: str):
    if isinstance(user, dict):
        return user.get(key)
    return getattr(user, key, None)


async def _prepare_mirror_context(request, form_data, metadata, user):
    """Resolve an authorized selected machine and file ownership; fail closed."""
    try:
        from uuid import UUID
        from open_webui.models.files import Files
        from open_webui.utils.machines import find_machine_connection
        import inspect

        if 'machine_id' in form_data:
            machine_id = form_data.get('machine_id')
        elif 'terminal_id' in form_data:
            machine_id = form_data.get('terminal_id')
        else:
            machine_id = metadata.get('machine_id')
        user_id = _user_field(user, 'id')
        if not isinstance(machine_id, str) or not user_id:
            return None
        connection = await find_machine_connection(user, machine_id, metadata, request=request)
        if not connection or not connection.get('enabled', True):
            return None
        with open('/sandbox-state/state.json', encoding='utf-8') as f:
            state = json.load(f)
        entry = state.get(user_id) or {}
        slug = entry.get('slug')
        if not isinstance(slug, str) or not re.fullmatch(r'[a-z0-9]+(?:-[a-z0-9]+)*', slug):
            return None
        if entry.get('user_id') != user_id or not entry.get('active'):
            return None
        if entry.get('dir') != f'/home/ai_service/sandbox/users/{slug}':
            return None
        url = (connection.get('url') or '').rstrip('/')
        if machine_id == entry.get('conn_id') and url == f'http://ot-u-{slug}:8000':
            agent_dir = SANDBOX_AGENT_INPUT
        elif _user_field(user, 'role') == 'admin' and url == 'http://open-terminal:8000':
            agent_dir = f'/mnt/host/home/ai_service/sandbox/users/{slug}/files/input'
        else:
            return None
        sandbox_dir = os.path.join(SANDBOX_USERS_ROOT, slug)
        if not os.path.isdir(sandbox_dir) or os.path.islink(sandbox_dir):
            return None
        ids = set()
        for message in form_data.get('messages', []):
            if not isinstance(message, dict) or message.get('role') != 'user':
                continue
            for file in message.get('files') or []:
                if not isinstance(file, dict):
                    continue
                file_id = file.get('id')
                try:
                    if not isinstance(file_id, str) or str(UUID(file_id)) != file_id:
                        continue
                except (ValueError, AttributeError):
                    continue
                ids.add(file_id)
        allowed = set()
        for file_id in ids:
            result = Files.get_file_by_id_and_user_id(file_id, user_id)
            owned = await result if inspect.isawaitable(result) else result
            if owned:
                allowed.add(file_id)
        return {'sandbox_dir': sandbox_dir, 'agent_dir': agent_dir, 'allowed': allowed}
    except Exception as e:
        log.warning('sandbox mirror context unavailable: %s', type(e).__name__)
        return None


def _mirror_upload_for_user(file: dict, user, mirror_context=None) -> str | None:
    """Mirror owned uploads only to the selected, authorized machine."""
    if not isinstance(file, dict) or not mirror_context:
        return None
    file_id = file.get('id')
    name = file.get('name') or file.get('filename') or ''
    if not isinstance(file_id, str) or file_id not in mirror_context['allowed'] or not isinstance(name, str):
        return None
    safe_name = os.path.basename(name).replace('\\x00', '').strip()
    if not safe_name or safe_name in ('.', '..'):
        return None
    from open_webui.env import DATA_DIR
    import stat
    import hashlib
    source_root = DATA_DIR / 'uploads'
    ext = os.path.splitext(safe_name)[1]
    source = next((p for p in (source_root / f'{file_id}_{safe_name}', source_root / f'{file_id}{ext}')
                   if p.is_file() and not p.is_symlink()), None)
    if source is None:
        return None
    # ID avoids name collisions; O_NOFOLLOW prevents a sandbox user redirecting writes.
    target_name = f'{file_id}_{safe_name}'
    agent_path = f"{mirror_context['agent_dir']}/{target_name}"
    fd = None
    current = None
    temp_name = None
    try:
        current = os.open(mirror_context['sandbox_dir'], os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        sandbox_stat = os.fstat(current)
        for part in ('files', 'input'):
            try:
                os.mkdir(part, 0o755, dir_fd=current)
                os.chown(part, sandbox_stat.st_uid, sandbox_stat.st_gid, dir_fd=current, follow_symlinks=False)
            except FileExistsError:
                pass
            next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=current)
            os.close(current)
            current = next_fd
        # Check the type before opening: reading a FIFO would block the request forever.
        try:
            existing_mode = os.stat(target_name, dir_fd=current, follow_symlinks=False).st_mode
        except FileNotFoundError:
            existing_mode = None
        if existing_mode is not None:
            if not stat.S_ISREG(existing_mode):
                log.warning('sandbox mirror target is not a regular file: %s', file_id)
                return None
            try:
                existing_fd = os.open(target_name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=current)
            except OSError:
                return None
            try:
                if not stat.S_ISREG(os.fstat(existing_fd).st_mode):
                    return None
                with os.fdopen(existing_fd, 'rb') as existing, open(source, 'rb') as original:
                    existing_fd = None
                    same = hashlib.file_digest(existing, 'sha256').digest() == hashlib.file_digest(original, 'sha256').digest()
                return agent_path if same else None
            finally:
                if existing_fd is not None:
                    os.close(existing_fd)
        # Copy under a temp name and rename atomically: a failed copy must never
        # leave a truncated file that a later retry would mistake for the real one.
        from uuid import uuid4
        temp_name = f'.{target_name}.{uuid4().hex}.tmp'
        fd = os.open(temp_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644, dir_fd=current)
        os.fchown(fd, sandbox_stat.st_uid, sandbox_stat.st_gid)
        with open(source, 'rb') as original, os.fdopen(fd, 'wb') as output:
            fd = None
            shutil.copyfileobj(original, output)
            output.flush()
            os.fsync(output.fileno())
        # Publish atomically without replacing a file created during the copy.
        # Both names are in the same directory/filesystem; link fails closed
        # with EEXIST even if another writer wins after our initial stat.
        os.link(temp_name, target_name, src_dir_fd=current, dst_dir_fd=current, follow_symlinks=False)
        os.unlink(temp_name, dir_fd=current)
        temp_name = None
        return agent_path
    except OSError as e:
        log.warning('sandbox file mirror failed for %s: %s', file_id, e)
        return None
    finally:
        if fd is not None:
            os.close(fd)
        if temp_name is not None and current is not None:
            try:
                os.unlink(temp_name, dir_fd=current)
            except OSError:
                pass
        if current is not None:
            os.close(current)


def add_file_context(messages: list, user=None, mirror_context=None) -> list:
    """Add attachment context with paths only for safe, authorized mirrors."""

    def format_file_tag(file, sandbox_path=None):
        attrs = {'type': file.get('type', 'file'), 'id': file.get('id') or file.get('url')}
        attrs.update({key: file[key] for key in ('url', 'content_type', 'name') if file.get(key)})
        if sandbox_path:
            attrs['path'] = sandbox_path
        return (
            '<file ' + ' '.join(f'{key}="{html.escape(str(value), quote=True)}"' for key, value in attrs.items()) + '/>'
        )
'''


OLD_LOOP = '''        if not attached_files:
            continue

        file_tags = [format_file_tag(file) for file in attached_files]
        file_context = '<attached_files>\\n' + '\\n'.join(file_tags) + '\\n</attached_files>\\n\\n'
'''

NEW_LOOP = '''        if not attached_files:
            continue

        sandbox_paths = {}
        for file in attached_files:
            mirrored = _mirror_upload_for_user(file, user, mirror_context)
            if mirrored:
                sandbox_paths[file.get('id') or file.get('url')] = mirrored

        file_tags = [
            format_file_tag(file, sandbox_paths.get(file.get('id')) if isinstance(file.get('id'), str) else None)
            for file in attached_files
        ]
        file_context = '<attached_files>\\n' + '\\n'.join(file_tags) + '\\n</attached_files>\\n\\n'
        if sandbox_paths:
            file_context += 'Files with a path attribute are real files on disk: open them with the terminal tools instead of relying on the extracted text.\\n\\n'
'''

OLD_SIGNATURE = '''def process_messages_with_output(
    messages: list[dict],
    reasoning_format: str | None = None,
    include_file_context: bool = False,
) -> list[dict]:'''

NEW_SIGNATURE = '''def process_messages_with_output(
    messages: list[dict],
    reasoning_format: str | None = None,
    include_file_context: bool = False,
    user=None,
    mirror_context=None,
) -> list[dict]:'''

OLD_CALLS = [
    ('        add_file_context(processed)\n', '        add_file_context(processed, user=user, mirror_context=mirror_context)\n'),
    (
        '''                        process_messages_with_output(
                            compacted,
                            reasoning_format=get_reasoning_format(model),
                            include_file_context=metadata['include_file_context'],
                        )''',
        '''                        process_messages_with_output(
                            compacted,
                            reasoning_format=get_reasoning_format(model),
                            include_file_context=metadata['include_file_context'],
                            user=user,
                            mirror_context=mirror_context,
                        )''',
    ),
    (
        '''    form_data['messages'] = process_messages_with_output(
        form_data.get('messages', []),
        reasoning_format=get_reasoning_format(model),
        include_file_context=metadata['include_file_context'],
    )''',
        '''    mirror_context = await _prepare_mirror_context(request, form_data, metadata, user) if metadata['include_file_context'] else None
    form_data['messages'] = process_messages_with_output(
        form_data.get('messages', []),
        reasoning_format=get_reasoning_format(model),
        include_file_context=metadata['include_file_context'],
        user=user,
        mirror_context=mirror_context,
    )''',
    ),
]

# Якорь импортов меняется от версии к версии (0.13.5 убрал `import random`).
# Берём первый совпавший вариант, иначе сборка падает — молча без shutil нельзя.
IMPORT_ANCHORS = [
    ('import random\nimport re\nimport sys\n', 'import random\nimport re\nimport shutil\nimport sys\n'),
    ('import re\nimport sys\n', 'import re\nimport shutil\nimport sys\n'),
]


def main() -> int:
    source = TARGET.read_text(encoding='utf-8')

    if MARKER in source:
        print('patch already applied; nothing to do')
        return 0

    for needle in (OLD_HEAD, OLD_LOOP, OLD_SIGNATURE):
        if needle not in source:
            print(f'FAIL: upstream block not found:\n{needle[:200]}')
            return 1

    source = source.replace(OLD_HEAD, NEW_HEAD, 1)
    source = source.replace(OLD_LOOP, NEW_LOOP, 1)
    source = source.replace(OLD_SIGNATURE, NEW_SIGNATURE, 1)
    for old_imports, new_imports in IMPORT_ANCHORS:
        if old_imports in source:
            source = source.replace(old_imports, new_imports, 1)
            break
    else:
        print('FAIL: import anchor not found for any known layout')
        return 1

    for old, new in OLD_CALLS:
        if old not in source:
            print(f'FAIL: call site not found:\n{old[:200]}')
            return 1
        source = source.replace(old, new, 1)

    TARGET.write_text(source, encoding='utf-8')

    check = TARGET.read_text(encoding='utf-8')
    for needle in (
        MARKER,
        'def add_file_context(messages: list, user=None, mirror_context=None)',
        'add_file_context(processed, user=user, mirror_context=mirror_context)',
        "agent_path = f\"{mirror_context['agent_dir']}",
        '_user_field(user, ',
    ):
        if needle not in check:
            print(f'FAIL: verification missing {needle}')
            return 1

    print('patched chat file mirroring into sandbox (files/input)')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
