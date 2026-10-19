"""Конвейер формирования ответа: поиск → источники → модель.

Связывает веб-поиск (search.py), загрузку страниц (crawler.py) и обращение к
LLM (llm.py). Логика поиска живёт в коде, а не отдаётся модели: это делает
поведение предсказуемым и защищает от «фантазий» про несуществующие нормы.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from . import crawler, query_expander, search
from .crawler import Source
from .file_reader import UserFile
from .llm import ChatMessage, get_provider
from .prompts import DOCUMENTS_INSTRUCTION, SOURCES_INSTRUCTION, SYSTEM_PROMPT

# Сколько последних сообщений отправлять модели.
MAX_HISTORY_MESSAGES = 12
# Максимальная длина поискового запроса (берём последний вопрос пользователя).
MAX_QUERY_CHARS = 400

# GigaChat иногда вставляет LaTeX-оформление: числа в долларах и команды с
# обратным слэшем. Запрет в промпте помогает не всегда, поэтому чиним в коде:
# интерфейс показывает обычный текст, формулы в нём выглядят мусором.
# Обратный слэш берём из константы: литералы с ним неудобны и легко портятся.
BACKSLASH = "\u005c"
BS = BACKSLASH * 2  # в регулярке два слэша матчат один настоящий слэш
_MATH_SPAN_RE = re.compile(r"\$([^$]{1,120})\$")
_PAREN_MATH_RE = re.compile(BS + "[(](.+?)" + BS + "[)]")
_MATH_COMMAND_RE = re.compile(BS + "[,;:!]|" + BS + "[a-zA-Z]+|[{}]")


# Ссылки вида [1], [4], [Д2] на найденные материалы. Пользователь их список не
# видит, поэтому в тексте ответа они бессмысленны. Запрет в промпте помогает не
# всегда, поэтому маркеры убираем в коде.
_CITATION_WITH_PUNCT_RE = re.compile(r"\s*[:;,]\s*\[(?:\d+|[ДD]\d+)\]")
_CITATION_RE = re.compile(r"\s*\[(?:\d+|[ДD]\d+)\]")


def clean_answer_markup(text: str) -> str:
    """Приводит ответ модели к виду обычной переписки.

    Убирает LaTeX-вкрапления и сноски на источники: интерфейс показывает
    простой текст, и то и другое выглядит в нём мусором.
    """
    if not text:
        return text

    if "$" in text or BACKSLASH in text:
        def _plain(span: re.Match) -> str:
            inner = _MATH_COMMAND_RE.sub(" ", span.group(1))
            return " ".join(inner.split())

        text = _PAREN_MATH_RE.sub(_plain, text)
        text = _MATH_SPAN_RE.sub(_plain, text)
        text = re.sub(BS + "[,;]", " ", text)

    # «найти здесь: [4].» → «найти здесь.», «согласно [1] норме» → «согласно норме»
    text = _CITATION_WITH_PUNCT_RE.sub("", text)
    text = _CITATION_RE.sub("", text)

    # После вычёркивания ссылки могла остаться сдвоенная точка («См..»);
    # многоточие не трогаем.
    text = re.sub(r"(?<!\.)\.\.(?!\.)", ".", text)
    return re.sub(" {2,}", " ", text)


@dataclass
class AssistantAnswer:
    """Результат работы конвейера."""

    text: str
    sources: list[Source]


def _build_search_query(history: list[dict]) -> str:
    """Формирует поисковый запрос из последнего сообщения пользователя."""
    for item in reversed(history):
        if item.get("role") == "user":
            return item.get("content", "").strip()[:MAX_QUERY_CHARS]
    return ""


def _format_sources(sources: list[Source]) -> str:
    """Формирует текстовый блок источников для передачи в модель."""
    blocks = []
    for src in sources:
        blocks.append(
            f"[{src.number}] {src.title}\nURL: {src.url}\n{src.text}"
        )
    return "\n\n".join(blocks)


def _format_documents(documents: list[UserFile]) -> str:
    """Формирует блок приложенных пользователем документов ([Д1], [Д2], ...)."""
    return "\n\n".join(
        f"[Д{index}] {doc.label}\n{doc.text}"
        for index, doc in enumerate(documents, start=1)
    )


def _documents_from_history(history: list[dict]) -> list[UserFile]:
    """Восстанавливает приложенные документы из истории диалога.

    Нужен именно этот шаг, потому что текст файла модель видит только в том
    сообщении, к которому файл приложен. Без восстановления уточняющий вопрос
    «а что в пункте 5 этого договора?» оставался бы без самого договора.

    Берём последний набор вложений: более старые документы в разборе уже
    не участвуют, иначе контекст разрастался бы до бесконечности.
    """
    for item in reversed(history):
        if item.get("role") != "user":
            continue
        stored = item.get("files") or []
        documents = [
            UserFile(
                name=entry.get("name", ""),
                kind=entry.get("kind", "text"),
                text=entry.get("text", ""),
            )
            for entry in stored
            if entry.get("text")
        ]
        if documents:
            return documents
        # Сообщения пользователя без вложений пропускаем, но как только
        # встретили сообщение с вложениями без текста — дальше не идём.
        if stored:
            return []
    return []


def _build_messages(
    history: list[dict],
    sources: list[Source],
    documents: list[UserFile] | None = None,
) -> list[ChatMessage]:
    """Собирает список сообщений для модели.

    GigaChat требует, чтобы системное сообщение было единственным и шло первым,
    поэтому найденные источники и приложенные документы приклеиваем к основному
    промпту, а не добавляем отдельными system-сообщениями.
    """
    system_content = SYSTEM_PROMPT
    if documents:
        system_content += "\n\n" + DOCUMENTS_INSTRUCTION.format(
            documents=_format_documents(documents)
        )
    if sources:
        system_content += "\n\n" + SOURCES_INSTRUCTION.format(
            sources=_format_sources(sources)
        )
    messages = [ChatMessage(role="system", content=system_content)]
    for item in history[-MAX_HISTORY_MESSAGES:]:
        messages.append(ChatMessage(role=item["role"], content=item["content"]))
    return messages


def answer(
    history: list[dict],
    documents: list[UserFile] | None = None,
) -> AssistantAnswer:
    """Готовит ответ ассистента по истории диалога.

    :param history: список сообщений [{'role': ..., 'content': ...}, ...],
                    последнее из которых — текущий вопрос пользователя.
    :param documents: извлечённые тексты приложенных пользователем файлов.
    """
    # Один вопрос превращаем в несколько поисковых формулировок и ищем
    # по всем — так покрываются и норма закона, и судебная практика.
    # Если текста вопроса нет (приложены только документы), ищем по началу
    # первого документа, чтобы к разбору добавились актуальные нормы.
    sources: list[Source] = []
    question = _build_search_query(history)
    if not question and documents:
        question = documents[0].text[:MAX_QUERY_CHARS]
    if question:
        queries = query_expander.expand_query(question)
        results = search.search_multi(queries, original_question=question)
        sources = crawler.collect_sources(results)

    # Если в этом сообщении файлов нет, берём последний приложенный ранее:
    # иначе модель «забывала» документ между сообщениями.
    documents = documents or _documents_from_history(history)

    provider = get_provider()
    text = clean_answer_markup(provider.chat(_build_messages(history, sources, documents)))
    return AssistantAnswer(text=text, sources=sources)

