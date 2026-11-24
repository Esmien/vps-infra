# Разделённый стек: WebUI на одном хосте, LiteLLM и VPN на другом

Штатный `install.py` ставит весь стек на одну машину и рассчитывает, что
Open WebUI ходит в LiteLLM по docker-имени `litellm:4000`. Такая схема
разъезжается, если LiteLLM вынесен на другой сервер: подключения WebUI
начинают смотреть в никуда, их модели (`chat.*`, `other.*`) не загружаются,
и WebUI падает в fallback («Model control options cannot be applied to the
fallback model»).

Ниже — проверенная раскладка и все грабли, собранные при переезде
24–25.11.2026. Команды рассчитаны на Ubuntu 24.04 и выполняются от root.

## Роли хостов

| Хост | Что крутится | DNS |
|---|---|---|
| `webui` (RU) | Open WebUI, PostgreSQL `webui_db`, open-terminal + песочницы, nginx `ai.<домен>` | `ai.esmien.ru` |
| `edge` (зарубежный) | Xray (VPN-вход), LiteLLM + Redis, PostgreSQL `litellm_db`, nginx `llm.<домен>` | `share.esmien.com`, `llm.esmien.ru` |

Сертификаты — Let's Encrypt через Cloudflare DNS-01: порт 80 на edge занят
stream-входом VPN, HTTP-01 там не пройдёт.

## Общая подготовка обоих хостов

```bash
apt-get update
apt-get install -y ca-certificates curl gnupg git jq openssl nginx \
  certbot python3-certbot-dns-cloudflare docker.io docker-compose-v2 rsync
systemctl enable --now docker

install -d /usr/share/postgresql-common/pgdg
curl -fsSL -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc \
  https://www.postgresql.org/media/keys/ACCC4CF8.asc
echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] \
https://apt.postgresql.org/repos/apt noble-pgdg main" > /etc/apt/sources.list.d/pgdg.list
apt-get update
apt-get install -y postgresql-17 postgresql-17-pgvector
```

Ключ шифрования дампов и утилита шифрования:

```bash
printf '%s\n' "$SECRETS_KEY" > /etc/backup-crypto.key
chown root:ai_service /etc/backup-crypto.key && chmod 440 /etc/backup-crypto.key
install -m 755 assets/host/bin/dump-crypto /usr/local/bin/dump-crypto
```

### Грабли: ufw режет docker → PostgreSQL

Docker-контейнеры ходят на хост через `host.docker.internal` (172.17.0.1 /
172.18.0.1). Если ufw включён, это входящий трафик и он блокируется — WebUI
вечно висит на «health: starting», в логах пусто, `connect_ex` возвращает
ошибку.

```bash
ufw allow 80/tcp
ufw allow 443/tcp
ufw allow from 172.16.0.0/12 to any port 5432 proto tcp
```

Либо не поднимать `listen_addresses` наружу и закрыть 5432 через
`fw-guard-postgres.service` (iptables, оставляет localhost и 172.16/12).

### Грабли: pgvector и ivfflat при восстановлении

- `CREATE EXTENSION vector` требует superuser. Дамп, восстановленный ролью
  `webui`, падает на этом — расширение надо создать заранее от `postgres`:
  `su - postgres -c "psql -d webui_db -c 'CREATE EXTENSION IF NOT EXISTS vector'"`.
- `maintenance_work_mem` по умолчанию 64 МБ, а ivfflat-индекс требует ~65 МБ.
  `pg_restore` падает на `CREATE INDEX ... USING ivfflat`. Восстанавливать с
  `PGOPTIONS='-c maintenance_work_mem=256MB'` (в `install.py` это уже учтено).

## Edge: LiteLLM + PostgreSQL

```bash
PW=$(grep -E '^LITELLM_DB_PASSWORD=' /home/ai_service/ai-stack/ai/.env | cut -d= -f2-)
su - postgres -c "psql -c \"CREATE ROLE litellm LOGIN PASSWORD '$PW'\""
su - postgres -c "createdb -O litellm litellm_db"
su - postgres -c "psql -d litellm_db -c 'CREATE EXTENSION IF NOT EXISTS vector'"
```

`postgresql.conf`: `listen_addresses = '127.0.0.1,172.17.0.1,172.18.0.1'`
(или `'*'` + `fw-guard-postgres`). В `pg_hba.conf`:
`host all all 172.16.0.0/12 scram-sha-256`.

Compose для LiteLLM — только `litellm` и `litellm-redis` из
`assets/stack/docker-compose.yml`, плюс `stack-net` (external). Обязательные
мелочи:

- каталог `/root/litellm-chatgpt` должен существовать (иначе docker создаст
  его root-ом как файл-том и OAuth-токены некуда писать);
- `DATABASE_URL` уже смотрит на `host.docker.internal:5432`;
- `litellm` слушает `127.0.0.1:4000`, наружу не публикуется.

Восстановление `litellm_db`: скачать дамп из файлового бота, расшифровать
`dump-crypto decrypt`, затем `pg_restore --clean --if-exists --no-owner
--no-acl` от роли `litellm`.

### Грабли: пароль админа LiteLLM

`general_settings.disable_env_credential_login: true` означает, что
`UI_USERNAME/UI_PASSWORD` из `.env` для входа в панель **не работают**.
Админ — обычный пользователь в БД. Сброс пароля:

