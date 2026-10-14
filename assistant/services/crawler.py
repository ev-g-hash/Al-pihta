"""Загрузка веб-страниц и извлечение из них чистого текста.

Используется библиотека trafilatura, которая убирает навигацию, рекламу и
прочий «мусор», оставляя основной текст документа (статьи закона, текста
судебного акта и т.п.).
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

import requests
import trafilatura

from .search import SearchResult

logger = logging.getLogger(__name__)

# Представляемся как обычный браузер, иначе часть сайтов блокирует запросы.
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0 Safari/537.36"
)

# Ограничения, чтобы не тянуть слишком много текста в модель.
# GigaChat имеет конечный размер контекста, поэтому вводим общий бюджет
# символов на все источники сразу.
MAX_PAGE_CHARS = 3500
TOTAL_CHAR_BUDGET = 18000
# Страницы с очень коротким текстом бесполезны для ответа: это заглушки,
# страницы с куки-согласием или сайты, полностью собранные на JavaScript.
# Такие источники пропускаем, чтобы не тратить место в контексте модели.
MIN_PAGE_CHARS = 400
FETCH_TIMEOUT = 20
MAX_WORKERS = 5


@dataclass
class Source:
    """Источник, готовый к передаче в модель."""

    number: int
    title: str
    url: str
    text: str


def fetch_text(url: str) -> str:
    """Скачивает страницу и возвращает её очищенный текст (или пустую строку)."""
    try:
        response = requests.get(
            url,
            headers={"User-Agent": USER_AGENT},
            timeout=FETCH_TIMEOUT,
        )
        if response.status_code != 200:
            logger.info("Страница %s вернула код %s", url, response.status_code)
            return ""
        text = trafilatura.extract(
            response.text,
            include_comments=False,
            include_tables=False,
        )
        return (text or "").strip()
    except requests.RequestException as exc:
        logger.info("Не удалось загрузить %s: %s", url, exc)
        return ""
    except Exception as exc:  # trafilatura тоже может падать на кривом HTML
        logger.info("Не удалось разобрать %s: %s", url, exc)
        return ""


def collect_sources(results: list[SearchResult]) -> list[Source]:
    """Загружает страницы параллельно и формирует список источников с текстом.

    Страницы, которые не удалось загрузить или из которых не извлёкся текст,
    пропускаются. Нумерация источников идёт по порядку успешных загрузок.
    Суммарный объём текста ограничен, чтобы не превысить контекст модели.
    """
    if not results:
        return []

    sources: list[Source] = []
    used_chars = 0
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        texts = list(executor.map(lambda r: fetch_text(r.url), results))

    number = 1
    for result, text in zip(results, texts):
        if len(text) < MIN_PAGE_CHARS:
            if text:
                logger.info(
                    "Источник %s даёт слишком мало текста (%d), пропущен",
                    result.url,
                    len(text),
                )
            continue
        remaining = TOTAL_CHAR_BUDGET - used_chars
        if remaining <= 500:
            logger.info("Достигнут бюджет текста, остальные источники пропущены")
            break
        # Последний источник может быть укорочен до остатка бюджета.
        chunk = text[: min(MAX_PAGE_CHARS, remaining)]
        sources.append(
            Source(
                number=number,
                title=result.title or result.url,
                url=result.url,
                text=chunk,
            )
        )
        used_chars += len(chunk)
        number += 1
    return sources
