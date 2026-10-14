"""Слой доступа к LLM-провайдерам.

Сейчас поддерживается GigaChat (основной) и YandexGPT (задел на будущее).
Провайдер выбирается переменной окружения LLM_PROVIDER в файле .env,
поэтому остальной код приложения не зависит от конкретного сервиса.
"""

from __future__ import annotations

import os
import time
import uuid
import base64
from dataclasses import dataclass

import requests
import urllib3

# GigaChat использует самоподписанные сертификаты (корневой сертификат Минцифры),
# поэтому проверку TLS для его запросов отключаем. Это допустимо для учебного
# проекта; в продакшене следует подключить официальный корневой сертификат.
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


class LLMError(Exception):
    """Ошибка при обращении к LLM-провайдеру."""


@dataclass
class ChatMessage:
    """Одно сообщение в диалоге."""

    role: str  # 'system' | 'user' | 'assistant'
    content: str


class BaseProvider:
    """Базовый интерфейс провайдера. Все провайдеры приводятся к нему."""

    name = "base"

    def chat(
        self,
        messages: list[ChatMessage],
        temperature: float = 0.3,
        max_tokens: int | None = None,
    ) -> str:
        raise NotImplementedError


class GigaChatProvider(BaseProvider):
    """Провайдер GigaChat (Сбер).

    Авторизация: OAuth 2.0, токен живёт 30 минут и переиспользуется.
    """

    name = "gigachat"

    OAUTH_URL = "https://ngw.devices.sberbank.ru:9443/api/v2/oauth"
    API_URL = "https://api.giga.chat/v1/chat/completions"

    def __init__(self) -> None:
        self.scope = os.getenv("GIGACHAT_SCOPE", "GIGACHAT_API_PERS").strip()
        self.model = os.getenv("GIGACHAT_MODEL", "GigaChat").strip()
        self._token: str | None = None
        self._token_expires_at: float = 0.0

        self.auth_key = self._resolve_auth_key()

    @staticmethod
    def _resolve_auth_key() -> str:
        """Возвращает ключ авторизации для заголовка Basic.

        Поддерживаются два способа (в порядке приоритета):
        1. GIGACHAT_AUTH_KEY — готовый ключ авторизации из личного кабинета.
        2. GIGACHAT_CLIENT_ID + GIGACHAT_CLIENT_SECRET — из них ключ
           собирается автоматически как Base64("client_id:client_secret").
        """
        auth_key = os.getenv("GIGACHAT_AUTH_KEY", "").strip()
        if auth_key:
            return auth_key

        client_id = os.getenv("GIGACHAT_CLIENT_ID", "").strip()
        client_secret = os.getenv("GIGACHAT_CLIENT_SECRET", "").strip()
        if client_id and client_secret:
            raw = f"{client_id}:{client_secret}".encode("utf-8")
            return base64.b64encode(raw).decode("ascii")

        raise LLMError(
            "Не заданы данные авторизации GigaChat. Укажите в файле .env либо "
            "GIGACHAT_AUTH_KEY (готовый ключ), либо пару "
            "GIGACHAT_CLIENT_ID и GIGACHAT_CLIENT_SECRET."
        )

    def _get_token(self) -> str:
        """Возвращает действующий токен доступа, при необходимости обновляет его."""
        # Обновляем токен заранее (за 60 секунд до истечения).
        if self._token and time.time() < self._token_expires_at - 60:
            return self._token

        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
            "RqUID": str(uuid.uuid4()),
            "Authorization": f"Basic {self.auth_key}",
        }
        try:
            response = requests.post(
                self.OAUTH_URL,
                headers=headers,
                data={"scope": self.scope},
                timeout=30,
                verify=False,
            )
        except requests.RequestException as exc:
            raise LLMError(f"Не удалось получить токен GigaChat: {exc}") from exc

        if response.status_code != 200:
            raise LLMError(
                f"GigaChat вернул ошибку авторизации {response.status_code}: "
                f"{response.text[:300]}"
            )

        payload = response.json()
        self._token = payload["access_token"]
        # expires_at — unix-время в миллисекундах.
        self._token_expires_at = payload.get("expires_at", 0) / 1000
        return self._token

    def chat(
        self,
        messages: list[ChatMessage],
        temperature: float = 0.3,
        max_tokens: int | None = None,
    ) -> str:
        token = self._get_token()
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        body = {
            "model": self.model,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "temperature": temperature,
        }
        # Ограничение длины ответа ускоряет работу коротких служебных вызовов.
        if max_tokens:
            body["max_tokens"] = max_tokens
        try:
            response = requests.post(
                self.API_URL, headers=headers, json=body, timeout=120, verify=False
            )
        except requests.RequestException as exc:
            raise LLMError(f"Ошибка запроса к GigaChat: {exc}") from exc

        if response.status_code != 200:
            raise LLMError(
                f"GigaChat вернул ошибку {response.status_code}: {response.text[:300]}"
            )

        data = response.json()
        try:
            return data["choices"][0]["message"]["content"]
        except (KeyError, IndexError) as exc:
            raise LLMError(f"Неожиданный формат ответа GigaChat: {data}") from exc


class YandexGPTProvider(BaseProvider):
    """Провайдер YandexGPT (задел на будущее, пока не протестирован)."""

    name = "yandex"

    API_URL = "https://llm.api.cloud.yandex.net/foundationModels/v1/completion"

    def __init__(self) -> None:
        self.api_key = os.getenv("YANDEX_API_KEY", "").strip()
        self.folder_id = os.getenv("YANDEX_FOLDER_ID", "").strip()
        self.model = os.getenv("YANDEX_MODEL", "yandexgpt-lite").strip()

        if not self.api_key or not self.folder_id:
            raise LLMError(
                "Не заданы YANDEX_API_KEY и/или YANDEX_FOLDER_ID. "
                "Впишите их в файл .env."
            )

    def chat(
        self,
        messages: list[ChatMessage],
        temperature: float = 0.3,
        max_tokens: int | None = None,
    ) -> str:
        headers = {
            "Authorization": f"Api-Key {self.api_key}",
            "Content-Type": "application/json",
        }
        completion_options = {"stream": False, "temperature": temperature}
        if max_tokens:
            completion_options["maxTokens"] = max_tokens
        body = {
            "modelUri": f"gpt://{self.folder_id}/{self.model}",
            "completionOptions": completion_options,
            # У YandexGPT поле называется text, а не content.
            "messages": [{"role": m.role, "text": m.content} for m in messages],
        }
        try:
            response = requests.post(
                self.API_URL, headers=headers, json=body, timeout=120
            )
        except requests.RequestException as exc:
            raise LLMError(f"Ошибка запроса к YandexGPT: {exc}") from exc

        if response.status_code != 200:
            raise LLMError(
                f"YandexGPT вернул ошибку {response.status_code}: {response.text[:300]}"
            )

        data = response.json()
        try:
            return data["result"]["alternatives"][0]["message"]["text"]
        except (KeyError, IndexError) as exc:
            raise LLMError(f"Неожиданный формат ответа YandexGPT: {data}") from exc


_PROVIDERS = {
    "gigachat": GigaChatProvider,
    "yandex": YandexGPTProvider,
}


def get_provider() -> BaseProvider:
    """Создаёт провайдера согласно переменной окружения LLM_PROVIDER."""
    name = os.getenv("LLM_PROVIDER", "gigachat").strip().lower()
    provider_cls = _PROVIDERS.get(name)
    if provider_cls is None:
        raise LLMError(
            f"Неизвестный провайдер '{name}'. Допустимые значения: "
            f"{', '.join(_PROVIDERS)}."
        )
    return provider_cls()
