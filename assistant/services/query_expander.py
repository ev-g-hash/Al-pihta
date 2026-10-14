"""Преобразование вопроса пользователя в несколько поисковых запросов.

Приём называется multi-query retrieval: вместо одного запроса к поисковику
делаем несколько формулировок, чтобы покрыть разные аспекты вопроса — норму
закона, судебную практику, порядок действий. Это заметно повышает полноту
найдённых источников.

Основной способ — модель переформулирует вопрос. Если вызов модели не удался,
используется простая эвристика, чтобы поиск работал всегда.
"""

from __future__ import annotations

import logging
import os
import re
import time

from .llm import ChatMessage, LLMError, get_provider
from .prompts import QUERY_EXPANSION_PROMPT

logger = logging.getLogger(__name__)

# Сколько запросов генерируем по умолчанию.
DEFAULT_MAX_QUERIES = 3
# Ограничения на длину одного поискового запроса.
MIN_QUERY_CHARS = 4
MAX_QUERY_CHARS = 120

# Простые русские стоп-слова, которые не несут смысла для поиска.
_STOP_WORDS = {
    "я", "мне", "меня", "мой", "моя", "моё", "мы", "наш", "он", "она", "оно",
    "они", "ты", "тебя", "твой", "вы", "вас", "ваш", "это", "этот", "эта",
    "эти", "как", "что", "чем", "чтобы", "если", "хотя", "но", "и", "или",
    "а", "у", "о", "об", "в", "во", "на", "с", "со", "из", "от", "до", "по",
    "для", "про", "при", "за", "к", "не", "ни", "да", "нет", "бы", "же", "ли",
    "был", "была", "было", "были", "есть", "буду", "будет", "мне", "него",
    "неё", "них", "нее", "сам", "сама", "очень", "уже", "ещё", "только",
    "хочу", "хотел", "скажите", "подскажите", "помогите", "расскажи",
    "подскажи", "пожалуйста", "вопрос", "ситуация", "случай",
}

# Шаблон для удаления нумерации и маркеров списка в ответе модели.
_BULLET_RE = re.compile(r"^[\s\-–—•*]*((\d+)[.)]|[-–—•*])\s*")


def _clean_line(line: str) -> str:
    """Приводит строку ответа модели к виду чистого поискового запроса."""
    line = line.strip()
    if not line:
        return ""
    line = _BULLET_RE.sub("", line).strip()
    line = line.strip("\"'“”«»:;.,")
    # Запрещаем модели «хитрить» и возвращать пояснения вместо запроса.
    if len(line) < MIN_QUERY_CHARS or len(line) > MAX_QUERY_CHARS:
        return ""
    return line


def _parse_queries(answer: str, max_queries: int) -> list[str]:
    """Разбирает ответ модели на список поисковых запросов."""
    queries: list[str] = []
    for line in (answer or "").splitlines():
        cleaned = _clean_line(line)
        if cleaned:
            queries.append(cleaned)
        if len(queries) >= max_queries:
            break
    return queries


def _normalize_base(question: str, max_words: int = 8) -> str:
    """Сжимает вопрос до короткой поисковой фразы из содержательных слов."""
    words = re.findall(r"[а-яёa-z0-9-]+", question.lower())
    meaningful = [w for w in words if w not in _STOP_WORDS and len(w) > 2]
    if not meaningful:
        meaningful = words
    return " ".join(meaningful[:max_words])


def _heuristic_queries(question: str, max_queries: int) -> list[str]:
    """Запасной способ: формирует запросы простыми подстановками.

    Используется, если модель не смогла переформулировать вопрос.
    """
    base = _normalize_base(question)
    if not base:
        return []

    candidates = [
        base,
        f"{base} судебная практика Верховный Суд РФ",
        f"{base} закон статья РФ",
        f"{base} порядок действий",
    ]
    return candidates[:max_queries]


def _dedupe(queries: list[str]) -> list[str]:
    """Убирает повторы без учёта регистра, сохраняя порядок."""
    seen: set[str] = set()
    unique: list[str] = []
    for query in queries:
        key = query.lower().strip()
        if key and key not in seen:
            seen.add(key)
            unique.append(query)
    return unique


def _enabled() -> bool:
    """Мультизапрос можно выключить переменной MULTI_QUERY в .env."""
    return os.getenv("MULTI_QUERY", "True").lower() in ("1", "true", "yes")


def expand_query(question: str, max_queries: int = DEFAULT_MAX_QUERIES) -> list[str]:
    """Возвращает список поисковых запросов по вопросу пользователя.

    Всегда содержит хотя бы один запрос (исходную формулировку), чтобы поиск
    работал даже при отказах модели.
    """
    question = (question or "").strip()
    if not question:
        return []

    if not _enabled():
        return [_normalize_base(question) or question[:MAX_QUERY_CHARS]]

    started = time.monotonic()
    queries: list[str] = []
    try:
        provider = get_provider()
        prompt = QUERY_EXPANSION_PROMPT.format(
            max_queries=max_queries, question=question[:800]
        )
        answer = provider.chat(
            [
                ChatMessage(role="system", content="Ты составляешь поисковые запросы."),
                ChatMessage(role="user", content=prompt),
            ],
            temperature=0.1,
            max_tokens=120,
        )
        queries = _parse_queries(answer, max_queries)
    except LLMError as exc:
        logger.warning("Не удалось расширить запрос через модель: %s", exc)

    if not queries:
        queries = _heuristic_queries(question, max_queries)
        logger.info("Использована эвристика для расширения запроса")

    # Добавляем исходную формулировку в конец: она надёжнее выдуманных.
    base = _normalize_base(question)
    if base:
        queries.append(base)

    result = _dedupe(queries)[:max_queries]
    logger.info(
        "Расширение запроса заняло %.1f с, получено %d запросов",
        time.monotonic() - started,
        len(result),
    )
    return result
