"""Извлечение текста из файлов, загруженных пользователем.

Поддерживаются PDF, DOCX, текстовые файлы и изображения. Для изображений
используется OCR Tesseract: он не является жёсткой зависимостью, поэтому при
его отсутствии картинка принимается, но вместо текста возвращается пометка об
этом — диалог при этом не ломается.

Все функции возвращают строку (извлечённый текст) и никогда не бросают
исключение наружу: на любой сбой возвращается пустая строка либо служебное
сообщение, которое увидит модель.
"""

from __future__ import annotations

import io
import logging
import shutil
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# Ограничения на стороне загрузки. Текст файла попадает в контекст модели на
# равных с веб-источниками, поэтому он тоже ограничен бюджетом символов.
MAX_FILE_BYTES = 10 * 1024 * 1024  # 10 МБ на файл
MAX_FILES_PER_MESSAGE = 4
MAX_FILE_CHARS = 6000
TOTAL_FILE_CHAR_BUDGET = 12000

# Extensions are checked by name, not by MIME type: browsers routinely send
# application/octet-stream for DOCX and TXT files.
EXTENSIONS_BY_KIND = {
    "pdf": {"pdf"},
    "docx": {"docx"},
    "text": {"txt", "md", "rtf", "csv", "log", "json", "html", "htm", "xml"},
    "image": {"png", "jpg", "jpeg", "gif", "bmp", "webp", "tiff"},
}

# Понятные человеку названия типов для сообщений об ошибках.
KIND_LABELS = {
    "pdf": "PDF",
    "docx": "DOCX",
    "text": "текстовый файл",
    "image": "изображение",
}

# Пустые вложенные структуры: файл загружен, но текста в нём нет.
EMPTY_TEXT_NOTICE = "(в файле не обнаружено распознаваемого текста)"
OCR_UNAVAILABLE_NOTICE = (
    "(текст с изображения не распознан: OCR-движок Tesseract не установлен)"
)


@dataclass
class UserFile:
    """Извлечённый из загруженного файла текст."""

    name: str
    kind: str
    text: str

    @property
    def label(self) -> str:
        return f"{KIND_LABELS.get(self.kind, self.kind)} «{self.name}»"


def detect_kind(filename: str) -> str | None:
    """Возвращает род файла по расширению ('pdf'|'docx'|'text'|'image') или None."""
    extension = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    for kind, extensions in EXTENSIONS_BY_KIND.items():
        if extension in extensions:
            return kind
    return None


def ocr_available() -> bool:
    """Проверяет, доступен ли Tesseract (нужен только для распознавания картинок)."""
    if shutil.which("tesseract"):
        return True
    try:
        import os

        import pytesseract

        return bool(pytesseract.tesseract_cmd) and os.path.exists(
            pytesseract.tesseract_cmd
        )
    except Exception:
        return False


def _read_pdf(data: bytes) -> str:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    pages = []
    for page in reader.pages:
        try:
            pages.append(page.extract_text() or "")
        except Exception as exc:  # повреждённая страница не должна рвать весь файл
            logger.info("Не удалось разобрать страницу PDF: %s", exc)
            pages.append("")
    return "\n".join(pages)


def _read_docx(data: bytes) -> str:
    import docx

    document = docx.Document(io.BytesIO(data))
    parts = [paragraph.text for paragraph in document.paragraphs]
    # Таблицы в юридических документах (расчётные листки, приложения) теряются
    # без явной обработки, поэтому читаем и их.
    for table in document.tables:
        for row in table.rows:
            cells = [cell.text.strip() for cell in row.cells]
            if any(cells):
                parts.append(" | ".join(cells))
    return "\n".join(parts)


def _read_text(data: bytes) -> str:
    # Перебор кодировок: документы из бухгалтерий и госорганов часто в cp1251.
    for encoding in ("utf-8-sig", "utf-8", "cp1251", "cp866"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="ignore")


def _read_image(data: bytes) -> str:
    if not ocr_available():
        return OCR_UNAVAILABLE_NOTICE
    try:
        import pytesseract
        from PIL import Image

        image = Image.open(io.BytesIO(data))
        # Русское+английское распознавание; если языковой пакет rus не установлен,
        # Tesseract падает — тогда пробуем только английский.
        for languages in ("rus+eng", "eng"):
            try:
                return pytesseract.image_to_string(image, lang=languages)
            except pytesseract.TesseractError:
                continue
        return EMPTY_TEXT_NOTICE
    except Exception as exc:
        logger.info("Не удалось распознать изображение: %s", exc)
        return EMPTY_TEXT_NOTICE


READERS = {
    "pdf": _read_pdf,
    "docx": _read_docx,
    "text": _read_text,
    "image": _read_image,
}


def extract_text(upload, name: str, kind: str) -> str:
    """Извлекает текст из объекта UploadedFile. Не бросает исключений наружу."""
    try:
        upload.seek(0)
        data = upload.read(MAX_FILE_BYTES + 1)
        if len(data) > MAX_FILE_BYTES:
            return "(файл слишком большой, содержимое не прочитано)"
        text = READERS[kind](data)
    except Exception as exc:
        logger.info("Не удалось прочитать файл %s: %s", name, exc)
        return "(файл повреждён или имеет нестандартный формат, текст не извлечён)"
    return (text or "").strip()


def read_uploads(uploads) -> tuple[list[UserFile], list[str]]:
    """Читает загруженные файлы.

    :return: (список файлов с текстом, список сообщений об ошибках)
    """
    files: list[UserFile] = []
    errors: list[str] = []
    used_chars = 0

    if len(uploads) > MAX_FILES_PER_MESSAGE:
        errors.append(
            f"Можно прикрепить не более {MAX_FILES_PER_MESSAGE} файлов, "
            f"лишние пропущены."
        )

    for upload in uploads[:MAX_FILES_PER_MESSAGE]:
        name = getattr(upload, "name", "") or "без имени"
        kind = detect_kind(name)
        if kind is None:
            errors.append(f"Файл «{name}»: неподдерживаемый формат.")
            continue

        text = extract_text(upload, name, kind)
        if not text:
            errors.append(f"Файл «{name}»: не удалось извлечь текст.")
            continue

        remaining = TOTAL_FILE_CHAR_BUDGET - used_chars
        if remaining <= 300:
            errors.append(
                "Суммарный объём прикреплённых документов слишком велик, "
                "остальные файлы не прочитаны."
            )
            break

        files.append(
            UserFile(name=name, kind=kind, text=text[: min(MAX_FILE_CHARS, remaining)])
        )
        used_chars += min(MAX_FILE_CHARS, remaining)

    return files, errors