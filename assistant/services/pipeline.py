"""Конвейер формирования ответа: поиск → источники → модель.

Связывает веб-поиск (search.py), загрузку страниц (crawler.py) и обращение к
LLM (llm.py). Логика поиска живёт в коде, а не отдаётся модели: это делает
поведение предсказуемым и защищает от «фантазий» про несуществующие нормы.
"""

from __future__ import annotations

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

    provider = get_provider()
    text = provider.chat(_build_messages(history, sources, documents))
    return AssistantAnswer(text=text, sources=sources)

