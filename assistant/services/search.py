"""Веб-поиск правовых источников.

Использует бесплатную библиотеку ddgs (DuckDuckGo) — без API-ключей.
Результаты фильтруются по белому списку доверенных правовых сайтов,
чтобы ответы опирались на надёжные источники, а не на случайные страницы.
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


# Белый список доверенных правовых источников (домены РФ).
# Дочерние поддомены попадают автоматически: для "duma.gov.ru" подойдёт и
# "sozd.duma.gov.ru".
TRUSTED_DOMAINS = [
    # 1. Официальные государственные правовые порталы (первоисточники).
    "pravo.gov.ru",           # официальный портал правовой информации
    "publication.pravo.gov.ru",
    "duma.gov.ru",            # Государственная Дума (включая СОЗД)
    "council.gov.ru",         # Совет Федерации
    "government.ru",          # Правительство РФ
    "kremlin.ru",             # указы и распоряжения Президента

    # 2. Официальные сайты высших судов.
    "vsrf.ru",                # Верховный Суд РФ
    "ksrf.ru",                # Конституционный Суд РФ

    # 3. Электронное правосудие и базы судебных дел.
    "kad.arbitr.ru",          # картотека арбитражных дел
    "sudrf.ru",               # ГАС «Правосудие»
    "mos-gorsud.ru",          # суды общей юрисдикции Москвы

    # 4. Агрегаторы судебной практики и нормативных актов.
    "sudact.ru",              # СудАкт
    "consultant.ru",          # КонсультантПлюс
    "garant.ru",              # Гарант
    "zakon.ru",               # аналитика и разборы кейсов
    "legalacts.ru",           # правовые акты
    "kodeks.ru",

    # 5. Органы, применяющие право (трудовые споры, исполнительное производство).
    "fssp.gov.ru",            # ФССП — порядок исполнения листов и удержаний
    "mintrud.gov.ru",         # Минтруд — разъяснения по трудовому праву
    # Онлайнинспекция Роструда: готовые ответы по зарплате и увольнениям.
    # Домен кириллический, поэтому указываем обе записи — поисковик или браузер
    # могут вернуть его и в юникоде, и в punycode.
    "онлайнинспекция.рф",
    "xn--80akibcicpdbetz7e2g.xn--p1ai",
]

# Сколько результатов запрашивать у поисковика на один запрос.
MAX_RESULTS_PER_QUERY = 6
# Сколько итоговых источников отдаём на загрузку. Берём с запасом: часть
# сайтов не отдаст текст (страницы на JavaScript), и они будут отброшены
# на этапе загрузки.
MAX_SOURCES = 8
# Вес доверенного домена при ранжировании.
TRUSTED_BONUS = 10.0

# Поисковые движки, которые используем. По умолчанию только duckduckgo:
# по замерам именно он стабильно отвечает из РФ за ~2 секунды, а остальные
# (yahoo, startpage, brave) висят на таймаутах и только замедляют поиск.
# Список можно переопределить в .env через SEARCH_BACKENDS.
DEFAULT_BACKENDS = "duckduckgo"


def _backends() -> str:
    return os.getenv("SEARCH_BACKENDS", DEFAULT_BACKENDS).strip() or DEFAULT_BACKENDS

# Стоп-слова не участвуют в оценке релевантности.
_RELEVANCE_STOP_WORDS = {
    "что", "как", "где", "когда", "почему", "зачем", "какой", "какая", "какие",
    "какое", "мне", "меня", "мой", "моя", "это", "этот", "эта", "если", "чтобы",
    "хотя", "был", "была", "было", "были", "есть", "буду", "будет", "хочу",
    "скажите", "подскажите", "пожалуйста", "прошу", "помогите",
}


def _domain_of(url: str) -> str:
    """Извлекает домен из URL без схемы и www."""
    try:
        from urllib.parse import urlparse

        host = urlparse(url).netloc.lower()
        return host[4:] if host.startswith("www.") else host
    except Exception:
        return ""


def is_trusted(url: str) -> bool:
    """Проверяет, относится ли URL к доверенному правовому источнику."""
    domain = _domain_of(url)
    return any(domain == d or domain.endswith("." + d) for d in TRUSTED_DOMAINS)


def _normalize_url(url: str) -> str:
    """Приводит URL к виду, удобному для сравнения (для дедупликации)."""
    from urllib.parse import urlparse, urlunparse

    try:
        parsed = urlparse(url.strip())
        path = parsed.path.rstrip("/")
        # Служебные параметры поиска в расчёт не берём.
        return urlunparse((parsed.scheme, parsed.netloc.lower(), path, "", "", ""))
    except Exception:
        return url.strip()


def _keywords(text: str) -> list[str]:
    """Извлекает содержательные слова для оценки релевантности."""
    words = re.findall(r"[а-яёa-z0-9]+", (text or "").lower())
    return [w for w in words if len(w) > 3 and w not in _RELEVANCE_STOP_WORDS]


def _relevance(result: SearchResult, position: int, keywords: list[str]) -> float:
    """Оценивает полезность результата.

    Учитывается: доверенный домен, вхождение ключевых слов вопроса в заголовок
    и описание, а также позиция в выдаче поисковика.
    """
    score = TRUSTED_BONUS if is_trusted(result.url) else 0.0

    haystack = f"{result.title} {result.snippet}".lower()
    hits = sum(1 for word in keywords if word in haystack)
    score += min(hits, 6) * 1.5

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

