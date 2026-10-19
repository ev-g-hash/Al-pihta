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

import codecs
import io
import logging
import os
import shutil
from dataclasses import dataclass
from pathlib import Path

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
SCANNED_PDF_NOTICE = (
    "(в PDF нет текстового слоя — это скан или фото; распознавание недоступно: "
    "OCR-движок Tesseract не установлен)"
)
SCANNED_PDF_NO_OCR_NOTICE = (
    "(в PDF нет текстового слоя, и распознавание по картинке не дало результата — "
    "страницы могут быть пустыми или слишком низкого качества)"
)
# Скан-страниц распознаём за один раз: OCR на процессоре медленный.
MAX_OCR_PDF_PAGES = 6
# Если текстового слоя почти нет, считаем PDF скан-копией.
MIN_SCANNED_PDF_CHARS = 40


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


# Возможные места установки Tesseract на Windows: ищем сами, потому что
# установщик не всегда прописывает каталог в PATH. Пути собираем из
# переменных окружения — на разных машинах каталог программ может отличаться.
TESSERACT_SUBPATH = ("Tesseract-OCR", "tesseract.exe")


def _candidate_paths() -> list[str]:
    roots = [
        os.environ.get("ProgramFiles", ""),
        os.environ.get("ProgramFiles(x86)", ""),
        os.environ.get("ProgramW6432", ""),
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs"),
    ]
    return [
        os.path.join(root, *TESSERACT_SUBPATH)
        for root in roots
        if root
    ]


# Языковые данные могут лежать в самом проекте (тогда не нужны права
# администратора на запись в Program Files).
PROJECT_TESSDATA = Path(__file__).resolve().parents[2] / "tools" / "tessdata"


def tesseract_cmd() -> str | None:
    """Возвращает путь к tesseract.exe или None, если движок не найден."""
    # Переменная окружения даёт возможность указать путь вручную.
    configured = os.getenv("TESSERACT_CMD", "").strip()
    if configured and os.path.exists(configured):
        return configured

    found = shutil.which("tesseract")
    if found:
        return found

    for candidate in _candidate_paths():
        if os.path.exists(candidate):
            return candidate
    return None


def tessdata_dir() -> str | None:
    """Каталог с языковыми данными проекта, если в нём есть русский пакет."""
    if (PROJECT_TESSDATA / "rus.traineddata").exists():
        return str(PROJECT_TESSDATA)
    return None


def ocr_available() -> bool:
    """Проверяет, доступен ли Tesseract (нужен только для распознавания картинок)."""
    return tesseract_cmd() is not None


class MissingDependency(Exception):
    """Не установлена библиотека, нужная для чтения конкретного формата.

    Отдельный тип нужен, чтобы не сообщать пользователю про «повреждённый
    файл», когда файл на самом деле целый, а программе просто чем его читать.
    """


def _ocr_pdf_images(reader) -> str:
    """Распознаёт скан-копию: страницы PDF как картинки через Tesseract.

    Страниц может быть много, а OCR на процессоре не быстрый, поэтому
    распознаём не более MAX_OCR_PDF_PAGES первых страниц.
    """
    pages_text = []
    for page in list(reader.pages)[:MAX_OCR_PDF_PAGES]:
        try:
            images = list(page.images)
        except Exception as exc:
            logger.info("Не удалось достать картинки страницы PDF: %s", exc)
            continue
        for image in images:
            text = _read_image(image.data)
            if text and not text.startswith("("):
                pages_text.append(text)
    return "\n".join(pages_text).strip()


def _read_pdf(data: bytes) -> str:
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise MissingDependency(
            "Не установлена библиотека pypdf. Установите зависимости: "
            "pip install -r requirements.txt"
        ) from exc

    reader = PdfReader(io.BytesIO(data))
    pages = []
    for page in reader.pages:
        try:
            pages.append(page.extract_text() or "")
        except Exception as exc:  # повреждённая страница не должна рвать весь файл
            logger.info("Не удалось разобрать страницу PDF: %s", exc)
            pages.append("")
    text = "\n".join(pages).strip()
    # Скан-копии и фото документов, сохранённые в PDF, не содержат текстового
    # слоя: pypdf возвращает пустоту. Если есть Tesseract — распознаём
    # страницы как картинки, иначе объясняем пользователю причину явно.
    if len(text) < MIN_SCANNED_PDF_CHARS:
        if ocr_available():
            return _ocr_pdf_images(reader) or SCANNED_PDF_NO_OCR_NOTICE
        return SCANNED_PDF_NOTICE
    return text


def _read_docx(data: bytes) -> str:
    try:
        import docx
    except ImportError as exc:
        raise MissingDependency(
            "Не установлена библиотека python-docx. Установите зависимости: "
            "pip install -r requirements.txt"
        ) from exc

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
    # Порядок важен: BOM определяем явно, иначе UTF-16 декодируется в «кашу» —
    # utf-8 и cp1251 принимают почти любые байты, поэтому их проверка не спасает.
    if data.startswith(codecs.BOM_UTF8):
        return data.decode("utf-8-sig")
    if data.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return data.decode("utf-16")
    # UTF-16 без BOM: у латиницы и кириллицы в этом коде каждый второй байт —
    # ноль. Такого в однобайтовых кодировках не бывает, так что признак надёжный.
    if len(data) >= 16:
        sample = data[: len(data) - len(data) % 2]
        zeros = sample.count(b"\x00")
        if zeros and zeros >= len(sample) * 0.25 and zeros % 2 == 0:
            encoding = "utf-16-le" if sample[1::2].count(b"\x00") > sample[::2].count(b"\x00") else "utf-16-be"
            try:
                return sample.decode(encoding)
            except UnicodeDecodeError:
                pass
    # Перебор кодировок: документы из бухгалтерий и госорганов часто в cp1251.
    for encoding in ("utf-8", "cp1251", "cp866"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="ignore")


def _read_image(data: bytes) -> str:
    cmd = tesseract_cmd()
    if not cmd:
        return OCR_UNAVAILABLE_NOTICE
    try:
        import pytesseract
        from PIL import Image
    except ImportError as exc:
        raise MissingDependency(
            "Не установлены библиотеки pytesseract/Pillow. Установите зависимости: "
            "pip install -r requirements.txt"
        ) from exc

    try:
        pytesseract.pytesseract.tesseract_cmd = cmd
        tessdata = tessdata_dir()

        # Языковые данные проекта передаём через переменную окружения: вариант
        # с ключом --tessdata-dir не подходит, потому что pytesseract 0.3.x
        # оставляет кавычки внутри пути, а в пути есть пробелы.
        previous_prefix = os.environ.get("TESSDATA_PREFIX")
        if tessdata:
            os.environ["TESSDATA_PREFIX"] = tessdata
        try:
            image = Image.open(io.BytesIO(data))
            # Русское+английское распознавание; если языковой пакет rus не установлен,
            # Tesseract падает — тогда пробуем только английский.
            for languages in ("rus+eng", "eng"):
                try:
                    return pytesseract.image_to_string(image, lang=languages)
                except pytesseract.TesseractError:
                    continue
        finally:
            if previous_prefix is None:
                os.environ.pop("TESSDATA_PREFIX", None)
            else:
                os.environ["TESSDATA_PREFIX"] = previous_prefix
        return EMPTY_TEXT_NOTICE
    except MissingDependency as exc:
        return str(exc)
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
    except MissingDependency as exc:
        # Не «файл плохой», а нечем читать: сообщаем причину прямо.
        logger.info("Не хватает зависимости для файла %s: %s", name, exc)
        return f"({exc})"
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