```bash
KEY=$(grep -E '^LITELLM_MASTER_KEY=' .env | cut -d= -f2-)
HASH=$(docker exec litellm python -c \
  'from litellm.proxy.utils import hash_password; print(hash_password("NEWPASS"))')
PW=$(grep -E '^LITELLM_DB_PASSWORD=' .env | cut -d= -f2-)
PGPASSWORD="$PW" psql -h 127.0.0.1 -U litellm -d litellm_db -c \
  "update \"LiteLLM_UserTable\" set password='$HASH', password_reset_required=false where user_email='admin@example.com'"
docker restart litellm
```

Проверка входа — `POST /v2/login` JSON-ом (у `/login` тело form-urlencoded,
на JSON он вернёт 401).

### nginx `llm.<домен>`

Сайт слушает `127.0.0.1:9443 proxy_protocol ssl` — туда его приводит stream
SNI-маршрутизация VPN-входа. В `stream.d/443-map.conf` точные SNI-правила
(`llm`, `mon`, `stats`, `ai` → `127.0.0.1:9443`) должны стоять **до**
catch-all reject-регекспа, иначе панель получит `127.0.0.1:1`.

Для dns-01 сертификатов certbot не создаёт `options-ssl-nginx.conf` и
`ssl-dhparams.pem` — их надо положить из пакетов:

```bash
install -m 644 /usr/lib/python3/dist-packages/certbot_nginx/_internal/tls_configs/options-ssl-nginx.conf /etc/letsencrypt/
install -m 644 /usr/lib/python3/dist-packages/certbot/ssl-dhparams.pem /etc/letsencrypt/
```

## VPN-вход (edge)

`vpn-service/scripts/deploy.sh` — после правок 24.11.2026 он сам:

- не ищет несуществующий `python3-jq`;
- кладёт `00-esmien-sub-map.conf` (`$esmien_sub_file` по User-Agent) и
  `00-vpn-logformat.conf` (`log_format vpnx`);
- копирует certbot-сниппеты;
- вырезает `sendThrough` из сгенерированного `config.json` — иначе freedom
  привязывается к IP прежнего хоста и весь direct-трафик умирает;
- прописывает SNI панелей в stream-map.

## WebUI-хост

```bash
mkdir -p /home/ai_service/ai-stack/ai /home/ai_service/webui_data \
         /home/ai_service/sandbox/users /home/ai_service/sandbox-users
docker network create stack-net
```

- `ai-stack/ai/.env` — из архива секретов;
- `docker-compose.yml` — `open-webui` (+ `open-terminal`), образ собирается
  из `assets/stack/webui-branding`;
- `DATABASE_URL` → `host.docker.internal:5432/webui_db`;
- **`OPENAI_API_BASE_URL` → публичный `https://llm.<домен>/v1`**, не
  `http://litellm:4000/v1`;
- volume `/home/ai_service/webui_data:/app/backend/data`;
- `/home/ai_service/sandbox/users:/sandbox-users` и
  `/home/ai_service/sandbox-users:/sandbox-state:ro` — нужны инструменту
  генерации картинок и фильтру памяти.

### Грабли: подключения WebUI после восстановления БД

В таблице `config`, ключ `openai.api_base_urls`, URL-ы подключений хранятся
как есть. Если LiteLLM переехал, заменить только URL, **не трогая**
`prefix_id`/`api_configs` (`chat.`, `other.`, `aider.` — внутренние префиксы
WebUI, к LiteLLM отношения не имеют):

```sql
update config
set value = replace(value::text, 'http://litellm:4000/v1', 'https://llm.example.com/v1')::json,
    updated_at = (extract(epoch from now())*1000)::bigint
where key = 'openai.api_base_urls';
```

После этого WebUI видит модели подключений, `base_model_id` резолвятся и
ошибка fallback исчезает. Перезапуск WebUI обязателен (кэш моделей).

### Песочницы и файловая память

- `/home/ai_service/ai-stack/ai/sandbox-users/template.compose.yml`;
- `/usr/local/bin/sandbox-provisioner.py` + `sandbox-provisioner.{service,timer}`;
- `/home/ai_service/sandbox-users/state.json` создаётся провижионером;
- `.agents` внутри рабочего каталога пользователя обязан принадлежать
  **root:root** (иначе `ValueError: memory directory must be root-owned`),
  сам каталог пользователя — uid 1000;
- `sandbox-provisioner.py --dry-run` показывает, каких контейнеров не хватает;
- `ensure_memory_files` не перезаписывает существующие AGENTS/MEMORY/PROFILE.

### Бэкапы на разделённых хостах

`assets/stack/backup-databases.sh` сам определяет, какие БД доступны: на
webui-хосте делает `webui_db`, на edge — `litellm_db`, отсутствующие
пропускает. Ключ `/etc/backup-crypto.key`, retention 3, таймер
`postgres-backup.timer` (02:30 UTC).

Память пользователей: `ai-memory-backup.timer` (00:00 UTC) пушит
`users/<slug>/.agents` в приватный `users_ai_memory`. При первом запуске на
восстановленном хосте сначала подтяните более свежие файлы из репозитория,
иначе снапшот откатит их к дате файлового бэкапа.

## Что проверять после развёртывания

```bash
# VPN
curl -s -o /dev/null -w '%{http_code}\n' https://share.<домен>/sub/<token>   # 200
# LiteLLM
curl -s -o /dev/null -w '%{http_code}\n' https://llm.<домен>/health/liveliness
# WebUI
curl -s -o /dev/null -w '%{http_code}\n' https://ai.<домен>/health
# Песочницы
docker ps --format '{{.Names}} {{.Status}}' | grep '^ot-u-' | grep -vc healthy   # 0
```
