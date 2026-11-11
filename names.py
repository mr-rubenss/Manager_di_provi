"""Сопоставление имён участников.

Правила:
  * сравнение только по ПОЛНОМУ совпадению имени (никакого поиска подстроки);
  * без учёта регистра, «ё» == «е», лишние пробелы и точка после инициала
    («Василиса К.» == «Василиса К») не важны;
  * варианты имён сводятся к основному через aliases.json (вариант -> основное);
  * people.json — список известных людей: основное имя -> пол («ж», «м» или «?»);
  * групповые обозначения («весь курс», «девушки», «хор (мужчины)»…) рассылаются
    всем подписчикам или подписчикам нужного пола; при поле «?» групповая
    рассылка по полу не идёт (только «всем»);
  * имя без буквы фамилии («Миша»), когда в таблице есть «Миша К»/«Миша П»,
    считается неоднозначным: бот не угадывает, кто это;
  * любое другое слово, которого нет ни в people.json, ни среди групп, —
    непонятное: рассылки по нему нет.
  Неоднозначные и непонятные случаи показывает ``python bot.py check_names``.
"""

from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from schedule_parser import Participant, Rehearsal

ALIASES_FILE = Path(__file__).with_name("aliases.json")
PEOPLE_FILE = Path(__file__).with_name("people.json")

ALL = "все"
FEMALE = "ж"
MALE = "м"
UNKNOWN_GENDER = "?"

# Групповые обозначения (ключи — после normalize). Пометка в скобках
# присоединяется к слову: «хор (женщины)» == «хор женщины».
GROUPS = {
    "весь курс": ALL, "все": ALL, "хор": ALL,
    "все девушки": FEMALE, "девушки": FEMALE,
    "хор женщины": FEMALE, "хор девушки": FEMALE,
    "все парни": MALE, "парни": MALE,
    "хор мужчины": MALE, "хор парни": MALE,
}

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


def group_of(p: Participant) -> str | None:
    """ALL / FEMALE / MALE для группового обозначения, иначе None."""
    if p.note:
        full = normalize(f"{p.name} {p.note}")
        if full in GROUPS:
            return GROUPS[full]
    return GROUPS.get(normalize(p.name))


@dataclass(frozen=True)
class UnknownMention:
    """Слово в участниках: не человек из people.json и не группа."""
    name: str
    rehearsal: Rehearsal


@dataclass(frozen=True)
class AmbiguousMention:
    """Имя без буквы фамилии при наличии нескольких/других вариантов."""
    name: str                  # как записано в таблице
    candidates: tuple[str, ...]  # возможные полные имена
    rehearsal: Rehearsal


class NameBook:
    def __init__(self, aliases: dict[str, str] | None = None,
                 people: dict[str, str] | None = None):
        """people=None — реестра людей нет: любое слово, кроме группы, — имя."""
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

        # ключ основного имени -> (как писать, пол)
        self._people: dict[str, tuple[str, str]] | None = None
        if people is not None:
            self._people = {}
            for name, gender in people.items():
                if name.startswith("_"):
                    continue
                gender = gender.strip()
                if gender not in (FEMALE, MALE, UNKNOWN_GENDER):
                    raise ValueError(f"people.json: у «{name}» пол «{gender}», "
                                     f"нужно «ж», «м» или «?»")
                self._people[self.key(name)] = (name.strip(), gender)

    @classmethod
    def load(cls, aliases_path: Path = ALIASES_FILE,
             people_path: Path = PEOPLE_FILE) -> "NameBook":
        def read(path):
            if not path.exists():
                return None
            with path.open(encoding="utf-8") as f:
                return json.load(f)
        return cls(read(aliases_path) or {}, read(people_path))

    # -- основное -------------------------------------------------------- #

    def key(self, name: str) -> str:
        """Нормализованный ключ ОСНОВНОГО имени."""
        k = normalize(name)
        if k in self._alias:
            return normalize(self._alias[k])
        return k

    def is_person(self, p: Participant) -> bool:
        """Похоже на отдельного человека (не пусто и не группа)."""
        return bool(normalize(p.name)) and group_of(p) is None

    def is_known(self, name: str) -> bool:
        """Есть в people.json (без реестра — любое имя)."""
        return self._people is None or self.key(name) in self._people

    def gender(self, name: str) -> str | None:
        """«ж» / «м»; None — пол не указан или «?»."""
        if self._people is None:
            return None
        entry = self._people.get(self.key(name))
        return entry[1] if entry and entry[1] != UNKNOWN_GENDER else None

    def matches(self, subscriber_name: str, rehearsal: Rehearsal) -> bool:
        """Касается ли репетиция подписчика.

        Да, если он указан по имени (полное совпадение после алиасов, имя
        есть в people.json) или указана группа: «всем» или его пол.
        """
        k = self.key(subscriber_name)
        gender = self.gender(subscriber_name)
        for p in rehearsal.participants:
            group = group_of(p)
            if group is not None:
                if group == ALL or (gender is not None and group == gender):
                    return True
            elif self.key(p.name) == k and self.is_known(p.name):
                return True
        return False

    # -- список имён для регистрации ------------------------------------- #

    def _display_names(self, rehearsals: Iterable[Rehearsal]) -> dict[str, str]:
        """ключ основного имени -> как его показывать."""
        spellings: dict[str, Counter] = defaultdict(Counter)
        for r in rehearsals:
            for p in r.participants:
                if self.is_person(p):
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

    def unknown(self, rehearsals: list[Rehearsal]) -> list[UnknownMention]:
        """Слова, которых нет ни в people.json, ни среди групп (кроме неоднозначных)."""
        if self._people is None:
            return []
        ambiguous = {self.key(m.name) for m in self.ambiguous(rehearsals)}
        return [UnknownMention(p.name, r) for r in rehearsals for p in r.participants
                if self.is_person(p) and not self.is_known(p.name)
                and self.key(p.name) not in ambiguous]

    def unknown_gender(self) -> list[str]:
        """Люди из people.json с полом «?»."""
        if self._people is None:
            return []
        return sorted((n for n, g in self._people.values() if g == UNKNOWN_GENDER),
                      key=normalize)

    def registration_names(self, rehearsals: list[Rehearsal]) -> list[str]:
        """Основные имена без повторов: из таблицы, есть в people.json, не группы
        и не неоднозначные."""
        names = self._display_names(rehearsals)
        bad = {self.key(m.name) for m in self.ambiguous(rehearsals)}
        result = []
        for k, display in names.items():
            if k in bad or not self.is_known(display):
                continue
            result.append(self._people[k][0] if self._people is not None else display)
        return sorted(result, key=normalize)

    def find_registration_name(self, rehearsals: list[Rehearsal], key: str) -> str | None:
        for name in self.registration_names(rehearsals):
            if self.key(name) == key:
                return name
        return None
