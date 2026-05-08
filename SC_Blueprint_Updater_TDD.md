# SC Blueprint Updater — Technical Design Document

## 1. Назначение

Приложение отслеживает лог-файлы Star Citizen, извлекает названия полученных
blueprint'ов (BP), сохраняет их список и автоматически выделяет их вхождения
в файле локализации `global.ini` с помощью настраиваемого шаблона.
Поддерживается автоматическое скачивание `global.ini` при его отсутствии.

---

## 2. Файловая структура

### Игровые каталоги

```
$root/                                              ← пользователь выбирает один раз
  $env/                                             ← окружение (LIVE, PTU, и т.д.)
    Bin64/                                          ← маркер SC-окружения (обязателен)
    Data.p4k                                        ← маркер SC-окружения (обязателен)
    data/                                           ← каталог данных (необязателен при проверке)
    user.cfg                                        ← опционально: g_language = <lang>
    Game.log                                        ← активный лог, постоянно дописывается
    logbackups/*.log                                ← архивные логи, не меняются
    data/Localization/<lang>/global.ini             ← файл локализации (UTF-8 + BOM, KEY=VALUE)
    data/Localization/<lang>/global.ini.original    ← резервная копия, создаётся один раз
```

`<lang>` определяется из `user.cfg` (ключ `g_language`), по умолчанию `english`.

### Настройки приложения

Хранятся в `Documents\SCBlueprintUpdater\` — **не** в `AppData`, чтобы обойти
VFS-редирект MS Store Python (подробнее в разделе 6.2).

```
Documents\SCBlueprintUpdater\
  settings.json           ← глобальные: root_path, template, ini_source
  $env\
    blueprints.txt        ← список BP, по одному на строку (plain text, редактируется вручную)
    scanned_logs.json     ← {filepath: {size, mtime}} уже просканированных logbackups
    env_settings.json     ← per-env настройки: bp_pattern
```

---

## 3. Архитектура

### 3.1 Потоки

Приложение использует два потока:

| Поток                  | Ответственность                                             |
|------------------------|-------------------------------------------------------------|
| Главный поток (Qt)     | Все виджеты, таймеры, взаимодействие с пользователем       |
| Воркер-поток (QThread) | Сканирование логов, модификация INI — никакого доступа к UI |

**Критическое правило:** воркер не содержит ни одного `QTimer`. Все таймеры
живут в главном потоке. Вызовы методов воркера из главного потока осуществляются
**только** через `QMetaObject.invokeMethod(..., Qt.QueuedConnection)`. Методы
воркера, вызываемые таким образом, должны быть декорированы `@Slot()`, иначе
Qt их не найдёт.

Прямой вызов методов воркера из главного потока (`self._worker.method()`) —
**запрещён**: приводит к race condition и нативному крашу без Python traceback.

### 3.2 Классы

```
MainWindow (QMainWindow)
├── QStackedWidget
│   ├── SetupPage (QWidget)              ← страница выбора окружения
│   └── MonitorPage (QWidget)            ← страница мониторинга
│       ├── poll_timer (QTimer, 60s)     ← главный поток
│       ├── countdown_timer (1s)         ← главный поток
│       └── _bp_watcher (QFileSystemWatcher) ← следит за blueprints.txt
├── ScanWorker (QObject)                 ← воркер-поток, без QTimer
├── DownloadWorker (QObject)             ← скачивание global.ini в отдельном потоке
└── DownloadProgressDialog (QDialog)     ← модальный диалог прогресса загрузки
```

### 3.3 Поток сигналов

```
Главный поток                              Воркер-поток
─────────────────────────────────────────────────────────────
thread.started ───────────────────────►  initial_scan()
                                           └─ log_message.emit()        ──► _append_log()
                                           └─ blueprints_updated.emit() ──► _update_bp_list()
                                           └─ ini_updated.emit()         ──► _on_ini_updated()

poll_timer.timeout ────invokeMethod────►  poll_game_log()
btn_edit + watcher.fileChanged ────────►  _reload_blueprints()
                                  └──invokeMethod──► reload_blueprints()

MonitorPage.status_changed ────────────► MainWindow._on_monitor_status()
                                           └─ _status_indicator (QLabel в статус-баре)
