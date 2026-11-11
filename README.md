# Manager_di_provi

Telegram-бот с расписанием репетиций из Google Таблицы.

## Парсер расписания (`schedule_parser.py`)

Скачивает вкладку «Лист1» как CSV (только чтение, через `export?format=csv`)
и превращает её в список записей `Rehearsal`: дата, день недели, время начала/конца,
название, аудитория, участники (`Participant(name, note)` — пометка из скобок).

```bash
python schedule_parser.py                      # печать расписания с реальных данных
python -m unittest discover -s tests -t .      # тесты
```

```python
from schedule_parser import load_schedule
for r in load_schedule():
    print(r.date, r.time_str, r.room, r.title, [p.name for p in r.participants])
```

Зависимостей нет, нужен Python 3.10+.

## Бот (`bot.py`)

Бот не висит постоянно: GitHub Actions (`.github/workflows/bot.yml`) раз в 10 минут
запускает `python bot.py run` — бот читает новые сообщения через `getUpdates`,
отвечает и выходит. С 18:00 МСК тот же запуск один раз в сутки рассылает
подписчикам их репетиции на завтра. Изменения в `state.json` и `subscribers.json`
коммитятся обратно в репозиторий.

| Файл | Что хранит |
|---|---|
| `state.json` | `offset` для getUpdates и дату последней рассылки |
| `subscribers.json` | `chat_id -> имя` (только после согласия) |
| `aliases.json` | варианты имён: `"вариант": "основное имя"` — можно править |

Команды: `/start` (согласие → выбор имени), `/today`, `/tomorrow`, `/week`, `/stop`.

Ручные режимы:

```bash
python bot.py poll                  # только сообщения
python bot.py send_daily [--force]  # рассылка на завтра
python bot.py check_names           # имена без буквы фамилии, которые надо проверить
```

### Настройка

1. Создать бота у @BotFather, токен положить в секрет репозитория
   **Settings → Secrets and variables → Actions → `TELEGRAM_BOT_TOKEN`**.
   Токен нигде больше не хранится и в логи не пишется.
2. Workflow по расписанию работает только из ветки по умолчанию — смержить её туда.
3. Settings → Actions → General → Workflow permissions: **Read and write**.

### Имена

Сравнение — только полное совпадение, без регистра, `ё == е`. «Миша К» и «Миша П»,
«Вася О» и «Вася К», «Софа» и «Соня» — разные люди; если `aliases.json` попытается
их объединить, бот упадёт с ошибкой. Имя без буквы фамилии («Василиса», когда есть
«Василиса К.» и «Василиса О») бот не угадывает: по нему рассылки нет, а
`check_names` показывает такие случаи.
