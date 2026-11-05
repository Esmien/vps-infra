# Кэш последних открытых чатов в Open WebUI — оценка и прототип

Дата: 2026-11-05. Статус: **предложение, не применялось**.

## 1. Замеры (хост, Open WebUI 1 воркер, чат 515 КБ в `chat.chat` jsonb)

Разложение одного открытия чата (медиана из 5 прогонов, in-process, без HTTP):

| шаг | время |
|---|---|
| чтение строки из Postgres (`SELECT chat` jsonb → dict) | **31 мс** |
| `shared_chat_response()` (трансформация, share_mode) | 5 мс |
| `ChatResponse.model_validate` + `model_dump_json` | 7 мс |
| **итого ядро** | **44 мс** |

Снаружи (nginx → uvicorn): медиана **81 мс**, p95 245 мс, max 932 мс. Разница — второй запрос прав
доступа, чтение overlay-стримов из redis, расчёт context usage (ещё один запрос в БД), gzip ответа.

Важное: ответ на этот чат — **1099 КБ** при 515 КБ в базе (jsonb хранится сжатым TOAST и без
дублей, ответ отдаёт `chat` + `meta` + служебные поля). То есть браузер получает в 2 раза больше,
чем лежит в БД, и потом парсит это в JS и рендерит.

## 2. Сколько даст кэш

Два варианта:

- **Кэш `ChatModel`** (на уровне `Chats.get_chat_by_id`): экономит 31 мс чтения БД, но при отдаче
  нужен `model_copy(deep=True)`, иначе мутации в вызывающем коде протекут в кэш. Замер:
  deep copy этого чата — **18.9 мс**. Итог: **+12 мс** из 81. Не окупается.
- **Кэш сериализованных байт ответа** (на уровне роутера): на попадании стоит 0 мс, экономит
  31 (БД) + 7 (сериализация) ≈ **38 мс из 81 (−45%)**. Требует инвалидации по записи.

## 3. Прототип (вариант 2), ~65 строк

```python
# open_webui/utils/chat_response_cache.py
import threading
import time
from collections import OrderedDict

_MAX_PER_USER = 5
_TTL = 900.0
_lock = threading.Lock()
_store: OrderedDict[tuple[str, str], tuple[float, bytes]] = OrderedDict()


def get(user_id: str, chat_id: str) -> bytes | None:
    key = (user_id, chat_id)
    with _lock:
        item = _store.get(key)
        if item is None:
            return None
        ts, body = item
        if time.monotonic() - ts > _TTL:
            _store.pop(key, None)
            return None
        _store.move_to_end(key)
        return body


def put(user_id: str, chat_id: str, body: bytes) -> None:
    key = (user_id, chat_id)
    with _lock:
        _store[key] = (time.monotonic(), body)
        _store.move_to_end(key)
        per_user = [k for k in _store if k[0] == user_id]
        for stale in per_user[:-_MAX_PER_USER]:
            _store.pop(stale, None)


def invalidate(chat_id: str) -> None:
    with _lock:
        for key in [k for k in _store if k[1] == chat_id]:
            _store.pop(key, None)
```

Точки подключения в `models/chats.py` — вызвать `invalidate(id)` в начале методов:

- `update_chat_by_id` (сюда же идут все правки сообщений и заголовка)
- `update_chat_variables_by_id`
- `delete_chat_by_id`, `delete_chat_by_id_and_user_id`
- `insert_new_chat`

В `routers/chats.py::get_chat_by_id`:

```python
cached = chat_response_cache.get(user.id, id)
if cached is not None and not response_streams:
    return Response(content=cached, media_type="application/json")
...
body = json.dumps(data, separators=(",", ":"))
chat_response_cache.put(user.id, id, body)
return Response(content=body, media_type="application/json")
```

Кэш не заполняется, если есть overlay-стримы (идёт генерация) или чат шарится в режиме `continue`.

## 4. Риски и грабли

1. **Инвалидация неполная.** `meta` меняется не только через `update_chat_by_id`: теги, папки,
   архив, pin, шаринг идут через другие модели (`Folders`, `Tags`, `SharedChats`). Каждое такое
   место — потенциальная «залипшая» карточка чата в списке.
2. **Overlay во время генерации.** Ответ склеивается со стримами из redis. Если закэшировать такой
   ответ, UI будет показывать «печатает…» вечно или не покажет вовсе.
3. **Мультиворкер.** In-process словарь при `UVICORN_WORKERS=2` даёт рассинхрон: запись попала в
   воркер A, чтение в воркер B — инвалидация не сработала. Понадобится redis + pub/sub. Сейчас у
   нас 1 воркер, но это закладывает ограничение на будущее.
4. **Память.** До 5 чатов × ~1.1 МБ = 5–6 МБ на пользователя; при 3–5 пользователях — 30 МБ с
   запасом, не проблема.
5. **Безопасность ключа.** Ключ обязан включать `user_id`: админ может читать чужие чаты
   (`ENABLE_ADMIN_CHAT_ACCESS`), и кэш вида «только по chat_id» отдаст чужой диалог.
6. **Форк образа.** Патч живёт в `ai/webui-branding` и должен переноситься через каждый апдейт
   OWUI: правки в `models/chats.py` и `routers/chats.py` — самые «горячие» файлы апстрима, конфликты
   почти гарантированы.
7. **Реальный выигрыш не там.** 38 мс из 81 даст переключение «мгновенным на ощупь» только на
   средних чатах. На «перекуре» (max 932 мс) доминируют не серверные 44 мс, а передача и рендер
   1.1 МБ JSON в браузере — кэш это не лечит.

## 5. Что делать

- Сейчас — **не делать**. Соотношение «38 мс выигрыша / патч в форке через каждый апдейт» плохое.
- Если понадобится ускорять по-настоящему, порядок такой: (1) уменьшать payload (отдавать окно
  сообщений вместо всего чата) — это и есть корень; (2) смотреть, что даёт апгрейд OWUI;
  (3) и только потом кэш байт.
- Дисциплина эксплуатации: длинные диалоги закрывать новым чатом — 500-КБ чат тормозит не сервер,
  а браузер.