```

---

## 4. Алгоритмы

### 4.1 Сканирование логов

**logbackups/ (один раз при старте):**
1. Загрузить `scanned_logs.json`
2. Для каждого `.log` файла: пропустить если `size` и `mtime` совпадают
   с сохранёнными
3. Новые/изменившиеся файлы читать целиком, извлечь BP, обновить
   `scanned_logs.json`

**Game.log (каждые 60 секунд):**
1. Сравнить `current_size` с сохранённым `offset`
2. Если `current_size < offset` — игра перезапустилась, файл пересоздан
   → сбросить `offset = 0`
3. Если `current_size == offset` — новых данных нет → пропустить
4. Иначе: читать от `offset` до конца файла, обновить `offset`

**Регулярное выражение для извлечения BP:**

Хранится в `env_settings.json` как `bp_pattern` (per-env).
Значение по умолчанию:
```
Received Blueprint:\s+(.+?):\s
```
При первом запуске env значение записывается в `env_settings.json` — опытные
пользователи могут изменить его вручную (например, для локализованных клиентов:
`ПОЛУЧЕН ЧЕРТЕЖ:\s+(.+?):\s`).

`ScanWorker` компилирует паттерн один раз при создании через
`compile_bp_pattern()`. При невалидном regex — молча откатывается к дефолту.

Захваченная группа обязательно проходит `.strip()` — в логе перед двоеточием
может стоять пробел, который иначе попадёт в имя BP и вызовет двойное выделение
при модификации INI.

### 4.2 Определение языка

```python
def get_language(env_path) -> str:
```

Читает `$env/user.cfg` построчно, ищет `g_language = <value>` (без кавычек,
регистронезависимо). Если файл отсутствует или ключ не найден — возвращает
`"english"`. Используется при построении пути к `global.ini` и
`global.ini.original`.

### 4.3 Валидация окружения

```python
def is_valid_sc_env(env_path) -> bool:
```

Проверяет наличие `Bin64/` (dir) и `Data.p4k` (file). Если хотя бы один маркер
отсутствует — окружение считается некорректным, кнопка Start Monitoring
заблокирована. Каталог `data/` не является обязательным маркером: он может
отсутствовать в чистом дистрибутиве и создаётся при необходимости (например,
при скачивании `global.ini`).

### 4.4 Скачивание global.ini

Если `global.ini` отсутствует при нажатии Start Monitoring:
1. Показывается диалог подтверждения с URL источника
2. `DownloadProgressDialog` запускает `DownloadWorker` в отдельном QThread
3. `DownloadWorker` поддерживает отмену; временный файл `.downloading`
   удаляется при ошибке
4. После успешного скачивания — атомарная замена через `shutil.move()`,
   создаётся `.original`

Источник (URL или локальный путь) задаётся в настройках (`ini_source`).
Дефолтный URL:
```
https://raw.githubusercontent.com/MrKraken/StarStrings/master/Data/Localization/english/global.ini
```

### 4.5 Модификация global.ini

**Шаблон выделения:**
Строка с плейсхолдером `$NAME`, например `$NAME [+]` (по умолчанию) или
`<EM2>$NAME</EM2>`. Разбивается на `(prefix, postfix)` функцией
`parse_template()`.

**Алгоритм `apply_highlights_to_value()`:**

Наивный подход (обработка BP по одному) ломается когда одно BP является
подстрокой другого (например `"Core"` и `"Antium Core"`). Используется
overlap-aware алгоритм:

1. Для всех BP (отсортированных от длинных к коротким) найти все вхождения
   в строке
2. Для каждого вхождения: если оно перекрывается с уже "занятым" диапазоном —
   пропустить (длинный BP имеет приоритет)
3. Собрать список непересекающихся диапазонов, отсортировать по позиции
4. Построить результирующую строку за один проход, оборачивая каждый диапазон
   в prefix/postfix
5. Проверка "уже выделено": если перед началом диапазона стоит `prefix` и после
   конца стоит `postfix` — не оборачивать повторно

**Regex-паттерны компилируются один раз** перед обходом файла
(`compile_bp_patterns()`), а не на каждой строке. Без этого 26 BP × 46 000
строк = 1.2 млн компиляций regex, что создаёт значительную нагрузку.

**Запись файла:**
Потоковая обработка через временный файл — одна строка за раз, без загрузки
всего файла в память (global.ini ~30–50 МБ):

```
Открыть global.ini на чтение (побайтово)
Открыть .tmp на запись
  → обработать BOM
  → для каждой строки: apply_highlights_to_value() → записать в .tmp
