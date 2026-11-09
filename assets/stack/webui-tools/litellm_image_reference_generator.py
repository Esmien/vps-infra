"""
title: LiteLLM Image Reference Generator (Universal 2K)
author: Vlad
description: Генерирует и редактирует изображения через локальный шлюз LiteLLM (2K по умолчанию, файловое хранилище Open WebUI без раздувания БД, атомарная запись O_EXCL, ретраи с jitter и общим дедлайном, умный подбор aspect_ratio).
required_open_webui_version: 0.5.0
required_python_version: >=3.11
version: 1.6.4
licence: MIT
"""

import asyncio
import base64
import inspect
import json
import os
import random
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Literal

import httpx
from pydantic import BaseModel, Field

MSK_TZ = timezone(timedelta(hours=3))

MIME_TO_EXT: dict[str, str] = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/webp": "webp",
    "image/gif": "gif",
}

EXT_TO_MIME: dict[str, str] = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "webp": "image/webp",
    "gif": "image/gif",
}

ALLOWED_ASPECT_RATIOS = {
    "1:1",
    "16:9",
    "9:16",
    "4:3",
    "3:4",
    "4:5",
    "5:4",
    "3:2",
    "2:3",
    "21:9",
}

ReferenceSourceLiteral = Literal["original", "last_generated", "both", "none"]

ALLOWED_RESOLUTIONS = {"1K", "2K", "4K"}
RETRYABLE_STATUS_CODES = {408, 429, 500, 502, 503, 504}

SAVED_PATH_PATTERN = re.compile(
    r"Сохранено (?:на диск|в хранилище Open WebUI):\s*(.+?)(?:\.\s|\.$|\n|$)"
)
FILE_CONTENT_URL_PATTERN = re.compile(
    r"^(?:https?://[^/]+)?/api/v1/files/([0-9a-fA-F\-]+)/content(?:[?#].*)?$"
)

MAGIC_SIGNATURES: list[tuple[bytes, str]] = [
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF8", "image/gif"),
]


def with_suppress_chown(path: Path, valves: Any) -> None:
    """chown в песочницу, ошибки не валят генерацию картинки."""
    try:
        os.chown(path, valves.workspace_uid, valves.workspace_gid)
    except OSError:
        pass


