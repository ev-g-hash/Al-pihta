"""Веб-поиск материалов в интернете.

Использует бесплатную библиотеку ddgs — без API-ключей. Никаких фильтров по
доменам: ищем по всей сети, а порядок результатов определяет релевантность
вопросу (совпадение слов и позиция в выдаче поисковика).
"""

from __future__ import annotations

import logging
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from ddgs import DDGS

logger = logging.getLogger(__name__)


@dataclass
class SearchResult:
    """Один результат поиска."""

    title: str
    url: str
    snippet: str = ""


# Сколько результатов запрашивать у поисковика на один запрос.
MAX_RESULTS_PER_QUERY = 6
# Сколько итоговых источников отдаём на загрузку. Берём с запасом: часть
# сайтов не отдаст текст (страницы на JavaScript), и они будут отброшены
# на этапе загрузки.
MAX_SOURCES = 8

# Поисковые движки, которые используем. В ddgs 9.x DuckDuckGo больше нет —
# его заменили bing, brave, grokipedia, mojeek, wikipedia. Ключевое слово
# "auto" просит ddgs прогнать все доступные движки параллельно, что быстрее.
DEFAULT_BACKENDS = "auto"


def _backends() -> str:
    return os.getenv("SEARCH_BACKENDS", DEFAULT_BACKENDS).strip() or DEFAULT_BACKENDS

# Стоп-слова не участвуют в оценке релевантности.
_RELEVANCE_STOP_WORDS = {
    "что", "как", "где", "когда", "почему", "зачем", "какой", "какая", "какие",
    "какое", "мне", "меня", "мой", "моя", "это", "этот", "эта", "если", "чтобы",
    "хотя", "был", "была", "было", "были", "есть", "буду", "будет", "хочу",
    "скажите", "подскажите", "пожалуйста", "прошу", "помогите",
}


def _normalize_url(url: str) -> str:
    """Приводит URL к виду, удобному для сравнения (для дедупликации).

    Схему отбрасываем: поисковик возвращает один и тот же документ то как
    http, то как https — без этого он считался бы двумя разными источниками.
    Служебные параметры поиска (# и ?) тоже не учитываются.
    """
    from urllib.parse import urlparse, urlunparse

    try:
        parsed = urlparse(url.strip())
        path = parsed.path.rstrip("/")
        return urlunparse(("", parsed.netloc.lower(), path, "", "", ""))
    except Exception:
        return url.strip()


def _keywords(text: str) -> list[str]:
    """Извлекает содержательные слова для оценки релевантности."""
    words = re.findall(r"[а-яёa-z0-9]+", (text or "").lower())
    return [w for w in words if len(w) > 3 and w not in _RELEVANCE_STOP_WORDS]


def _relevance(result: SearchResult, position: int, keywords: list[str]) -> float:
    """Оценивает полезность результата.

    Учитывается: вхождение ключевых слов вопроса в заголовок и описание, а
    также позиция в выдаче поисковика. Оценке домена внимания не отдаём —
    ищем по всей сети, а не по заранее выбранному списку сайтов.
    """
    haystack = f"{result.title} {result.snippet}".lower()
    hits = sum(1 for word in keywords if word in haystack)
    score = min(hits, 6) * 1.5

    # Чем выше результат в выдаче, тем он чуть лучше.
    score += max(0, MAX_RESULTS_PER_QUERY - position) * 0.2
    return score


def search(query: str, max_results: int = MAX_RESULTS_PER_QUERY) -> list[SearchResult]:
    """Ищет в интернете по одному запросу и возвращает список результатов.

    Пустой ответ поисковика обычно означает временный отказ (неожиданно
    много параллельных обращений), поэтому делаем один повтор с паузой.
    """
    query = (query or "").strip()
    if not query:
        return []

    for attempt in range(2):
        results = _search_once(query, max_results)
        if results:
            return results
        if attempt == 0:
            time.sleep(1.0)
    return []


def _search_once(query: str, max_results: int) -> list[SearchResult]:
    """Одна попытка обращения к поисковику."""
    try:
        raw = DDGS().text(
            query,
            max_results=max_results,
            region="ru-ru",
            backend=_backends(),
        )
    except Exception as exc:  # ddgs может падать при блокировке/сети
        logger.info("Веб-поиск по запросу '%s' не дал результатов: %s", query, exc)
        return []

    return [
        SearchResult(
            title=(item.get("title") or "").strip(),
            url=(item.get("href") or item.get("url") or "").strip(),
            snippet=(item.get("body") or item.get("snippet") or "").strip(),
        )
        for item in raw
        if (item.get("href") or item.get("url"))
    ]


def search_multi(
    queries: list[str],
    original_question: str = "",
    max_sources: int = MAX_SOURCES,
) -> list[SearchResult]:
    """Ищет сразу по нескольким запросам и возвращает лучший набор результатов.

    Результаты объединяются, дедуплицируются по URL и ранжируются по
    релевантности исходному вопросу. Так один и тот же вопрос покрывается и по
    норме закона, и по судебной практике.
    """
    queries = [q.strip() for q in queries if q and q.strip()]
    if not queries:
        return []

    # Поиск по всем формулировкам, но не слишком агрессивно параллельно:
    # поисковик отвечает пустой выдачей при большом числе одновременных обращений.
    with ThreadPoolExecutor(max_workers=min(len(queries), 2)) as executor:
        batches = list(executor.map(search, queries))

    keywords = _keywords(original_question or " ".join(queries))
    best: dict[str, tuple[SearchResult, float]] = {}

    for results in batches:
        for position, result in enumerate(results):
            key = _normalize_url(result.url)
            score = _relevance(result, position, keywords)
            existing = best.get(key)
            if existing is None or score > existing[1]:
                best[key] = (result, score)

    ranked = sorted(best.values(), key=lambda pair: pair[1], reverse=True)
    selected = [result for result, _ in ranked[:max_sources]]

    logger.info(
        "Мультизапрос: %d формулировок, %d уникальных результатов, выбрано %d",
        len(queries),
        len(best),
        len(selected),
    )
    return selected

