/**
 * AJAX-чат без перезагрузки страницы.
 *
 * При отправке вопроса:
 *  1. Блокируем поле ввода и показываем индикатор «думает…».
 *  2. Отправляем fetch() на /api/chat/.
 *  3. Получаем JSON с ответом → вставляем в ленту.
 *  4. Плавно прокручиваем ленту вниз.
 */
(function () {
    'use strict';

    const chat = document.getElementById('chat');
    const emptyState = document.getElementById('empty-state');
    const composer = document.getElementById('composer');
    const input = document.getElementById('input');
    const sendBtn = document.getElementById('send-btn');
    const clearBtn = document.getElementById('clear-btn');
    const scrollDownBtn = document.getElementById('scroll-down');

    let isLoading = false;

    /* ───────── Вспомогательные функции ───────── */

    function escapeHtml(text) {
        var div = document.createElement('div');
        div.appendChild(document.createTextNode(text));
        return div.innerHTML;
    }

    /* ───────── Отрисовка сообщений ───────── */

    /** Создаёт DOM-элемент сообщения (пользователь или ассистент). */
    function createMessage(role, content, sources) {
        var wrapper = document.createElement('div');
        wrapper.className = 'message message--' + role;

        var bubble = document.createElement('div');
        bubble.className = 'message__bubble';
        // textContent + white-space: pre-wrap в CSS: переносы строк из ответа
        // модели сохраняются, а случайный HTML не исполняется.
        bubble.textContent = content;

        wrapper.appendChild(bubble);

        // Источники — только для ассистента
        if (role === 'assistant' && sources && sources.length > 0) {
            var sourcesDiv = document.createElement('div');
            sourcesDiv.className = 'sources';

            var title = document.createElement('div');
            title.className = 'sources__title';
            title.textContent = 'Источники:';
            sourcesDiv.appendChild(title);

            var list = document.createElement('ul');
            list.className = 'sources__list';

            sources.forEach(function (src) {
                var li = document.createElement('li');
                li.className = 'sources__item';

                var num = document.createElement('span');
                num.className = 'sources__num';
                num.textContent = '[' + src.number + '] ';

                var a = document.createElement('a');
                a.href = src.url;
                a.target = '_blank';
                a.rel = 'noopener noreferrer';
                a.textContent = src.title;

                li.appendChild(num);
                li.appendChild(a);
                list.appendChild(li);
            });

            sourcesDiv.appendChild(list);
            wrapper.appendChild(sourcesDiv);
        }

        return wrapper;
    }

    /** Индикатор набора текста (три пульсирующие точки). */
    function createTypingIndicator() {
        var wrapper = document.createElement('div');
        wrapper.className = 'message message--assistant typing-indicator';
        var bubble = document.createElement('div');
        bubble.className = 'message__bubble typing';
        bubble.innerHTML = '<span></span><span></span><span></span>';
        wrapper.appendChild(bubble);
        return wrapper;
    }

    /** Индикатор ошибки. */
    function createError(text) {
        var div = document.createElement('div');
        div.className = 'alert';
        div.innerHTML = '<strong>Не удалось получить ответ.</strong><span>' +
            escapeHtml(text) + '</span>';
        return div;
    }

    /* ───────── Прокрутка ───────── */

    /** Мгновенно прокручивает ленту вниз (без анимации). */
    function scrollToBottom() {
        if (chat) {
            chat.scrollTop = chat.scrollHeight;
        }
    }

    /** Плавно прокручивает ленту вниз. */
    function smoothScrollToBottom() {
        if (chat) {
            chat.scrollTo({ top: chat.scrollHeight, behavior: 'smooth' });
        }
    }

    /** Показывает/скрывает кнопку «к концу диалога». */
    function updateScrollDownButton() {
        if (!chat) return;
        var diff = chat.scrollHeight - chat.scrollTop - chat.clientHeight;
        // Если прокрутили выше ~80 px от низа — показываем кнопку
        if (diff > 80) {
            scrollDownBtn.removeAttribute('hidden');
        } else {
            scrollDownBtn.setAttribute('hidden', '');
        }
    }

    /* ───────── Загрузка начальной истории ───────── */

    function loadHistory() {
        var raw = document.getElementById('initial-history').textContent.trim();
        if (!raw) return;

        try {
            var history = JSON.parse(raw);
        } catch (e) {
            return; // мёртвый JSON — игнорируем
        }

        if (history.length === 0) return;

        emptyState.hidden = true;
        chat.hidden = false;
        clearBtn.removeAttribute('hidden');

        history.forEach(function (msg) {
            chat.appendChild(createMessage(msg.role, msg.content, msg.sources || []));
        });

        // Прокручиваем к последнему сообщению
        requestAnimationFrame(scrollToBottom);
    }

    /* ───────── Отправка вопроса ───────── */

    function sendMessage() {
        var text = input.value.trim();
        if (!text || isLoading) return;

        // Показываем ленту и кнопку очистки
        emptyState.hidden = true;
        chat.hidden = false;
        clearBtn.removeAttribute('hidden');

        // Вставляем сообщение пользователя
        chat.appendChild(createMessage('user', text, []));
        input.value = '';
        input.focus();

        // Блокируем форму
        isLoading = true;
        input.disabled = true;
        sendBtn.disabled = true;

        // Индикатор «думает…»
        var typing = createTypingIndicator();
        chat.appendChild(typing);
        smoothScrollToBottom();

        // Отправляем на сервер
        fetch('/api/chat/', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'X-CSRFToken': getCookie('csrftoken')
            },
            body: JSON.stringify({ message: text })
        })
        .then(function (res) { return res.json(); })
        .then(function (data) {
            // Убираем индикатор
            typing.remove();

            if (data.error) {
                chat.appendChild(createError(data.error));
            } else if (data.answer) {
                chat.appendChild(createMessage(
                    data.answer.role,
                    data.answer.content,
                    data.answer.sources || []
                ));
            }

            isLoading = false;
            input.disabled = false;
            sendBtn.disabled = false;
            input.focus();

            // Прокрутка: если пользователь не скроллил вверх — едем вниз
            var diff = chat.scrollHeight - chat.scrollTop - chat.clientHeight;
            if (diff <= 80) {
                smoothScrollToBottom();
            } else {
                // Пользователь читает старое — не трогаем его прокрутку
                scrollDownBtn.removeAttribute('hidden');
            }
        })
        .catch(function (err) {
            typing.remove();
            chat.appendChild(createError(
                'Ошибка сети. Проверьте соединение и перезагрузите страницу.'
            ));
            isLoading = false;
            input.disabled = false;
            sendBtn.disabled = false;
            input.focus();
        });
    }

    /* ───────── Очистка диалога ───────── */

    function clearChat() {
        fetch('/api/clear/', {
            method: 'POST',
            headers: {
                'X-CSRFToken': getCookie('csrftoken')
            }
        }).then(function () {
            if (chat) chat.innerHTML = '';
            emptyState.hidden = false;
            clearBtn.setAttribute('hidden', '');
            scrollDownBtn.setAttribute('hidden', '');
            input.focus();
        });
    }

    /* ───────── Утилиты ───────── */

    function getCookie(name) {
        var value = '; ' + document.cookie;
        var parts = value.split('; ' + name + '=');
        if (parts.length === 2) {
            return parts.pop().split(';').shift();
        }
        return '';
    }

    /* ───────── Инициализация ───────── */

    // Загрузка начальной истории из JSON
    loadHistory();

    // Отправка по Enter (Shift+Enter — новая строка)
    input.addEventListener('keydown', function (e) {
        if (e.key === 'Enter' && !e.shiftKey) {
            e.preventDefault();
            sendMessage();
        }
    });

    // Отправка кнопкой
    composer.addEventListener('submit', function (e) {
        e.preventDefault();
        sendMessage();
    });

    // Очистка
    clearBtn.addEventListener('click', clearChat);

    // Кнопка «вниз»
    scrollDownBtn.addEventListener('click', smoothScrollToBottom);

    // Отслеживание прокрутки
    if (chat) {
        chat.addEventListener('scroll', updateScrollDownButton);
    }
})();