class Tools:
    class Valves(BaseModel):
        litellm_api_key: str = Field(
            default="",
            description="Ключ от твоего LiteLLM (Virtual Key или Master Key)",
        )
        litellm_api_url: str = Field(
            default="http://litellm:4000/v1/images/generations",
            description="Внутренний URL эндпоинта генерации картинок в LiteLLM",
        )
        model: str = Field(
            default="img.bytedance-seed/seedream-5-0-flash",
            description="Public Model Name модели в LiteLLM (например, img.bytedance-seed/seedream-5-0-flash или img.openrouter/hy-image-v3.5)",
        )
        default_resolution: str = Field(
            default="2K",
            description="Разрешение по умолчанию: 1K, 2K или 4K (2K стоит столько же, сколько 1K, но в 4 раза чётче)",
        )
        save_archive_copy: bool = Field(
            default=True,
            description="Сохранять ли дополнительную архивную копию с именем по дате/времени (UTC+3) в save_dir",
        )
        save_dir: str = Field(
            default="/app/backend/data/generated_images",
            description="Папка внутри контейнера Open WebUI для сохранения архивных копий по дате/времени",
        )
        save_to_user_workspace: bool = Field(
            default=True,
            description="Класть картинки в generated_images рабочего каталога пользователя (песочница) вместо общего save_dir",
        )
        workspace_root: str = Field(
            default="/sandbox-users",
            description="Каталог песочниц пользователей, смонтированный в контейнер",
        )
        workspace_state_path: str = Field(
            default="/sandbox-state/state.json",
            description="state.json провижионера (user_id / email → рабочий каталог)",
        )
        workspace_uid: int = Field(
            default=1000,
            description="uid владельца файлов в песочнице",
        )
        workspace_gid: int = Field(
            default=1000,
            description="gid владельца файлов в песочнице",
        )
        filename_format: str = Field(
            default="%Y-%m-%d_%H-%M-%S",
            description="Формат даты/времени (UTC+3) для имени файла (strftime)",
        )
        max_reference_images: int = Field(
            default=10,
            description="Максимум референсных картинок для отправки в модель",
        )
        lookback_user_messages: int = Field(
            default=3,
            description="Сколько последних сообщений пользователя с фото просматривать (чтобы можно было скинуть 2 фото разными сообщениями)",
        )
        max_retries: int = Field(
            default=3,
            description="Количество попыток при временных сбоях сети или ошибках 502/503/504/429 от шлюза",
        )
        timeout_seconds: int = Field(
            default=75,
            description="Таймаут ожидания чтения одной попытки генерации в секундах",
        )
        total_deadline_seconds: int = Field(
            default=180,
            description="Общий максимальный бюджет времени (включая все ретраи и паузы) в секундах",
        )

    def __init__(self) -> None:
        self.valves = self.Valves()

    def _sniff_media_type(self, raw: bytes) -> str | None:
        """Определяет реальный MIME по magic-байтам: LiteLLM иногда отдаёт JPEG под именем image/png."""
        if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
            return "image/webp"
        for signature, mime in MAGIC_SIGNATURES:
            if raw.startswith(signature):
                return mime
        return None

    def _resolve_aspect_ratio(self, raw_aspect: str) -> tuple[str, str | None]:
        """
        Проверяет aspect_ratio. Если передан нестандартный формат вроде '2:1' или '9:19.5',
        вычисляет математически ближайший из ALLOWED_ASPECT_RATIOS (например, '2:1' -> '16:9'),
        а не сбрасывает панораму в квадрат '1:1'.
        """
        clean = raw_aspect.strip()
        if clean in ALLOWED_ASPECT_RATIOS:
            return clean, None

        match = re.match(r"^\s*(\d+(?:\.\d+)?)\s*[:/xX]\s*(\d+(?:\.\d+)?)\s*$", clean)
        if match:
            w, h = float(match.group(1)), float(match.group(2))
            if w > 0 and h > 0:
                target = w / h
                closest = min(
                    ALLOWED_ASPECT_RATIOS,
                    key=lambda r: abs(
                        (int(r.split(":")[0]) / int(r.split(":")[1])) - target
                    ),
                )
                return (
                    closest,
                    f"Запрошенный aspect_ratio '{clean}' не поддерживается напрямую, автоматически выбран ближайший по пропорциям '{closest}'.",
                )

        return (
            "1:1",
            f"Некорректный aspect_ratio '{clean}' заменён на стандартный '1:1'.",
        )

    def _file_url_to_data_url(self, url: str) -> str | None:
        """
        Резолвит /api/v1/files/<id>/content в data:image/...;base64 из локального хранилища Open WebUI.
        Нужно, чтобы ссылки на файлы (вместо base64 в БД) продолжали работать как референсы.
        """
        match = FILE_CONTENT_URL_PATTERN.match(url.strip())
        if not match:
            return None
        file_id = match.group(1)
        try:
            try:
                from open_webui.models.files import Files

                file_record = Files.get_file_by_id(file_id)
                if file_record and getattr(file_record, "path", None):
                    p = Path(file_record.path)
                    if p.is_file():
                        raw = p.read_bytes()
                        mime = self._sniff_media_type(raw) or EXT_TO_MIME.get(
                            p.suffix.lstrip(".").lower(), "image/png"
                        )
                        b64 = base64.b64encode(raw).decode("ascii")
                        return f"data:{mime};base64,{b64}"
            except Exception:
                pass

            from open_webui.config import UPLOAD_DIR

            candidates = list(Path(UPLOAD_DIR).glob(f"{file_id}_*"))
            if not candidates:
                return None
            path_obj = candidates[0]
            raw = path_obj.read_bytes()
            mime = self._sniff_media_type(raw) or EXT_TO_MIME.get(
                path_obj.suffix.lstrip(".").lower(), "image/png"
            )
            b64 = base64.b64encode(raw).decode("ascii")
            return f"data:{mime};base64,{b64}"
        except Exception:
            return None

    def _normalize_image_ref(self, url: str) -> str | None:
        """Приводит любой URL/путь картинки из сообщения к виду data:image/... или внешнему http URL."""
        if not isinstance(url, str) or not url.strip():
            return None
        url = url.strip()
        if url.startswith("data:image"):
            return url
        resolved = self._file_url_to_data_url(url)
        if resolved:
            return resolved
        if url.startswith(("http://", "https://")):
            return url
        return None

    async def _store_image_as_file(
        self,
        raw: bytes,
        media_type: str,
        request: Any,
        user_dict: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        """
        Кладёт картинку в файловое хранилище Open WebUI и возвращает запись со ссылкой /api/v1/files/<id>/content.
        Корректно обрабатывает как синхронные, так и асинхронные внутренние функции Open WebUI.
        """
        try:
            if request is None or not user_dict or not user_dict.get("id"):
                return None
            from open_webui.models.users import Users
            from open_webui.routers.images import upload_image

            user_res = Users.get_user_by_id(user_dict["id"])
            user = await user_res if inspect.isawaitable(user_res) else user_res
            if user is None:
                return None

            detected = self._sniff_media_type(raw) or media_type or "image/png"
            upload_res = upload_image(request, raw, detected, {}, user)
            _, image_file = (
                await upload_res if inspect.isawaitable(upload_res) else upload_res
            )

            if not image_file:
                return None
            if isinstance(image_file, dict) and image_file.get("url"):
                return {"type": "image", **image_file}
            file_id = getattr(image_file, "id", None) or (
                image_file.get("id") if isinstance(image_file, dict) else None
            )
            if file_id:
                return {"type": "image", "url": f"/api/v1/files/{file_id}/content"}
            return None
        except Exception:
            return None

    def _extract_images_from_single_message(self, msg: dict[str, Any]) -> list[str]:
        """Извлекает все картинки (content, images, files) из одного объекта сообщения и резолвит внутренние ссылки."""
        msg_found: list[str] = []

        content = msg.get("content")
        if isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get("type") == "image_url":
                    image_url_obj = part.get("image_url")
                    url = (
                        image_url_obj.get("url")
                        if isinstance(image_url_obj, dict)
                        else image_url_obj
                    )
                    if isinstance(url, str) and url:
                        norm = self._normalize_image_ref(url)
                        if norm:
                            msg_found.append(norm)

        msg_images = msg.get("images")
        if isinstance(msg_images, list):
            for img in msg_images:
                if isinstance(img, str) and img:
                    raw_url = (
                        img
                        if img.startswith(("data:", "http", "/api/"))
                        else f"data:image/png;base64,{img}"
                    )
                    norm = self._normalize_image_ref(raw_url)
                    if norm:
                        msg_found.append(norm)

        msg_files = msg.get("files")
        if isinstance(msg_files, list):
            for file_obj in msg_files:
                if isinstance(file_obj, dict) and file_obj.get("type") == "image":
                    url = file_obj.get("url")
                    if isinstance(url, str) and url:
                        norm = self._normalize_image_ref(url)
                        if norm:
                            msg_found.append(norm)

        return msg_found

    def _load_saved_paths_from_text(self, text: str) -> list[str]:
        """
        Ищет в тексте сообщения пути к ранее сохранённым в этом чате генерациям
        ('Сохранено на диск: ...' или 'Сохранено в хранилище Open WebUI: ...') и читает их в data:image/...;base64.
        Сохраняет пробелы внутри имён папок/файлов, отсекая только возможные суффиксы в скобках.
        """
        loaded_data_urls: list[str] = []
        matches = SAVED_PATH_PATTERN.findall(text)
        for match_group in matches:
            raw_paths = [p.strip() for p in match_group.split(",") if p.strip()]
            for raw_p in raw_paths:
                clean_p = raw_p.split(" (")[0].strip().rstrip(".)")
                if clean_p.startswith("/api/v1/files/"):
                    resolved = self._file_url_to_data_url(clean_p)
                    if resolved:
                        loaded_data_urls.append(resolved)
                    continue
                path_obj = Path(clean_p)
                if path_obj.is_file():
                    try:
                        raw = path_obj.read_bytes()
                        mime = self._sniff_media_type(raw) or EXT_TO_MIME.get(
                            path_obj.suffix.lstrip(".").lower(), "image/png"
                        )
                        b64 = base64.b64encode(raw).decode("ascii")
                        loaded_data_urls.append(f"data:{mime};base64,{b64}")
                    except Exception:
                        continue
        return loaded_data_urls

    def _extract_user_images(self, messages: list[dict[str, Any]]) -> list[str]:
        """Собирает оригинальные фото, прикреплённые пользователем."""
        found_urls: list[str] = []
        user_msgs_with_images = 0

        for msg in reversed(messages):
            if msg.get("role") != "user":
                continue

            msg_found = self._extract_images_from_single_message(msg)
            if msg_found:
                found_urls.extend(msg_found)
                user_msgs_with_images += 1
                if (
                    len(found_urls) >= self.valves.max_reference_images
                    or user_msgs_with_images >= self.valves.lookback_user_messages
                ):
                    break

        unique_urls = list(dict.fromkeys(found_urls))
        return unique_urls[: self.valves.max_reference_images]

    def _extract_last_generated_images(
        self, messages: list[dict[str, Any]]
    ) -> list[str]:
        """
        Ищет последнюю сгенерированную в этом чате картинку (из сообщений assistant или tool),
        проверяя вложения files/content и пути, записанные в историю этого чата.
        """
        for msg in reversed(messages):
            if msg.get("role") == "user":
                continue

            direct_imgs = self._extract_images_from_single_message(msg)
            if direct_imgs:
                return direct_imgs[: self.valves.max_reference_images]

            content = msg.get("content")
            if isinstance(content, str) and (
                "Сохранено на диск:" in content
                or "Сохранено в хранилище Open WebUI:" in content
            ):
                disk_imgs = self._load_saved_paths_from_text(content)
                if disk_imgs:
                    return disk_imgs[: self.valves.max_reference_images]

        return []

    def _resolve_reference_images(
        self, messages: list[dict[str, Any]] | None, reference_source: str
    ) -> list[str]:
        """
        Выбирает нужные референсы в зависимости от режима reference_source:
        - 'original': только исходные фото пользователя
        - 'last_generated': последняя сгенерированная картинка (с фоллбэком на оригинал)
        - 'both': последняя сгенерированная картинка + исходные фото пользователя
        - 'none': генерация с нуля без референсов (игнорируя фото в истории чата)
        """
        source = reference_source.lower().strip()
        if source == "none" or not messages:
            return []

        last_msg = messages[-1] if messages else {}
        if last_msg.get("role") == "user":
            fresh_user_imgs = self._extract_images_from_single_message(last_msg)
            if fresh_user_imgs and source == "last_generated":
                source = "original"

        if source == "last_generated":
            gen_imgs = self._extract_last_generated_images(messages)
            if gen_imgs:
                return gen_imgs[: self.valves.max_reference_images]
            return self._extract_user_images(messages)

        if source == "both":
            gen_imgs = self._extract_last_generated_images(messages)
            user_imgs = self._extract_user_images(messages)
            combined = list(dict.fromkeys(gen_imgs + user_imgs))
            return combined[: self.valves.max_reference_images]

        return self._extract_user_images(messages)

    def _atomic_write_unique_file(
        self,
        directory: Path,
        base_stem: str,
        ext: str,
        index: int,
        total: int,
        data: bytes,
    ) -> Path:
        """
        Атомарно создаёт и записывает файл через O_CREAT | O_EXCL, исключая гонку (race condition),
        когда Haiku запускает несколько параллельных вызовов тулзы в одну и ту же секунду.
        """
        directory.mkdir(parents=True, exist_ok=True)
        stem = f"{base_stem}_{index}" if total > 1 else base_stem
        counter = 1

        while True:
            suffix = f"_{counter}" if counter > 1 else ""
            candidate = directory / f"{stem}{suffix}.{ext}"
            try:
                fd = os.open(
                    str(candidate),
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                    0o644,
                )
                try:
                    os.write(fd, data)
                finally:
                    os.close(fd)
                self._claim_ownership(candidate)
                return candidate
            except FileExistsError:
                counter += 1

    def _workspace_entry(self, user_dict: dict[str, Any] | None) -> dict[str, Any] | None:
        """Запись провижионера для пользователя чата (по id, иначе по email)."""
        if not user_dict:
            return None
        try:
            state = json.loads(
                Path(self.valves.workspace_state_path).read_text(encoding="utf-8")
            )
        except Exception:
            return None
        entry = state.get(user_dict.get("id") or "")
        if entry:
            return entry
        email = (user_dict.get("email") or "").strip().lower()
        if not email:
            return None
        for value in state.values():
            if (value.get("email") or "").strip().lower() == email:
                return value
        return None

    def _resolve_save_dir(self, user_dict: dict[str, Any] | None) -> Path:
        """Куда класть картинки: generated_images пользователя, иначе общий save_dir."""
        if self.valves.save_to_user_workspace:
            entry = self._workspace_entry(user_dict)
            if entry and entry.get("dir"):
                try:
                    target = (
                        Path(self.valves.workspace_root)
                        / Path(entry["dir"]).name
                        / "generated_images"
                    )
                    target.mkdir(parents=True, exist_ok=True)
                    with_suppress_chown(target, self.valves)
                    return target
                except Exception:
                    pass
        return Path(self.valves.save_dir)

    def _claim_ownership(self, path: Path) -> None:
        """Файл пишет root контейнера — отдаём его владельцу песочницы."""
        if str(path).startswith(self.valves.workspace_root.rstrip("/") + "/"):
            with_suppress_chown(path, self.valves)

    def _decode_and_optionally_archive(
        self,
        raw_b64_or_data_url: str,
        default_media_type: str,
        index: int,
        total: int,
        batch_timestamp: str,
        save_dir: Path | None = None,
    ) -> tuple[str, Path | None, bytes, str]:
        """
        Декодирует base64, определяет реальный MIME по magic-байтам и (если включён save_archive_copy)
        атомарно сохраняет архивную копию на диск с именем по UTC+3.
        """
        media_type = default_media_type or "image/png"
        b64_payload = raw_b64_or_data_url

        if raw_b64_or_data_url.startswith("data:"):
            header, _, b64_payload = raw_b64_or_data_url.partition(",")
            if ";" in header and header.startswith("data:"):
                media_type = header[5:].split(";", 1)[0] or media_type

        image_bytes = base64.b64decode(b64_payload)
        actual_media_type = self._sniff_media_type(image_bytes) or media_type
        ext = MIME_TO_EXT.get(actual_media_type.lower(), "png")

        file_path: Path | None = None
        if self.valves.save_archive_copy:
            file_path = self._atomic_write_unique_file(
                directory=save_dir or Path(self.valves.save_dir),
                base_stem=batch_timestamp,
                ext=ext,
                index=index,
                total=total,
                data=image_bytes,
            )

        data_url = f"data:{actual_media_type};base64,{b64_payload}"
        return data_url, file_path, image_bytes, actual_media_type

    async def _download_and_optionally_archive(
        self,
        client: httpx.AsyncClient,
        url: str,
        index: int,
        total: int,
        batch_timestamp: str,
        save_dir: Path | None = None,
    ) -> tuple[Path | None, bytes, str] | None:
        """
        Если модель вернула внешний URL вместо b64_json — скачивает байты и опционально кладёт в архив.
        """
        try:
            resp = await client.get(url)
            resp.raise_for_status()
        except Exception:
            return None

        raw_bytes = resp.content
        header_type = (
            resp.headers.get("content-type", "image/png").split(";", 1)[0].strip()
        )
        actual_media_type = self._sniff_media_type(raw_bytes) or header_type
        ext = MIME_TO_EXT.get(actual_media_type.lower(), "png")

        file_path: Path | None = None
        if self.valves.save_archive_copy:
            file_path = self._atomic_write_unique_file(
                directory=save_dir or Path(self.valves.save_dir),
                base_stem=batch_timestamp,
                ext=ext,
                index=index,
                total=total,
                data=raw_bytes,
            )

        return file_path, raw_bytes, actual_media_type

    @staticmethod
    def _format_clean_error(status_code: int, raw_text: str) -> str:
        """Форматирует лаконичное сообщение об ошибке без гигантских стектрейсов LiteLLM."""
        short_reason = raw_text.strip()
        try:
            parsed = json.loads(raw_text)
            if isinstance(parsed, dict):
                err = parsed.get("error")
                if isinstance(err, dict) and err.get("message"):
                    short_reason = str(err["message"]).splitlines()[0]
                elif isinstance(err, str):
                    short_reason = err
        except Exception:
            pass
        if len(short_reason) > 220:
            short_reason = short_reason[:220] + "..."
        return f"Ошибка генерации (HTTP {status_code}): {short_reason}"

    @staticmethod
    def _build_storage_summary(
        disk_paths: list[str],
        webui_urls: list[str],
        inline_count: int,
    ) -> tuple[str, str]:
        """
        Формирует:
        1) короткую метку для статус-бара UI (без дублей 'webui_storage, webui_storage'),
        2) честную фразу для возврата в LLM, где точка ставится СТРОГО сразу после списка путей,
           чтобы SAVED_PATH_PATTERN парсил чистые пути без скобок и пояснений.
        """
        if disk_paths:
            ui_label = ", ".join(Path(p).name for p in disk_paths)
        elif webui_urls:
            ui_label = (
                f"webui_storage: {len(webui_urls)}"
                if len(webui_urls) > 1
                else "webui_storage"
            )
        else:
            ui_label = f"inline: {inline_count}" if inline_count > 1 else "inline"

        if disk_paths and webui_urls:
            llm_clause = (
                f"Сохранено на диск: {', '.join(disk_paths)}. "
                "Копия также сохранена в файловое хранилище Open WebUI. "
            )
        elif disk_paths:
            llm_clause = f"Сохранено на диск: {', '.join(disk_paths)}. "
        elif webui_urls:
            llm_clause = f"Сохранено в хранилище Open WebUI: {', '.join(webui_urls)}. "
        else:
            llm_clause = (
                "Внимание: сохранено только как inline-вложение в сообщении "
                "(запись на диск и в файловое хранилище Open WebUI не выполнена). "
            )

        return ui_label, llm_clause

    async def generate_image_from_reference(
        self,
        prompt: str,
        aspect_ratio: str = "1:1",
        reference_source: ReferenceSourceLiteral = "original",
        __messages__: list[dict[str, Any]] | None = None,
        __event_emitter__: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
        __request__: Any = None,
        __user__: dict[str, Any] | None = None,
    ) -> str:
        """
        Генерирует новое изображение или редактирует/объединяет фотографии из чата.
        Вызывай этот инструмент КАЖДЫЙ РАЗ, когда пользователь просит изменить фото, доработать сгенерированный результат,
        объединить людей/объекты с разных фото, улучшить композицию, стилизовать или сгенерировать изображение.
        Перед вызовом переведи и обогати запрос пользователя в детальный промпт на английском языке.

        КРИТИЧЕСКИ ВАЖНЫЕ ПРАВИЛА:
        1. ВЫБОР ИСТОЧНИКА РЕФЕРЕНСА (параметр reference_source):
           - "original" (по умолчанию): используй при первой генерации по фото, при выборе нового стиля/сценария, или когда пользователь просит переделать заново с исходника.
           - "last_generated": используй, когда пользователь просит ДОРАБОТАТЬ ИМЕННО ТО, ЧТО ТОЛЬКО ЧТО СГЕНЕРИРОВАЛОСЬ (например: "на этой картинке подвинь кота выше", "убери лишний предмет с результата", "сделай полученный вариант чуть светлее").
           - "both": используй, когда нужно доработать последнюю генерацию, но при этом вернуть/сверить черты лица или детали с оригинального фото пользователя.
           - "none": используй, когда пользователь просит сгенерировать совершенно новую картинку С НУЛЯ, не используя ранее прикреплённые в этом чате фотографии.
        2. СОХРАНЕНИЕ ЛИЦ И МИМИКИ (если на фото люди и не требуется мультяшная стилизация):
           Обязательно включай в английский prompt требование:
           "Strictly preserve the exact facial identity, authentic micro-expressions, natural original smile shape, eye squint, skin pores, and natural skin texture of each person from the reference photos. Do NOT apply beauty filters, plastic skin smoothing, or artificial symmetrical smiles. Sharp focus, crisp high-definition details."
        3. ЗАПРЕТ НА ЭЛЕМЕНТЫ ИНТЕРФЕЙСА (если просят обои, заставку на айфон/телефон, аватарку):
           НИКОГДА не пиши в prompt слова "iPhone", "smartphone", "lockscreen", "wallpaper", "screen", "UI", "clock", "widgets", "icons", "mockup".
           Вместо этого задай aspect_ratio="9:16", а в prompt опиши только сам арт и композицию:
           "vertical artwork composition with clean uncluttered negative space in the upper third of the frame, no text, no UI elements, no borders".
        4. ПРОПОРЦИИ КАДРА:
           Если пользователь редактирует горизонтальное групповое/семейное фото и не просил менять формат — передавай aspect_ratio="4:3", "3:2" или "16:9". Если вертикальный портрет — "3:4", "2:3" или "4:5". Если заставку на телефон — "9:16".

        :param prompt: Подробный промпт на английском языке с соблюдением правил сохранения мимики и отсутствия UI-элементов.
        :param aspect_ratio: Соотношение сторон: "1:1", "16:9", "9:16", "4:3", "3:4", "4:5", "5:4", "3:2", "2:3", "21:9". По умолчанию "1:1".
        :param reference_source: Какие фото брать за основу: "original" (исходные фото пользователя), "last_generated" (последнюю сгенерированную картинку для доработки), "both" (и генерацию, и оригинал) или "none" (с нуля без фото).
        """
        if not self.valves.litellm_api_key:
            return "Ошибка конфигурации: не задан litellm_api_key в настройках инструмента (Valves)."

        reference_urls = self._resolve_reference_images(
            messages=__messages__,
            reference_source=reference_source,
        )

        resolution = (
            self.valves.default_resolution.upper()
            if self.valves.default_resolution.upper() in ALLOWED_RESOLUTIONS
            else "2K"
        )

        clean_aspect, aspect_warning = self._resolve_aspect_ratio(str(aspect_ratio))

        if __event_emitter__:
            if aspect_warning:
                await __event_emitter__(
                    {
                        "type": "status",
                        "data": {
                            "description": aspect_warning,
                            "done": False,
                        },
                    }
                )

            source_label = {
                "original": "оригинал",
                "last_generated": "предыдущая генерация",
                "both": "генерация + оригинал",
                "none": "без референса",
            }.get(reference_source.lower().strip(), "референс")

            status_text = (
                f"Отправляю {len(reference_urls)} фото ({source_label}) в LiteLLM ({self.valves.model}, {resolution}, {clean_aspect})..."
                if reference_urls
                else f"Генерирую изображение через LiteLLM ({self.valves.model}, {resolution}, {clean_aspect})..."
            )
            await __event_emitter__(
                {
                    "type": "status",
                    "data": {"description": status_text, "done": False},
                }
            )

        extra_body: dict[str, Any] = {
            "resolution": resolution,
            "aspect_ratio": clean_aspect,
        }

        if reference_urls:
            extra_body["input_references"] = [
                {"type": "image_url", "image_url": {"url": url}}
                for url in reference_urls
            ]

        payload: dict[str, Any] = {
            "model": self.valves.model,
            "prompt": prompt,
            **extra_body,
            "extra_body": extra_body,
        }

        headers = {
            "Authorization": f"Bearer {self.valves.litellm_api_key}",
            "Content-Type": "application/json",
        }

        timeout_cfg = httpx.Timeout(
            connect=15.0,
            read=float(self.valves.timeout_seconds),
            write=30.0,
            pool=15.0,
        )
        max_attempts = max(1, self.valves.max_retries)
        total_deadline = float(max(30, self.valves.total_deadline_seconds))

        disk_paths: list[str] = []
        webui_urls: list[str] = []
        inline_count = 0
        output_files: list[dict[str, str]] = []

        try:
            async with asyncio.timeout(total_deadline):
                async with httpx.AsyncClient(timeout=timeout_cfg) as client:
                    response: httpx.Response | None = None
                    last_exc: Exception | None = None

                    for attempt in range(1, max_attempts + 1):
                        last_exc = None
                        try:
                            response = await client.post(
                                self.valves.litellm_api_url,
                                headers=headers,
                                json=payload,
                            )
                            if (
                                response.status_code in RETRYABLE_STATUS_CODES
                                and attempt < max_attempts
                            ):
                                delay = 1.5 * attempt + random.uniform(0.1, 0.9)
                                if __event_emitter__:
                                    await __event_emitter__(
                                        {
                                            "type": "status",
                                            "data": {
                                                "description": (
                                                    f"Шлюз вернул {response.status_code}, повтор "
                                                    f"{attempt + 1}/{max_attempts} через {delay:.1f}с..."
                                                ),
                                                "done": False,
                                            },
                                        }
                                    )
                                await asyncio.sleep(delay)
                                continue
                            break
                        except (
                            httpx.TimeoutException,
                            httpx.ConnectError,
                            httpx.RemoteProtocolError,
                        ) as exc:
                            last_exc = exc
                            if attempt < max_attempts:
                                delay = 1.5 * attempt + random.uniform(0.1, 0.9)
                                if __event_emitter__:
                                    await __event_emitter__(
                                        {
                                            "type": "status",
                                            "data": {
                                                "description": (
                                                    f"Сбой сети ({type(exc).__name__}), повтор "
                                                    f"{attempt + 1}/{max_attempts} через {delay:.1f}с..."
                                                ),
                                                "done": False,
                                            },
                                        }
                                    )
                                await asyncio.sleep(delay)
                                continue
                            raise

                    if response is None:
                        raise last_exc or RuntimeError("Нет ответа от LiteLLM")

                    if response.status_code != 200:
                        clean_err = self._format_clean_error(
                            response.status_code, response.text
                        )
                        if __event_emitter__:
                            await __event_emitter__(
                                {
                                    "type": "status",
                                    "data": {
                                        "description": f"Ошибка LiteLLM ({response.status_code})",
                                        "done": True,
                                    },
                                }
                            )
                        return clean_err

                    data = response.json()
                    images_data = data.get("data") or []
                    if not images_data:
                        return "Ошибка: LiteLLM вернул пустой список изображений."

                    batch_timestamp = datetime.now(MSK_TZ).strftime(
                        self.valves.filename_format
                    )
                    total_images = len(images_data)
                    target_dir = self._resolve_save_dir(__user__)

                    for idx, item in enumerate(images_data, start=1):
                        b64_json = item.get("b64_json")
                        media_type = item.get("media_type", "image/png")
                        if b64_json:
                            data_url, saved_path, img_bytes, actual_mime = (
                                self._decode_and_optionally_archive(
                                    raw_b64_or_data_url=b64_json,
                                    default_media_type=media_type,
                                    index=idx,
                                    total=total_images,
                                    batch_timestamp=batch_timestamp,
                                    save_dir=target_dir,
                                )
                            )
                            stored = await self._store_image_as_file(
                                img_bytes, actual_mime, __request__, __user__
                            )
                            if saved_path:
                                disk_paths.append(str(saved_path))
                            if stored and stored.get("url"):
                                webui_urls.append(str(stored["url"]))
                                output_files.append(stored)
                            else:
                                inline_count += 1
                                output_files.append({"type": "image", "url": data_url})

                        elif item.get("url"):
                            img_url = item["url"]
                            dl_res = await self._download_and_optionally_archive(
                                client=client,
                                url=img_url,
                                index=idx,
                                total=total_images,
                                batch_timestamp=batch_timestamp,
                                save_dir=target_dir,
                            )
                            stored = None
                            saved_path = None
                            if dl_res:
                                saved_path, img_bytes, actual_mime = dl_res
                                stored = await self._store_image_as_file(
                                    img_bytes,
                                    actual_mime,
                                    __request__,
                                    __user__,
                                )
                            if saved_path:
                                disk_paths.append(str(saved_path))
                            if stored and stored.get("url"):
                                webui_urls.append(str(stored["url"]))
                                output_files.append(stored)
                            else:
                                inline_count += 1
                                output_files.append({"type": "image", "url": img_url})

        except httpx.ConnectTimeout:
            if __event_emitter__:
                await __event_emitter__(
                    {
                        "type": "status",
                        "data": {
                            "description": f"Шлюз LiteLLM не отвечает на подключение ({max_attempts} попыток)",
                            "done": True,
                        },
                    }
                )
            return f"Ошибка: шлюз LiteLLM не отвечает на TCP-подключение (connect timeout после {max_attempts} попыток)."
        except httpx.ConnectError:
            if __event_emitter__:
                await __event_emitter__(
                    {
                        "type": "status",
                        "data": {
                            "description": f"Шлюз LiteLLM недоступен ({max_attempts} попыток)",
                            "done": True,
                        },
                    }
                )
            return f"Ошибка: шлюз LiteLLM недоступен (connect failed после {max_attempts} попыток)."
        except httpx.TimeoutException:
            if __event_emitter__:
                await __event_emitter__(
                    {
                        "type": "status",
                        "data": {
                            "description": "Превышен таймаут ожидания ответа от LiteLLM",
                            "done": True,
                        },
                    }
                )
            return f"Ошибка: LiteLLM не ответил (таймаут {self.valves.timeout_seconds}с после {max_attempts} попыток)."
        except TimeoutError:
            if __event_emitter__:
                await __event_emitter__(
                    {
                        "type": "status",
                        "data": {
                            "description": f"Превышен общий лимит времени ({int(total_deadline)}с)",
                            "done": True,
                        },
                    }
                )
            return f"Ошибка: превышен общий лимит времени генерации ({int(total_deadline)} сек)."
        except Exception as exc:
            return f"Ошибка при генерации или сохранении изображения: {type(exc).__name__}: {exc}"

        if not output_files:
            return "Не удалось извлечь изображение из ответа LiteLLM."

        ui_storage_label, llm_storage_clause = self._build_storage_summary(
            disk_paths=disk_paths,
            webui_urls=webui_urls,
            inline_count=inline_count,
        )

        if __event_emitter__:
            await __event_emitter__(
                {
                    "type": "files",
                    "data": {"files": output_files},
                }
            )
            await __event_emitter__(
                {
                    "type": "status",
                    "data": {
                        "description": f"Готово! ({resolution}, {clean_aspect}) Сохранено: {ui_storage_label}",
                        "done": True,
                    },
                }
            )

        aspect_clause = f" {aspect_warning}" if aspect_warning else ""

        return (
            f"Успешно сгенерировано изображений: {len(output_files)} "
            f"(разрешение {resolution}, формат {clean_aspect}, режим референса: {reference_source})."
            f"{aspect_clause} "
            f"{llm_storage_clause}"
            f"Использовано референсных фото: {len(reference_urls)}. "
            "Изображение уже прикреплено и показано пользователю в чате — "
            "НЕ пытайся вставлять markdown-ссылку на картинку, просто кратко опиши результат."
        )

