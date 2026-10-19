"""Выделение числового расчёта из готового ответа ассистента.

Задача: когда разбор содержит деньги и проценты (удержания по исполнительному
листу, расчёт задолженности), показать пользователю аккуратную таблицу вместо
того, чтобы он искал цифры в тексте.

Как это устроено и почему безопасно:
  1. Модель возвращает строго JSON с числами и подписями.
  2. Мы проверяем каждую строку: тип, длину, количество. Всё лишнее отбрасываем.
  3. Дальше таблицу рисует шаблон Django с автоэкранированием, поэтому даже
     если модель вернёт HTML-теги, они превратятся в обычный текст.

Никаких исключений наружу этот модуль не бросает: не получилось выделить
расчёт — пользователь получит просто текст ответа, как и раньше.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field

from .llm import ChatMessage, get_provider
from .prompts import CALC_EXTRACTION_PROMPT

logger = logging.getLogger(__name__)

# Признаки того, что в разборе есть смысл считать числа. Без них лишний вызов
# модели не делаем: он добавляет секунды к ответу.
FINANCIAL_MARKERS = (
    "₽",
    "руб",
    "копейк",
    "процент",
    "%",
    "удержан",
    "начислен",
    "взыскан",
    "лимит",
    "прожиточн",
    "ндфл",
)

# Минимальное число цифр в тексте: «2026 год» расчётом не считаем.
MIN_DIGITS = 6

# Границы схемы. Нужны, чтобы ответ модели не раздувал интерфейс.
MAX_TITLE_CHARS = 120
MAX_LABEL_CHARS = 90
MAX_VALUE_CHARS = 40
MAX_COMMENT_CHARS = 140
MAX_CONCLUSION_CHARS = 400
MAX_NORM_CHARS = 160
MAX_ROWS = 8
MAX_NORMS = 5


@dataclass
class CalcRow:
    """Одна строка таблицы: показатель, значение, пояснение."""

    label: str
    value: str
    comment: str = ""


@dataclass
class Calculation:
    """Расчёт, готовый к отрисовке таблицей."""

    title: str
    rows: list[CalcRow] = field(default_factory=list)
    conclusion: str = ""
    norms: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "title": self.title,
            "rows": [
                {"label": r.label, "value": r.value, "comment": r.comment}
                for r in self.rows
            ],
            "conclusion": self.conclusion,
            "norms": self.norms,
        }


def looks_financial(*texts: str) -> bool:
    """Стоит ли вообще пытаться выделять расчёт из этих текстов."""
    blob = " ".join(t or "" for t in texts).lower()
    if sum(c.isdigit() for c in blob) < MIN_DIGITS:
        return False
    return any(marker in blob for marker in FINANCIAL_MARKERS)


def _clean(value: object, limit: int) -> str:
    """Приводит значение из JSON к безопасной короткой строке.

    Числа и логические значения модель может вернуть без кавычек — это не
    ошибка, а вариант нормы: превращаем их в текст.
    """
    if isinstance(value, bool):
        return ""
    if isinstance(value, (int, float)):
        value = str(value)
    if not isinstance(value, str):
        return ""
    # Управляющие символы и лишние пробелы таблице ни к чему.
    text = re.sub(r"\s+", " ", value).strip()
    return text[:limit]


def _parse_payload(raw: str) -> dict | None:
    """Достаёт JSON из ответа модели.

    Модель может оборнуть ответ в ```json ... ``` или добавить пояснение —
    поэтому ищем первую и последнюю фигурную скобку, а не надеемся на чистоту.
    """
    text = (raw or "").strip()
    if not text:
        return None

    fenced = re.search(r"```(?:json)?\s*(.+?)```", text, re.DOTALL)
    if fenced:
        text = fenced.group(1).strip()

    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        return None

    try:
        payload = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        logger.info("Модель вернула некорректный JSON расчёта")
        return None
    return payload if isinstance(payload, dict) else None


def _build_calculation(payload: dict) -> Calculation | None:
    """Проверяет распарсенный JSON и собирает Calculation. None, если данных нет."""
    title = _clean(payload.get("title"), MAX_TITLE_CHARS) or "Расчёт"

    rows: list[CalcRow] = []
    raw_rows = payload.get("rows")
    if isinstance(raw_rows, list):
        for item in raw_rows[:MAX_ROWS]:
            if not isinstance(item, dict):
                continue
            label = _clean(item.get("label"), MAX_LABEL_CHARS)
            value = _clean(item.get("value"), MAX_VALUE_CHARS)
            # Строка без показателя или без числа бесполезна в таблице.
            if not label or not value:
                continue
            rows.append(
                CalcRow(
                    label=label,
                    value=value,
                    comment=_clean(item.get("comment"), MAX_COMMENT_CHARS),
                )
            )

    if not rows:
        return None

    norms: list[str] = []
    raw_norms = payload.get("norms")
    if isinstance(raw_norms, list):
        for item in raw_norms[:MAX_NORMS]:
            norm = _clean(item, MAX_NORM_CHARS)
            if norm:
                norms.append(norm)

    return Calculation(
        title=title,
        rows=rows,
        conclusion=_clean(payload.get("conclusion"), MAX_CONCLUSION_CHARS),
        norms=norms,
    )


def extract(
    question: str, answer: str, documents_text: str = ""
) -> Calculation | None:
    """Выделяет расчёт из ответа ассистента. Никогда не бросает исключений."""
    if not looks_financial(question, answer, documents_text):
        return None

    prompt = CALC_EXTRACTION_PROMPT.format(
        max_rows=MAX_ROWS,
        question=(question or "")[:600],
        answer=(answer or "")[:4000],
    )
    try:
        provider = get_provider()
        raw = provider.chat(
            [
                ChatMessage(role="system", content="Ты возвращаешь только JSON."),
                ChatMessage(role="user", content=prompt),
            ],
            temperature=0.0,
            max_tokens=700,
        )
    except Exception as exc:
        # Расчёт — украшение ответа: любая ошибка здесь не должна мешать
        # пользователю получить текстовый разбор.
        logger.info("Выделить расчёт не удалось: %s", exc)
        return None

    payload = _parse_payload(raw)
    if not payload:
        return None

    calculation = _build_calculation(payload)
    if calculation:
        logger.info("Выделен расчёт: %d строк", len(calculation.rows))
    return calculation
