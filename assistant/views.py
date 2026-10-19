"""Представления приложения assistant."""

from __future__ import annotations

import json

from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import render
from django.template.loader import render_to_string
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_GET, require_POST

from .services import calculations, file_reader, pipeline
from .services.file_reader import UserFile
from .services.llm import LLMError

SESSION_HISTORY_KEY = "chat_history"

# Шаблон таблицы расчёта. Рендерит его Django, поэтому текст модели
# экранируется и не может превратиться в разметку.
CALC_TABLE_TEMPLATE = "assistant/widgets/calc_table.html"


def _get_history(request: HttpRequest) -> list[dict]:
    return request.session.get(SESSION_HISTORY_KEY, [])


def _save_history(request: HttpRequest, history: list[dict]) -> None:
    request.session[SESSION_HISTORY_KEY] = history
    request.session.modified = True


# Сколько текста приложенного файла храним в истории. Нужно, чтобы модель
# видела документ и в следующем сообщении: пользователь мог приложить договор
# одним сообщением, а уточняющий вопрос задать следующим.
DOC_TEXT_IN_HISTORY = 2000


def _serialize_files(files: list[UserFile]) -> list[dict]:
    """В историю попадает текст файла (обрезанный), а не только имя и тип.

    Без текста уточняющий вопрос «а что в пункте 5 этого договора?» повисал бы
    в воздухе: модель видела бы только название файла. Обрезка нужна, потому что
    размер сессии ограничен, а в диалоге может быть несколько документов.
    """
    return [
        {
            "name": f.name,
            "kind": f.kind,
            "text": f.text[:DOC_TEXT_IN_HISTORY],
        }
        for f in files
    ]


def _attach_table(entry: dict) -> dict:
    """Добавляет готовый HTML таблицы к сообщению перед выдачей браузеру.

    В истории хранятся только данные (числа и подписи), а разметка создаётся
    шаблоном Django в момент ответа. Так в сессии не лежит HTML, и таблица
    всегда отрисуется актуальным шаблоном.
    """
    calc = entry.get("calc")
    if not calc:
        return entry
    enriched = dict(entry)
    enriched["table_html"] = render_to_string(
        CALC_TABLE_TEMPLATE, {"calc": calc}
    ).strip()
    return enriched


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
    }

    # Если в разборе есть деньги и проценты — выделяем расчёт в таблицу.
    # Ошибка здесь не должна влиять на ответ, поэтому extract сам всё ловит.
    calc = calculations.extract(
        user_text,
        result.text,
        " ".join(doc.text for doc in files),
    )
    if calc:
        answer["calc"] = calc.to_dict()

    if file_errors:
        answer["notices"] = file_errors
    history.append(answer)
    return answer


def _history_json(history: list[dict]) -> str:
    r"""Сериализует историю для вставки в <script type="application/json">.

    Текст приложенных документов в страницу не отдаём: он нужен модели на
    сервере, а интерфейсу достаточно имени и типа файла.

    Закрываемую последовательность </ экранируем как <\/: иначе текст ответа,
    в котором встретится «</script>», преждевременно закрыл бы блок и текст
    ушёл бы в разметку. В JSON последовательность <\/ законна и читается
    браузером обратно как </, так что данные не искажаются.
    """
    public = []
    for item in history:
        entry = dict(item)
        if entry.get("files"):
            entry["files"] = [
                {"name": f.get("name", ""), "kind": f.get("kind", "")}
                for f in entry["files"]
            ]
        public.append(_attach_table(entry))
    return json.dumps(public, ensure_ascii=False).replace("</", r"<\/")


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
            "history_json": _history_json(history),
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
    return JsonResponse({"answer": _attach_table(answer)})


@require_POST
def api_clear(request: HttpRequest) -> JsonResponse:
    request.session.pop(SESSION_HISTORY_KEY, None)
    return JsonResponse({"ok": True})


def _transcript(request: HttpRequest) -> str:
    """Собирает переписку в читаемый текст для сохранения в файл.

    Текст приложенных документов не включаем — в файле нужны реплики диалога,
    от файлов остаётся только название.
    """
    lines = ["Переписка с помощником", ""]
    for item in _get_history(request):
        role = "Я" if item.get("role") == "user" else "Помощник"
        content = (item.get("content") or "").strip()
        if content:
            lines.append(f"{role}:")
            lines.append(content)
            lines.append("")
        for f in item.get("files") or []:
            name = f.get("name") or ""
            if name:
                lines.append(f"{role}: [приложен файл {name}]")
                lines.append("")
    return "\n".join(lines).strip() + "\n"


@require_GET
def api_export(request: HttpRequest) -> HttpResponse:
    """Отдаёт переписку текстовым файлом для сохранения пользователем."""
    transcript = _transcript(request)
    if not transcript.strip():
        return HttpResponse("Переписка пуста.", content_type="text/plain; charset=utf-8")

    response = HttpResponse(transcript, content_type="text/plain; charset=utf-8")
    response["Content-Disposition"] = 'attachment; filename="perepiska.txt"'
    return response
