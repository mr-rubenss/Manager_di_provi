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
