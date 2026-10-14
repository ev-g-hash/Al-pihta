"""Представления приложения assistant."""

from __future__ import annotations

import json

from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import render
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_GET, require_POST

from .services import file_reader, pipeline
from .services.crawler import Source
from .services.file_reader import UserFile
from .services.llm import LLMError

SESSION_HISTORY_KEY = "chat_history"


def _get_history(request: HttpRequest) -> list[dict]:
    return request.session.get(SESSION_HISTORY_KEY, [])


def _save_history(request: HttpRequest, history: list[dict]) -> None:
    request.session[SESSION_HISTORY_KEY] = history
    request.session.modified = True


def _serialize_sources(sources: list[Source]) -> list[dict]:
    return [
        {"number": src.number, "title": src.title, "url": src.url}
        for src in sources
    ]


def _serialize_files(files: list[UserFile]) -> list[dict]:
    """В историю попадают только имена и типы: сам текст нужен модели один раз,
    в интерфейсе он больше не участвует, а сессия Django ограничена по размеру."""
    return [{"name": f.name, "kind": f.kind} for f in files]


def _ask(
    history: list[dict],
    user_text: str,
    files: list[UserFile],
    file_errors: list[str],
) -> dict:
    """Задаёт вопрос модели и возвращает готовый ответ.

    При ошибке провайдера история откатывается, чтобы повторная отправка того же
    вопроса не породила дубль.
    """
    entry: dict = {"role": "user", "content": user_text}
    attachments = _serialize_files(files)
    if attachments:
        entry["files"] = attachments
    history.append(entry)

    try:
        result = pipeline.answer(history, documents=files)
    except LLMError:
        history.pop()
        raise

    answer = {
        "role": "assistant",
        "content": result.text,
        "sources": _serialize_sources(result.sources),
    }
    if file_errors:
        answer["notices"] = file_errors
    history.append(answer)
    return answer


@require_GET
@ensure_csrf_cookie  # JS берёт токен из cookie для запросов к /api/chat/.
def chat(request: HttpRequest) -> HttpResponse:
    history = _get_history(request)
    return render(
        request,
        "assistant/chat.html",
        {
            # Историю отдаём одним JSON-блоком: фронтенд восстанавливает по ней
            # ленту тем же кодом, которым рисует новые сообщения.
            "history_json": json.dumps(history, ensure_ascii=False).replace("</", "<\u005c\u005c/"),
            # Подсказка про OCR: без Tesseract текст с картинок не извлечь.
            "ocr_ready": file_reader.ocr_available(),
        },
    )


@require_POST
def api_chat(request: HttpRequest) -> JsonResponse:
    """AJAX-эндпоинт: принимает вопрос и прикреплённые файлы, возвращает ответ.

    Запрос приходит как multipart/form-data, а не JSON: файлы в JSON не
    передать, а держать два отдельных пути обработки смысла нет.
    """
    user_text = request.POST.get("message", "").strip()
    uploads = request.FILES.getlist("files")

    if not user_text and not uploads:
        return JsonResponse(
            {"error": "Задайте вопрос или приложите документ."}, status=400
        )

    files, file_errors = file_reader.read_uploads(uploads)
    if uploads and not files and not user_text:
        # Приложены только файлы, из которых не удалось прочитать текст,
        # а вопроса нет — анализировать нечего.
        return JsonResponse(
            {"error": " ".join(file_errors) or "Не удалось прочитать файлы."},
            status=400,
        )

    history = _get_history(request)
    try:
        answer = _ask(history, user_text, files, file_errors)
    except LLMError as exc:
        return JsonResponse({"error": str(exc)}, status=502)

    _save_history(request, history)
    return JsonResponse({"answer": answer})


@require_POST
def api_clear(request: HttpRequest) -> JsonResponse:
    request.session.pop(SESSION_HISTORY_KEY, None)
    return JsonResponse({"ok": True})