shutil.move(.tmp → global.ini)   ← атомарная замена
```

Защита от параллельного запуска: `threading.Lock()` (`_ini_lock`) гарантирует
что только один `update_ini_file` работает в любой момент.

### 4.6 Фильтрация списка BP

```python
def matches_filter(name: str, pattern: str) -> bool:
```

Glob-стиль: `*` — wildcard, остальные символы — литералы. Алгоритм: разбить
паттерн по `*`, проверить что каждая часть встречается в строке в порядке
следования (case-insensitive). Без `*` — простой поиск подстроки.

Примеры: `core` → любое имя содержащее `core`;
`antium*camo` → `antium` встречается раньше `camo`.

При активном фильтре: в конце списка добавляется disabled-элемент
`*** N blueprints filtered out ***`; счётчик показывает `Blueprints (6/17)`.
При пустом фильтре — `Blueprints (17)`.

---

## 5. UI

### SetupPage

- Выбор root-каталога; диалог открывается в текущем root-пути если он задан
- Список окружений: корректные SC-окружения (Bin64/ + Data.p4k) показываются
  обычным текстом, некорректные — серым; при выборе некорректного — подробное
  сообщение с подсказкой про стандартный путь SC
- Инфо-панель: путь, статус SC-установки, язык (из user.cfg или дефолт),
  статус global.ini, Game.log, logbackups, кол-во известных BP
- Поле шаблона выделения с кнопкой Reset (сохраняется в `settings.json`)
- Поле источника global.ini (URL или путь) с кнопками Browse и Reset
  (сохраняется в `settings.json`)
- Кнопка "Start Monitoring": при отсутствии global.ini — запрашивает
  скачивание перед стартом

### MonitorPage

- Статус мониторинга (`● Monitoring — next scan in 47s` / `● Idle`) —
  в правой части статус-бара главного окна; название окружения — в левой части
- Список BP (левая панель): показывает отфильтрованные BP, поддерживает
  glob-фильтр
- Лог активности (правая панель) с кнопкой Clear
- Кнопки над списком BP: **✏️ Edit list** (открыть `blueprints.txt`
  в системном редакторе), **🔁 Re-apply** (перечитать список с диска
  и переприменить к INI)
- Автоматический Re-apply при изменении `blueprints.txt`
  (отслеживается через `QFileSystemWatcher`)
- Поле фильтра под списком BP
- Кнопка **🔙 Restore Original Localization** (активна только если `.original`
  существует) — восстанавливает global.ini из резервной копии с подтверждением

---

## 6. Ключевые технические решения

### 6.1 Таймеры только в главном потоке

**Проблема:** `QTimer`, созданный в воркер-потоке, нельзя остановить из другого
потока — Qt выдаёт `QObject::killTimer: Timers cannot be stopped from another
thread` и поток не завершается корректно.

**Решение:** оба таймера (`poll_timer` 60 с и `countdown_timer` 1 с) создаются
в `MonitorPage` (главный поток). При `stop_monitoring()` они останавливаются
без каких-либо cross-thread операций.

### 6.2 Директория настроек — Documents, не AppData

**Проблема:** MS Store Python перенаправляет все операции с `AppData\Roaming`
и `AppData\Local` в приватную sandbox-папку пакета
(`LocalCache\...\SCBlueprintUpdater`). `os.environ["APPDATA"]` и даже явно
построенный путь через `USERPROFILE` возвращают "правильный" путь, но `mkdir`
и файловые операции физически происходят в sandbox — Windows перехватывает их
на уровне ядра (VFS redirection).

**Решение:** использовать `Documents` (`SHGetFolderPathW` через `ctypes`,
CSIDL_PERSONAL=5) — папка Documents не входит в список VFS-редиректов
для Store-приложений.

### 6.3 Нативные крэши без Python traceback

**Проблема:** крэши в воркер-потоке (включая прямые вызовы его методов из
главного потока) приводят к `0xc0000005 Access Violation` в `python313.dll` —
Qt не передаёт исключение в Python, программа закрывается молча.

**Решение:**
- Обернуть все публичные методы воркера в `try/except` с `traceback.format_exc()`
  и эмитом через `log_message`
- Все вызовы методов воркера — только через
  `QMetaObject.invokeMethod(..., Qt.QueuedConnection)`
- Все вызываемые методы — декорированы `@Slot()`

### 6.4 Trailing space в именах BP

**Проблема:** в логе строка выглядит как
`Received Blueprint: Antium Core Moss Camo : ` — пробел перед финальным
двоеточием является частью разделителя, но паттерн `(.+?):` захватывает его
в имя. В памяти BP хранится с пробелом, хотя `load_blueprints()` делает
`.strip()` при чтении с диска. В итоге в одной сессии в множестве оказываются
и `"Antium Core Moss Camo "` и `"Antium Core Moss Camo"`, что приводит
к двойному тегированию (`[+][+]`).

**Решение:** `extract_blueprints_from_text()` делает `.strip()` на каждом
найденном имени сразу при извлечении.

### 6.5 Потоковая запись INI через временный файл

**Проблема:** загрузка всего global.ini в память создаёт ~6× дублирование
данных (raw bytes + str + lines list + new_lines list + result str + result
bytes). При файле 50 МБ это ~300 МБ, что вызывает OOM-краш.

**Решение:** читать и писать построчно, временный файл создаётся в той же
директории что и global.ini, после успешного завершения заменяется атомарно
через `shutil.move()`.

### 6.6 QFileSystemWatcher и атомарное сохранение

**Проблема:** большинство текстовых редакторов сохраняют файлы атомарно
(write temp → rename). При этом `QFileSystemWatcher` теряет отслеживание
исходного пути — после rename путь удаляется из списка watched files.

**Решение:** в слоте `_on_blueprints_file_changed` после получения события
повторно добавляем путь в watcher если он пропал. При открытии редактора
через "Edit list" — добавляем путь сразу (файл мог быть создан только что).

### 6.7 Скачивание global.ini в отдельном потоке

**Проблема:** global.ini весит 30–50 МБ; синхронное скачивание заморозит UI
на несколько секунд.

**Решение:** `DownloadWorker` работает в отдельном `QThread`, эмитит
`progress(downloaded, total)`. `DownloadProgressDialog` показывает прогресс-бар
и кнопку Cancel. Поддерживается как HTTP/HTTPS URL, так и локальный путь
(через `shutil.copy2`). Временный файл `.downloading` гарантирует, что при
ошибке или отмене целевой файл не остаётся в неполном состоянии.

---

## 7. Зависимости и запуск

```
Python 3.12+ с python.org (НЕ из Microsoft Store)
PySide6 >= 6.5
```

**Важно:** Python из Microsoft Store несовместим с приложением по двум причинам:
1. VFS-редирект AppData (раздел 6.2)
2. Нестабильность при интенсивных файловых операциях
   (`0xc0000005` в `python313.dll`)

**Сборка exe:**
```
python -m PyInstaller --onefile --windowed --name "SC Blueprint Updater" sc_blueprint_updater.py
```
Использовать `PyInstaller` с заглавной буквы (`-m PyInstaller`) — Python 3.14
чувствителен к регистру в `-m`.

---

## 8. Структура модуля

```
sc_blueprint_updater.py
│
├── Paths & Settings
│   ├── get_app_data_dir()               ← Documents\SCBlueprintUpdater (обходит VFS)
│   ├── get_*/load_*/save_settings()     ← глобальные настройки (root_path, template, ini_source)
│   ├── get_*/load_*/save_blueprints()   ← blueprints.txt per-env
│   ├── get_*/load_*/save_scanned_logs() ← scanned_logs.json per-env
│   ├── get_*/load_*/save_env_settings() ← env_settings.json per-env
│   └── get_bp_pattern()                ← читает bp_pattern, пишет дефолт при первом запуске
│
├── Log parsing
│   ├── DEFAULT_BP_PATTERN              ← строка-дефолт regex
│   ├── compile_bp_pattern()            ← компилирует, откатывается к дефолту при ошибке
│   ├── extract_blueprints_from_text()  ← принимает скомпилированный pattern
│   ├── scan_log_file()                 ← читает файл целиком
│   └── scan_log_file_from_offset()     ← читает с байтового смещения
│
├── INI modification
│   ├── get_language()                  ← читает g_language из user.cfg, дефолт "english"
│   ├── get_ini_path()                  ← использует get_language()
│   ├── get_ini_backup_path()           ← использует get_language()
│   ├── is_valid_sc_env()               ← проверяет Bin64/ и Data.p4k
│   ├── matches_filter()                ← glob-style фильтр для списка BP
│   ├── _ini_lock                       ← threading.Lock()
│   ├── parse_template()                ← "$NAME [+]" → ("", " [+]")
│   ├── compile_bp_patterns()           ← компилирует regex один раз
│   ├── apply_highlights_to_value()     ← overlap-aware выделение
│   ├── update_ini_file()               ← потоковая запись через .tmp
│   ├── backup_ini_if_needed()          ← создаёт .original при первом запуске
│   └── restore_ini_from_backup()       ← восстанавливает из .original
│
├── Download
│   ├── DownloadWorker(QObject)         ← HTTP/локальное копирование в отдельном потоке
│   └── DownloadProgressDialog(QDialog) ← модальный диалог с прогресс-баром и Cancel
│
├── ScanWorker(QObject)                 ← воркер-поток, без QTimer
│   ├── @Slot initial_scan()
│   ├── @Slot poll_game_log()
│   ├── @Slot rescan_ini()
│   └── @Slot reload_blueprints()
│
├── UI (PySide6, тёмная тема)
│   ├── DARK_STYLE                      ← QSS stylesheet
│   ├── SetupPage(QWidget)              ← выбор root/env/template/ini_source
│   ├── MonitorPage(QWidget)            ← владеет poll_timer, countdown_timer, _bp_watcher
│   └── MainWindow(QMainWindow)         ← статус-бар с индикатором мониторинга
│
└── main()                              ← диагностика путей, запуск QApplication
```
