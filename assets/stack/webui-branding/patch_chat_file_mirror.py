#!/usr/bin/env python3
"""Патч Open WebUI: вложения чата становятся реальными файлами в песочнице.

Проблема: upload лежит в DATA_DIR/uploads внутри контейнера open-webui, а
агент работает в отдельном контейнере песочницы, куда смонтирован только
/home/ai_service/sandbox/users/<slug> -> /home/user/workspace. Модель получает
картинку в base64 и текст PDF, но не сам файл — «не могу получить исходник».

Что делает патч:
  1) в тег <file .../> добавляет path="..." — путь оригинала, как его видит
     агент (/home/user/workspace/uploads/<имя>);
  2) перед этим копирует upload в <sandbox>/uploads (lazy, при первом
     упоминании файла в запросе). Копия делается один раз: если файл уже есть
     с тем же размером — не трогаем, чтобы не затереть правки агента.

Правила слаг-а те же, что в sandbox-provisioner.py и terminal-autoselect.js:
локальная часть email, [^a-z0-9]+ -> '-', обрезка 24.

Fail closed: если апстрим переименует функции — сборка падает.
"""

from pathlib import Path

TARGET = Path('/app/backend/open_webui/utils/middleware.py')
MARKER = 'SANDBOX_UPLOADS_SUBDIR'

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
SANDBOX_UPLOADS_SUBDIR = 'uploads'
SANDBOX_AGENT_UPLOADS = '/home/user/workspace/uploads'


def _sandbox_slug(email: str | None) -> str:
    """Slug песочницы пользователя: та же схема, что в sandbox-provisioner.py."""
    local = (email or '').split('@')[0].lower()
    return re.sub(r'^-+|-+$', '', re.sub(r'[^a-z0-9]+', '-', local))[:24]


def _mirror_upload_for_user(file: dict, user: dict | None) -> str | None:
    """Копирует upload в песочницу пользователя, возвращает путь глазами агента.

    None — если копировать нечего/некуда (нет песочницы, файл не upload).
    """
    if not isinstance(file, dict) or not isinstance(user, dict):
        return None

    file_id = file.get('id') or file.get('url')
    name = file.get('name') or file.get('filename') or ''
    if not isinstance(file_id, str) or not file_id or not isinstance(name, str) or not name:
        return None

    slug = _sandbox_slug(user.get('email'))
    if not slug:
        return None

    sandbox_dir = os.path.join(SANDBOX_USERS_ROOT, slug)
    if not os.path.isdir(sandbox_dir):
        return None

    safe_name = os.path.basename(name).replace('\\x00', '').strip()
    if not safe_name or safe_name in ('.', '..'):
        return None

    from open_webui.env import DATA_DIR

    uploads_source = DATA_DIR / 'uploads'
    extension = os.path.splitext(safe_name)[1]
    source = next(
        (
            path
            for path in (
                uploads_source / f'{file_id}_{safe_name}',
                uploads_source / f'{file_id}{extension}',
            )
            if path.is_file()
        ),
        None,
    )
    if source is None:
        return None

    uploads_dir = os.path.join(sandbox_dir, SANDBOX_UPLOADS_SUBDIR)
    target = os.path.join(uploads_dir, safe_name)
    agent_path = f'{SANDBOX_AGENT_UPLOADS}/{safe_name}'

    try:
        source_size = source.stat().st_size
        if os.path.exists(target):
            if os.path.getsize(target) == source_size:
                return agent_path
            # То же имя, другой файл: не затираем, кладём рядом с префиксом id.
            safe_name = f'{file_id[:8]}_{safe_name}'
            target = os.path.join(uploads_dir, safe_name)
            agent_path = f'{SANDBOX_AGENT_UPLOADS}/{safe_name}'
            if os.path.exists(target):
                return agent_path
        os.makedirs(uploads_dir, exist_ok=True)
        shutil.copyfile(source, target)
        os.chmod(target, 0o644)
        try:
            # Владелец — как у самой песочницы: агент должен мочь файл заменить.
            sandbox_stat = os.stat(sandbox_dir)
            os.chown(target, sandbox_stat.st_uid, sandbox_stat.st_gid)
        except OSError:
            pass
    except OSError as e:
        log.warning('sandbox file mirror failed for %s: %s', file_id, e)
        return None

    return agent_path


def add_file_context(messages: list, user: dict | None = None) -> list:
    """
    Replay attachment references beside their original user messages for native tools.

    Files mirrored into the user's sandbox carry path="..." pointing at the real
    upload, readable by the builtin terminal tools.
    """

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
            mirrored = _mirror_upload_for_user(file, user)
            if mirrored:
                sandbox_paths[file.get('id') or file.get('url')] = mirrored

        file_tags = [
            format_file_tag(file, sandbox_paths.get(file.get('id') or file.get('url'))) for file in attached_files
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
    user: dict | None = None,
) -> list[dict]:'''

OLD_CALLS = [
    ('        add_file_context(processed)\n', '        add_file_context(processed, user=user)\n'),
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
                        )''',
    ),
    (
        '''    form_data['messages'] = process_messages_with_output(
        form_data.get('messages', []),
        reasoning_format=get_reasoning_format(model),
        include_file_context=metadata['include_file_context'],
    )''',
        '''    form_data['messages'] = process_messages_with_output(
        form_data.get('messages', []),
        reasoning_format=get_reasoning_format(model),
        include_file_context=metadata['include_file_context'],
        user=user,
    )''',
    ),
]

OLD_IMPORTS = 'import random\nimport re\nimport sys\n'
NEW_IMPORTS = 'import random\nimport re\nimport shutil\nimport sys\n'


def main() -> int:
    source = TARGET.read_text(encoding='utf-8')

    if MARKER in source:
        print('patch already applied; nothing to do')
        return 0

    for needle in (OLD_HEAD, OLD_LOOP, OLD_SIGNATURE, OLD_IMPORTS):
        if needle not in source:
            print(f'FAIL: upstream block not found:\n{needle[:200]}')
            return 1

    source = source.replace(OLD_HEAD, NEW_HEAD, 1)
    source = source.replace(OLD_LOOP, NEW_LOOP, 1)
    source = source.replace(OLD_SIGNATURE, NEW_SIGNATURE, 1)
    source = source.replace(OLD_IMPORTS, NEW_IMPORTS, 1)

    for old, new in OLD_CALLS:
        if old not in source:
            print(f'FAIL: call site not found:\n{old[:200]}')
            return 1
        source = source.replace(old, new, 1)

    TARGET.write_text(source, encoding='utf-8')

    check = TARGET.read_text(encoding='utf-8')
    for needle in (
        MARKER,
        'def add_file_context(messages: list, user: dict | None = None)',
        'add_file_context(processed, user=user)',
        "path = f'{SANDBOX_AGENT_UPLOADS}",
    ):
        if needle not in check:
            print(f'FAIL: verification missing {needle}')
            return 1

    print('patched chat file mirroring into sandbox')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
