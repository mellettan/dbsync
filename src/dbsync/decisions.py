"""
Стратегии принятия решений во время синхронизации.

Синхронизация задаёт три вопроса: удалять ли лишние объекты в prod, какой ключ использовать
для сопоставления строк и что делать при нарушении NOT NULL. `InteractiveDecider` спрашивает
пользователя в терминале, `AutoDecider` отвечает по заранее заданной политике и подходит для CI.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal, Protocol

NotNullAction = tuple[Literal["drop", "default"], str | None]


class SyncAbortedError(RuntimeError):
    """Синхронизация остановлена: решение нельзя принять без пользователя."""


class Decider(Protocol):
    def confirm_drop(self, objects: Sequence[str]) -> bool: ...

    def choose_sync_key(
        self, table_name: str, candidates: Sequence[str], primary_key: tuple[str, ...]
    ) -> tuple[str, ...] | None: ...

    def resolve_not_null(self, table_name: str, column_name: str) -> NotNullAction: ...


class InteractiveDecider:
    """Задаёт вопросы пользователю в терминале."""

    def confirm_drop(self, objects: Sequence[str]) -> bool:
        choice = input(f"Удалить {', '.join(objects)} из продуктовой базы? [y/N]: ")
        return choice.strip().lower() == "y"

    def choose_sync_key(
        self, table_name: str, candidates: Sequence[str], primary_key: tuple[str, ...]
    ) -> tuple[str, ...] | None:
        print(f"\nТаблица '{table_name}': выберите столбец для синхронизации (ID):")
        for index, column in enumerate(candidates, 1):
            suffix = " (PRIMARY KEY)" if (column,) == primary_key else ""
            print(f"{index}. {column}{suffix}")
        if primary_key and primary_key[0] not in candidates:
            print(f"p. Использовать текущий PK: {', '.join(primary_key)}")

        while True:
            choice = input("Ваш выбор (номер или 'p'): ").strip().lower()
            if choice == "p" and primary_key:
                return primary_key
            if choice.isdigit() and 0 < int(choice) <= len(candidates):
                return (candidates[int(choice) - 1],)
            print("Некорректный выбор. Пожалуйста, введите номер или 'p'.")

    def resolve_not_null(self, table_name: str, column_name: str) -> NotNullAction:
        prompt = (
            f'Столбец "{column_name}" в таблице "{table_name}" не может быть NULL.\n'
            "Укажите действие: [d] удалить ограничение NOT NULL, [v] вставить значение по умолчанию: "
        )
        while (choice := input(prompt).strip().lower()) not in {"d", "v"}:
            print("Введите d или v.")
        if choice == "d":
            return "drop", None
        return "default", input(
            f"Введите значение по умолчанию для {table_name}.{column_name}: "
        )


@dataclass(frozen=True)
class AutoDecider:
    """Неинтерактивные решения по политике.

    Атрибуты:
        allow_drop: удалять ли таблицы и колонки, которых нет в тестовой базе.
        not_null_default: значение для NULL в NOT NULL-колонках; если не задано, синхронизация
            останавливается, а не ослабляет схему prod молча.
    """

    allow_drop: bool = False
    not_null_default: str | None = None

    def confirm_drop(self, objects: Sequence[str]) -> bool:
        return self.allow_drop

    def choose_sync_key(
        self, table_name: str, candidates: Sequence[str], primary_key: tuple[str, ...]
    ) -> tuple[str, ...] | None:
        if primary_key and len(primary_key) == 1 and primary_key[0] in candidates:
            return primary_key
        return (candidates[0],) if candidates else primary_key or None

    def resolve_not_null(self, table_name: str, column_name: str) -> NotNullAction:
        if self.not_null_default is None:
            raise SyncAbortedError(
                f"NULL в NOT NULL-колонке {table_name}.{column_name}: задайте --not-null-default "
                "или запустите интерактивно"
            )
        return "default", self.not_null_default
