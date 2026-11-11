"""Сопоставление имён участников.

Правила:
  * сравнение только по ПОЛНОМУ совпадению имени (никакого поиска подстроки);
  * без учёта регистра, «ё» == «е», лишние пробелы и точка после инициала
    («Василиса К.» == «Василиса К») не важны;
  * варианты имён сводятся к основному через aliases.json (вариант -> основное);
  * имя без буквы фамилии («Миша»), когда в таблице есть «Миша К»/«Миша П»,
    считается неоднозначным: бот не угадывает, кто это, такие случаи выводятся
    на проверку (``python bot.py check_names``).
"""

from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from schedule_parser import Rehearsal

ALIASES_FILE = Path(__file__).with_name("aliases.json")

# Записи в «участниках», которые не являются отдельными людьми:
# их нет в списке имён при регистрации, и рассылка по ним не идёт.
NON_PERSONS = {"весь курс", "хор"}

# Это РАЗНЫЕ люди. Если aliases.json сведёт любую пару к одному имени,
# загрузка упадёт с ошибкой, а не тихо перепутает рассылку.
MUST_BE_DISTINCT = [
    ("Миша К", "Миша П"),
    ("Вася О", "Вася К"),
    ("Софа", "Соня"),
    ("Софа", "Лиза"),
]


def normalize(name: str) -> str:
    """Ключ для сравнения: регистр, ё/е, пробелы, точки после инициалов."""
    s = name.replace("\xa0", " ").casefold().replace("ё", "е")
    s = s.replace(".", " ")
    return re.sub(r"\s+", " ", s).strip()


class AliasError(ValueError):
    pass


@dataclass(frozen=True)
class AmbiguousMention:
    """Имя без буквы фамилии при наличии нескольких/других вариантов."""
    name: str                  # как записано в таблице
    candidates: tuple[str, ...]  # возможные полные имена
    rehearsal: Rehearsal


class NameBook:
    def __init__(self, aliases: dict[str, str] | None = None):
        aliases = aliases or {}
        # нормализованный вариант -> основное имя (в написании из aliases.json)
        self._alias: dict[str, str] = {}
        for variant, main in aliases.items():
            if variant.startswith("_"):
                continue  # «_comment» и подобные служебные ключи
            self._alias[normalize(variant)] = main.strip()
        for a, b in MUST_BE_DISTINCT:
            if self.key(a) == self.key(b):
                raise AliasError(f"aliases.json объединяет разных людей: «{a}» и «{b}»")

    @classmethod
    def load(cls, path: Path = ALIASES_FILE) -> "NameBook":
        if not path.exists():
            return cls({})
        with path.open(encoding="utf-8") as f:
            return cls(json.load(f))

    # -- основное -------------------------------------------------------- #

    def key(self, name: str) -> str:
        """Нормализованный ключ ОСНОВНОГО имени."""
        k = normalize(name)
        if k in self._alias:
            return normalize(self._alias[k])
        return k

    def is_person(self, name: str) -> bool:
        return bool(normalize(name)) and normalize(name) not in NON_PERSONS

    def matches(self, subscriber_name: str, rehearsal: Rehearsal) -> bool:
        """Участвует ли подписчик в репетиции (полное совпадение после алиасов)."""
        k = self.key(subscriber_name)
        return any(self.key(p.name) == k for p in rehearsal.participants)

    # -- список имён для регистрации ------------------------------------- #

    def _display_names(self, rehearsals: Iterable[Rehearsal]) -> dict[str, str]:
        """ключ основного имени -> как его показывать."""
        spellings: dict[str, Counter] = defaultdict(Counter)
        for r in rehearsals:
            for p in r.participants:
                if self.is_person(p.name):
                    spellings[self.key(p.name)][p.name] += 1
        alias_main = {normalize(m): m for m in self._alias.values()}
        result = {}
        for k, counter in spellings.items():
            if k in alias_main:
                result[k] = alias_main[k]
            else:
                # с заглавной буквы лучше, затем самое частое написание
                best = max(counter, key=lambda s: (s[:1].isupper(), counter[s], s))
                result[k] = " ".join(w[:1].upper() + w[1:] for w in best.split())
        return result

    def ambiguous(self, rehearsals: list[Rehearsal]) -> list[AmbiguousMention]:
        """Имена из одного слова, для которых в таблице есть «то же имя + буква»."""
        names = self._display_names(rehearsals)
        by_first: dict[str, list[str]] = defaultdict(list)
        for k in names:
            if " " in k:
                by_first[k.split(" ", 1)[0]].append(k)
        found = []
        for r in rehearsals:
            for p in r.participants:
                k = self.key(p.name)
                if " " not in k and k in by_first:
                    found.append(AmbiguousMention(
                        p.name, tuple(sorted(names[c] for c in by_first[k])), r))
        return found

    def registration_names(self, rehearsals: list[Rehearsal]) -> list[str]:
        """Основные имена без повторов, без групп и без неоднозначных."""
        names = self._display_names(rehearsals)
        bad = {self.key(m.name) for m in self.ambiguous(rehearsals)}
        return sorted((v for k, v in names.items() if k not in bad), key=normalize)

    def find_registration_name(self, rehearsals: list[Rehearsal], key: str) -> str | None:
        for name in self.registration_names(rehearsals):
            if self.key(name) == key:
                return name
        return None
