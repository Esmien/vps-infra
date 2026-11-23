#!/usr/bin/env python3
"""Патч Open WebUI: компакция контекста не запускается посреди tool-loop.

Почему: upstream вызывает compact_messages_for_request() внутри цикла после
каждой пачки tool-результатов. Агент в этот момент ещё работает по задаче, а
из истории вырезается кусок вместе с тем, что он только что прочитал: задача
рвётся на середине, модель начинает «забывать» уже проверенное.

Что делает патч: ровно на этом вызове ставит compacted = [] — компакция
остаётся только на сборке запроса (до первого обращения к модели) и по
кнопке ручной компакции ветки чата (POST /chats/{id}/compact).

Fail closed: если апстрим переименует/сдвинет блок — сборка падает.
"""

from pathlib import Path

TARGET = Path('/app/backend/open_webui/utils/middleware.py')
MARKER = 'Mid-loop compaction disabled'

OLD_CALL = '''                                    messages, context_summary, compacted = await compact_messages_for_request(
                                        request,
                                        summary_form_data,
                                        user,
                                        messages,
                                        metadata,
                                        form_data['model'],
                                        request.app.state.MODELS,
                                        bypass_system_prompt=True,
                                    )
'''

NEW_CALL = '''                                    # Mid-loop compaction disabled: compacting between tool
                                    # calls drops the evidence the agent is still working with.
                                    # Compaction still runs when the request is built (before
                                    # the first model call) and on manual branch compaction.
                                    compacted = []
'''


def main() -> int:
    source = TARGET.read_text(encoding='utf-8')

    if MARKER in source:
        print('patch already applied; nothing to do')
        return 0

    if source.count(OLD_CALL) != 1:
        print(f'FAIL: mid-loop compaction call found {source.count(OLD_CALL)} times, expected 1')
        return 1

    source = source.replace(OLD_CALL, NEW_CALL, 1)
    TARGET.write_text(source, encoding='utf-8')

    check = TARGET.read_text(encoding='utf-8')
    remaining = check.count('await compact_messages_for_request(')
    if MARKER not in check or remaining != 2:
        print(f'FAIL: verification failed, remaining pre-request calls = {remaining} (expected 2)')
        return 1

    print('patched: mid-loop compaction disabled, pre-request compaction kept')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
