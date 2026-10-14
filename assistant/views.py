"""Представления приложения assistant."""

from __future__ import annotations

import json

from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import render
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_GET, require_POST

from .services import pipeline
from .services.crawler import Source
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


def _ask(history: list[dict], user_text: str) -> dict:
    """Задаёт вопрос модели и возвращает готовый ответ.

    Вопрос добавляется в историю до обращения к модели (он нужен для поиска и
    контекста), но при ошибке провайдера история откатывается, чтобы повторная
    отправка того же вопроса не породила дубль.
    """
    history.append({"role": "user", "content": user_text})
    try:
        result = pipeline.answer(history)
    except LLMError:
        history.pop()
        raise
    answer = {
        "role": "assistant",
        "content": result.text,
        "sources": _serialize_sources(result.sources),
    }
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
            "history_json": json.dumps(history, ensure_ascii=False).replace("</", "<\u005c/"),
        },
    )


@require_POST
def api_chat(request: HttpRequest) -> JsonResponse:
    """AJAX-эндпоинт: принимает вопрос, возвращает ответ."""
    try:
        payload = json.loads(request.body.decode("utf-8") or "{}")
    except ValueError:
        return JsonResponse({"error": "Ожидался JSON."}, status=400)

    user_text = str(payload.get("message", "")).strip()
    if not user_text:
        return JsonResponse({"error": "Пустой вопрос."}, status=400)

    history = _get_history(request)
    try:
        answer = _ask(history, user_text)
    except LLMError as exc:
        return JsonResponse({"error": str(exc)}, status=502)

    _save_history(request, history)
    return JsonResponse({"answer": answer})


@require_POST
def api_clear(request: HttpRequest) -> JsonResponse:
    request.session.pop(SESSION_HISTORY_KEY, None)
    return JsonResponse({"ok": True})