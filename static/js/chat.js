/**
 * AJAX-чат без перезагрузки страницы.
 *
 * Вопрос вместе с прикреплёнными файлами уходит multipart/form-data на
 * /api/chat/. Ответ вставляется в ленту точечно, страница не перезагружается,
 * прокруткой управляет этот скрипт.
 */
(function () {
    'use strict';

    var chat = document.getElementById('chat');
    var emptyState = document.getElementById('empty-state');
    var composer = document.getElementById('composer');
    var input = document.getElementById('input');
    var sendBtn = document.getElementById('send-btn');
    var clearBtn = document.getElementById('clear-btn');
    var exportBtn = document.getElementById('export-btn');
    var scrollDownBtn = document.getElementById('scroll-down');
    var fileInput = document.getElementById('file-input');
    var clipBtn = document.getElementById('clip-btn');
    var attachmentsBox = document.getElementById('attachments');
    var uploadNotice = document.getElementById('upload-notice');

    var MAX_FILES = 4;
    var MAX_FILE_BYTES = 10 * 1024 * 1024;

    var isLoading = false;
    var pendingFiles = [];

    var ocrReady = true;
    try {
        ocrReady = JSON.parse(document.getElementById('ocr-ready').textContent);
    } catch (e) { /* не удалось прочитать — считаем, что OCR есть */ }

    var KIND_ICONS = { pdf: '📄', docx: '📘', text: '📃', image: '🖼' };

    /* ───────── Утилиты ───────── */

    function getCookie(name) {
        var value = '; ' + document.cookie;
        var parts = value.split('; ' + name + '=');
        if (parts.length === 2) {
            return parts.pop().split(';').shift();
        }
        return '';
    }

    function humanSize(bytes) {
        if (bytes < 1024) return bytes + ' Б';
        if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(0) + ' КБ';
        return (bytes / 1024 / 1024).toFixed(1) + ' МБ';
    }

    function kindOf(name) {
        var ext = name.indexOf('.') >= 0 ? name.split('.').pop().toLowerCase() : '';
        if (ext === 'pdf') return 'pdf';
        if (ext === 'docx') return 'docx';
        if (['png', 'jpg', 'jpeg', 'gif', 'bmp', 'webp', 'tiff'].indexOf(ext) >= 0) return 'image';
        return 'text';
    }

    function showNotice(text) {
        uploadNotice.textContent = text;
        uploadNotice.hidden = !text;
    }

    /* ───────── Прикреплённые файлы ───────── */

    function renderAttachments() {
        attachmentsBox.innerHTML = '';
        if (pendingFiles.length === 0) {
            attachmentsBox.hidden = true;
            return;
        }
        attachmentsBox.hidden = false;

        pendingFiles.forEach(function (file, index) {
            var chip = document.createElement('span');
            chip.className = 'chip';

            var icon = document.createElement('span');
            icon.textContent = KIND_ICONS[kindOf(file.name)] || '📎';

            var name = document.createElement('span');
            name.className = 'chip__name';
            name.textContent = file.name;
            name.title = file.name;

            var size = document.createElement('span');
            size.className = 'chip__size';
            size.textContent = humanSize(file.size);

            var remove = document.createElement('button');
            remove.type = 'button';
            remove.className = 'chip__remove';
            remove.textContent = '✕';
            remove.title = 'Убрать файл';
            remove.addEventListener('click', function () {
                pendingFiles.splice(index, 1);
                renderAttachments();
            });

            chip.appendChild(icon);
            chip.appendChild(name);
            chip.appendChild(size);
            chip.appendChild(remove);
            attachmentsBox.appendChild(chip);
        });
    }

    function addFiles(fileList) {
        var rejected = [];

        Array.prototype.forEach.call(fileList, function (file) {
            if (pendingFiles.length >= MAX_FILES) {
                rejected.push('Можно прикрепить не более ' + MAX_FILES + ' файлов.');
                return;
            }
            if (file.size > MAX_FILE_BYTES) {
                rejected.push('«' + file.name + '» больше 10 МБ.');
                return;
            }
            pendingFiles.push(file);
        });

        renderAttachments();
        showNotice(rejected.join(' '));
    }

    /* ───────── Отрисовка сообщений ───────── */

    /** Список вложений: в сообщении он только показывает, что было приложено. */
    function createAttachmentsList(files) {
        var box = document.createElement('div');
        box.className = 'attachments attachments--static';

        files.forEach(function (file) {
            var chip = document.createElement('span');
            chip.className = 'chip';

            var icon = document.createElement('span');
            icon.textContent = KIND_ICONS[file.kind] || '📎';

            var name = document.createElement('span');
            name.className = 'chip__name';
            name.textContent = file.name;
            name.title = file.name;

            chip.appendChild(icon);
            chip.appendChild(name);
            box.appendChild(chip);
        });

        return box;
    }

    function createMessage(role, content, options) {
        options = options || {};

        var wrapper = document.createElement('div');
        wrapper.className = 'message message--' + role;

        if (content) {
            var bubble = document.createElement('div');
            bubble.className = 'message__bubble';
            // textContent + white-space: pre-wrap: переносы строк сохраняются,
            // случайный HTML не исполняется.
            bubble.textContent = content;
            wrapper.appendChild(bubble);
        }

        if (options.files && options.files.length) {
            wrapper.appendChild(createAttachmentsList(options.files));
        }

        if (options.tableHtml) {
            var tableBox = document.createElement('div');
            tableBox.className = 'message__table';
            // innerHTML здесь единственный допустимый случай: строку собрал
            // шаблон Django из проверенных данных (см. calculations.py), а не
            // модель. Текст модели в интерфейс всегда идёт через textContent.
            tableBox.innerHTML = options.tableHtml;
            wrapper.appendChild(tableBox);
        }

        if (options.notices && options.notices.length) {
            var notices = document.createElement('div');
            notices.className = 'notices';
            options.notices.forEach(function (text) {
                var line = document.createElement('span');
                line.textContent = '⚠ ' + text;
                notices.appendChild(line);
            });
            wrapper.appendChild(notices);
        }

        return wrapper;
    }

    function createTypingIndicator() {
        var wrapper = document.createElement('div');
        wrapper.className = 'message message--assistant typing-indicator';
        var bubble = document.createElement('div');
        bubble.className = 'message__bubble typing';
        bubble.innerHTML = '<span></span><span></span><span></span>';
        wrapper.appendChild(bubble);
        return wrapper;
    }

    function createError(text) {
        var div = document.createElement('div');
        div.className = 'alert';
        var strong = document.createElement('strong');
        strong.textContent = 'Не удалось получить ответ.';
        var span = document.createElement('span');
        span.textContent = text;
        div.appendChild(strong);
        div.appendChild(span);
        return div;
    }

    /* ───────── Прокрутка ───────── */

    function scrollToBottom() {
        chat.scrollTop = chat.scrollHeight;
    }

    function smoothScrollToBottom() {
        chat.scrollTo({ top: chat.scrollHeight, behavior: 'smooth' });
    }

    function isNearBottom() {
        return chat.scrollHeight - chat.scrollTop - chat.clientHeight <= 80;
    }

    function updateScrollDownButton() {
        scrollDownBtn.hidden = isNearBottom();
    }

    /** Показывает ленту, скрывая приветственный экран. */
    function showChat() {
        emptyState.hidden = true;
        chat.hidden = false;
        clearBtn.hidden = false;
        exportBtn.hidden = false;
    }

    /* ───────── Высота поля ввода ───────── */

    function autosize() {
        input.style.height = 'auto';
        input.style.height = Math.min(input.scrollHeight, 160) + 'px';
    }

    /* ───────── Отправка вопроса ───────── */

    function sendMessage() {
        var text = input.value.trim();
        if (isLoading) return;
        if (!text && pendingFiles.length === 0) return;

        showChat();

        // Вложения рисуем в сообщении по метаданным: так лента выглядит так же,
        // как после перезагрузки страницы, где сервер отдаёт только имена.
        var attachmentsMeta = pendingFiles.map(function (f) {
            return { name: f.name, kind: kindOf(f.name) };
        });
        chat.appendChild(createMessage('user', text, { files: attachmentsMeta }));

        var formData = new FormData();
        if (text) formData.append('message', text);
        pendingFiles.forEach(function (f) { formData.append('files', f); });

        pendingFiles = [];
        renderAttachments();
        showNotice('');
        input.value = '';
        autosize();
        input.focus();

        isLoading = true;
        input.disabled = true;
        sendBtn.disabled = true;
        clipBtn.disabled = true;

        var typing = createTypingIndicator();
        chat.appendChild(typing);
        smoothScrollToBottom();

        fetch('/api/chat/', {
            method: 'POST',
            headers: { 'X-CSRFToken': getCookie('csrftoken') },
            body: formData
        })
        .then(function (res) {
            return res.json().catch(function () {
                throw new Error('Сервер вернул некорректный ответ.');
            });
        })
        .then(function (data) {
            typing.remove();

            if (data.error) {
                chat.appendChild(createError(data.error));
            } else if (data.answer) {
                chat.appendChild(createMessage(
                    data.answer.role,
                    data.answer.content,
                    {
                        notices: data.answer.notices || [],
                        tableHtml: data.answer.table_html || ''
                    }
                ));
            }

            finishRequest();

            if (isNearBottom()) {
                smoothScrollToBottom();
            } else {
                scrollDownBtn.hidden = false;
            }
        })
        .catch(function (err) {
            typing.remove();
            chat.appendChild(createError(
                err.message || 'Ошибка сети. Проверьте соединение и перезагрузите страницу.'
            ));
            finishRequest();
        });
    }

    function finishRequest() {
        isLoading = false;
        input.disabled = false;
        sendBtn.disabled = false;
        clipBtn.disabled = false;
        input.focus();
    }

    /* ───────── Очистка диалога ───────── */

    function clearChat() {
        fetch('/api/clear/', {
            method: 'POST',
            headers: { 'X-CSRFToken': getCookie('csrftoken') }
        }).then(function () {
            chat.innerHTML = '';
            chat.hidden = true;
            emptyState.hidden = false;
            clearBtn.hidden = true;
            exportBtn.hidden = true;
            scrollDownBtn.hidden = true;
            pendingFiles = [];
            renderAttachments();
            showNotice('');
            input.focus();
        });
    }

    /* ───────── Начальная история ───────── */

    function loadHistory() {
        var raw = document.getElementById('initial-history').textContent.trim();
        if (!raw) return;

        var history;
        try {
            history = JSON.parse(raw);
        } catch (e) {
            return;
        }
        if (!history.length) return;

        showChat();
        history.forEach(function (msg) {
            chat.appendChild(createMessage(msg.role, msg.content, {
                files: msg.files || [],
                notices: msg.notices || [],
                tableHtml: msg.table_html || ''
            }));
        });
        requestAnimationFrame(scrollToBottom);
    }

    /* ───────── События ───────── */

    clipBtn.addEventListener('click', function () { fileInput.click(); });

    fileInput.addEventListener('change', function () {
        addFiles(fileInput.files);
        // Сбрасываем значение, чтобы тот же файл можно было выбрать повторно.
        fileInput.value = '';
    });

    // Перетаскивание файлов прямо в окно браузера.
    ['dragenter', 'dragover'].forEach(function (event) {
        document.addEventListener(event, function (e) {
            e.preventDefault();
        });
    });
    document.addEventListener('drop', function (e) {
        e.preventDefault();
        if (e.dataTransfer && e.dataTransfer.files.length) {
            addFiles(e.dataTransfer.files);
        }
    });

    input.addEventListener('input', autosize);

    input.addEventListener('keydown', function (e) {
        if (e.key === 'Enter' && !e.shiftKey) {
            e.preventDefault();
            sendMessage();
        }
    });

    composer.addEventListener('submit', function (e) {
        e.preventDefault();
        sendMessage();
    });

    clearBtn.addEventListener('click', clearChat);
    scrollDownBtn.addEventListener('click', smoothScrollToBottom);
    chat.addEventListener('scroll', updateScrollDownButton);

    /* ───────── Инициализация ───────── */

    loadHistory();

    if (!ocrReady) {
        showNotice(
            'Распознавание текста с фотографий недоступно: не установлен ' +
            'Tesseract OCR. Текстовые файлы, PDF и DOCX читаются.'
        );
    }

    input.focus();
})();