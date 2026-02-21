from collections.abc import Sequence
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from dbsync.config import SyncConfig
from dbsync.database import ColumnSchema, ForeignKeySchema, SchemaSnapshot, TableSchema
from dbsync.runner import (
    _compute_diff,
    _select_sync_column,
    _sync_data,
    run_sync,
)


def _mk_table(
    name: str,
    column_names: list[str],
    foreign_keys: tuple[ForeignKeySchema, ...] = (),
) -> TableSchema:
    """
    Вспомогательная функция для создания объекта TableSchema.
    """
    cols = [
        ColumnSchema(name=col, column_type="text", is_nullable=True, default=None)
        for col in column_names
    ]
    return TableSchema(
        name=name,
        columns=tuple(cols),
        primary_key=tuple(cols[0].name for _ in (0,)),
        foreign_keys=foreign_keys,
    )


def _mk_fk(
    constraint_name: str = "fk",
    columns: tuple[str, ...] = ("user_id",),
    referenced_table: str = "users",
    referenced_columns: tuple[str, ...] = ("id",),
) -> ForeignKeySchema:
    return ForeignKeySchema(
        constraint_name=constraint_name,
        columns=columns,
        referenced_table=referenced_table,
        referenced_columns=referenced_columns,
        on_update="NO ACTION",
        on_delete="NO ACTION",
        match_option=None,
    )


def test_compute_diff_detects_new_and_missing_tables() -> None:
    """
    Тестирует функцию _compute_diff на корректное обнаружение новых и отсутствующих таблиц.
    """
    test_schema = SchemaSnapshot.from_tables(
        [
            _mk_table("a", ["id"]),
            _mk_table("b", ["id"]),  # Новая таблица в тестовой схеме
        ]
    )
    prod_schema = SchemaSnapshot.from_tables(
        [
            _mk_table("a", ["id"]),
            _mk_table("c", ["id"]),  # Отсутствующая таблица в тестовой схеме
        ]
    )

    diff = _compute_diff(test_schema, prod_schema)
    # Проверяем, что 'b' обнаружена как новая таблица
    assert {table.name for table in diff.new_tables} == {"b"}
    # Проверяем, что 'c' обнаружена как отсутствующая таблица
    assert {table.name for table in diff.missing_tables} == {"c"}


def test_compute_diff_reports_orphan_and_missing_columns() -> None:
    """
    Тестирует функцию _compute_diff на корректное обнаружение "лишних" и отсутствующих колонок.
    """
    test_schema = SchemaSnapshot.from_tables(
        [
            _mk_table(
                "users", ["id", "name"]
            ),  # 'name' - новая колонка в тестовой схеме
        ]
    )
    prod_schema = SchemaSnapshot.from_tables(
        [
            _mk_table(
                "users", ["id", "email"]
            ),  # 'email' - "лишняя" колонка в продуктовой схеме
        ]
    )

    diff = _compute_diff(test_schema, prod_schema)
    # Проверяем, что 'email' обнаружена как "лишняя" колонка
    assert diff.orphan_columns == {"users": ["email"]}
    # Проверяем, что 'name' обнаружена как отсутствующая колонка
    assert {col.name for col in diff.missing_columns["users"]} == {"name"}


def test_compute_diff_reports_missing_foreign_keys() -> None:
    fk = _mk_fk(constraint_name="users_fk")
    test_table = _mk_table("orders", ["id", "user_id"], foreign_keys=(fk,))
    prod_table = _mk_table("orders", ["id", "user_id"])

    test_schema = SchemaSnapshot.from_tables([test_table])
    prod_schema = SchemaSnapshot.from_tables([prod_table])

    diff = _compute_diff(test_schema, prod_schema)
    assert diff.missing_foreign_keys == {"orders": [fk]}


def test_compute_diff_includes_foreign_keys_for_new_tables() -> None:
    fk = _mk_fk(constraint_name="users_fk")
    test_table = _mk_table("orders", ["id", "user_id"], foreign_keys=(fk,))

    test_schema = SchemaSnapshot.from_tables([test_table])
    prod_schema = SchemaSnapshot.from_tables([])

    diff = _compute_diff(test_schema, prod_schema)
    assert diff.new_tables[0].name == "orders"
    assert diff.missing_foreign_keys == {"orders": [fk]}


def test_run_sync_adds_missing_foreign_keys(monkeypatch) -> None:
    fk = _mk_fk(constraint_name="orders_fk")
    test_table = _mk_table("orders", ["id", "user_id"], foreign_keys=(fk,))
    prod_table = _mk_table("orders", ["id", "user_id"])

    test_snapshot = SchemaSnapshot.from_tables([test_table])
    prod_snapshot = SchemaSnapshot.from_tables([prod_table])

    snapshots = iter([test_snapshot, prod_snapshot, prod_snapshot])
    inspector_instances: list[Any] = []

    class FakeInspector:
        def __init__(self, dsn: str, decider: Any = None) -> None:
            self.dsn = dsn
            self.added_foreign_keys: list[tuple[str, ForeignKeySchema]] = []
            inspector_instances.append(self)

        def fetch_schema(self) -> SchemaSnapshot:
            return next(snapshots)

        def create_table(self, table: TableSchema) -> None:
            pass

        def add_column(self, table_name: str, column: ColumnSchema) -> None:
            pass

        def drop_column(self, table_name: str, column_name: str) -> None:
            pass

        def drop_table(self, table_name: str) -> None:
            pass

        def add_foreign_key(self, table_name: str, fk: ForeignKeySchema) -> None:
            self.added_foreign_keys.append((table_name, fk))

        # _sync_data helpers
        def fetch_rows(
            self, table_name: str, columns: list[str]
        ) -> list[dict[str, Any]]:
            return []

        def fetch_primary_key_values(
            self, table_name: str, primary_key: Sequence[str]
        ) -> set[tuple[Any, ...]]:
            return set()

        def insert_rows(
            self,
            table_name: str,
            columns: Sequence[str],
            rows: Sequence[dict[str, Any]],
        ) -> int:
            return 0

        def update_rows(
            self,
            table_name: str,
            sync_key: Sequence[str],
            columns: Sequence[str],
            rows: Sequence[dict[str, Any]],
        ) -> int:
            return 0

        def is_column_unique(self, table_name: str, column_name: str) -> bool:
            return True

    monkeypatch.setattr("dbsync.runner.PostgresInspector", FakeInspector)
    monkeypatch.setattr(
        "dbsync.runner._select_sync_column", lambda *args, **kwargs: ("id",)
    )

    config = SyncConfig(test_dsn="test", prod_dsn="prod")
    run_sync(config)

    prod_inspectors = [inst for inst in inspector_instances if inst.dsn == "prod"]
    assert prod_inspectors, "No PostgresInspector(prod) instances were created"
    last_prod_inspector = prod_inspectors[-1]
    assert last_prod_inspector.added_foreign_keys == [("orders", fk)]


@pytest.fixture
def mock_inspectors():
    """
    Фикстура для создания моков PostgresInspector.
    """
    mock_test_inspector = MagicMock()
    mock_prod_inspector = MagicMock()
    return mock_test_inspector, mock_prod_inspector


@pytest.fixture
def mock_select_sync_column():
    """
    Фикстура для мокирования функции _select_sync_column.
    """
    with patch("dbsync.runner._select_sync_column") as mock:
        yield mock


def test_sync_data_inserts_new_rows(mock_inspectors, mock_select_sync_column) -> None:
    """
    Тестирует функцию _sync_data на корректную вставку новых строк.
    """
    mock_test_inspector, mock_prod_inspector = mock_inspectors

    # Мокируем _select_sync_column, чтобы он возвращал "id" как ключ синхронизации
    mock_select_sync_column.return_value = ("id",)

    # Схемы таблиц
    test_table = _mk_table("users", ["id", "name"])
    prod_table = _mk_table("users", ["id", "name"])

    test_schema = SchemaSnapshot.from_tables([test_table])
    prod_schema = SchemaSnapshot.from_tables([prod_table])

    # Мокируем данные, которые будут возвращены инспекторами
    mock_test_inspector.fetch_rows.return_value = [
        {"id": 1, "name": "Alice"},
        {"id": 2, "name": "Bob"},
    ]
    mock_prod_inspector.fetch_primary_key_values.return_value = (
        set()
    )  # В продуктовой базе нет строк
    mock_prod_inspector.insert_rows.return_value = 2  # Две строки вставлены

    _sync_data(test_schema, prod_schema, mock_test_inspector, mock_prod_inspector)

    # Проверяем, что fetch_rows был вызван для тестовой базы
    mock_test_inspector.fetch_rows.assert_called_once_with("users", ["id", "name"])
    # Проверяем, что insert_rows был вызван для продуктовой базы с правильными данными
    mock_prod_inspector.insert_rows.assert_called_once_with(
        "users", ["id", "name"], [{"id": 1, "name": "Alice"}, {"id": 2, "name": "Bob"}]
    )
    # Проверяем, что update_rows не был вызван
    mock_prod_inspector.update_rows.assert_not_called()


def test_sync_data_updates_existing_rows(
    mock_inspectors, mock_select_sync_column
) -> None:
    """
    Тестирует функцию _sync_data на корректное обновление существующих строк.
    """
    mock_test_inspector, mock_prod_inspector = mock_inspectors

    # Мокируем _select_sync_column, чтобы он возвращал "id" как ключ синхронизации
    mock_select_sync_column.return_value = ("id",)

    # Схемы таблиц
    test_table = _mk_table("users", ["id", "name"])
    prod_table = _mk_table(
        "users", ["id", "name"]
    )  # Обе схемы имеют одинаковые колонки

    test_schema = SchemaSnapshot.from_tables([test_table])
    prod_schema = SchemaSnapshot.from_tables([prod_table])

    # Мокируем данные, которые будут возвращены инспекторами
    mock_test_inspector.fetch_rows.return_value = [
        {"id": 1, "name": "Alice Updated"},
    ]
    # В продуктовой базе уже есть строка с id=1
    mock_prod_inspector.fetch_primary_key_values.return_value = {(1,)}
    mock_prod_inspector.update_rows.return_value = 1  # Одна строка обновлена

    _sync_data(test_schema, prod_schema, mock_test_inspector, mock_prod_inspector)

    # Проверяем, что fetch_rows был вызван для тестовой базы
    mock_test_inspector.fetch_rows.assert_called_once_with("users", ["id", "name"])
    # Проверяем, что update_rows был вызван для продуктовой базы с правильными данными
    mock_prod_inspector.update_rows.assert_called_once_with(
        "users", ("id",), ["id", "name"], [{"id": 1, "name": "Alice Updated"}]
    )
    # Проверяем, что insert_rows не был вызван
    mock_prod_inspector.insert_rows.assert_not_called()


def test_sync_data_skips_rows_with_pk_conflict(
    mock_inspectors, mock_select_sync_column
) -> None:
    """
    Тестирует функцию _sync_data на пропуск строк с конфликтом первичного ключа при вставке.
    """
    mock_test_inspector, mock_prod_inspector = mock_inspectors

    # Мокируем _select_sync_column, чтобы он возвращал "id" как ключ синхронизации
    mock_select_sync_column.return_value = ("id",)

    # Схемы таблиц
    test_table = _mk_table("users", ["id", "name"])
    prod_table = _mk_table(
        "users", ["id", "name"]
    )  # Обе схемы имеют одинаковые колонки

    test_schema = SchemaSnapshot.from_tables([test_table])
    prod_schema = SchemaSnapshot.from_tables([prod_table])

    # Мокируем данные, которые будут возвращены инспекторами
    mock_test_inspector.fetch_rows.return_value = [
        {"id": 1, "name": "Alice"},  # Эта строка будет пропущена из-за PK конфликта
        {"id": 2, "name": "Bob"},
    ]
    # В продуктовой базе нет строки с id=1 по sync_key, но есть по реальному PK
    mock_prod_inspector.fetch_primary_key_values.side_effect = [
        set(),  # Для sync_key
        {(1,)},  # Для реального PK
    ]
    mock_prod_inspector.insert_rows.return_value = (
        1  # Одна строка вставлена (только Bob)
    )

    _sync_data(test_schema, prod_schema, mock_test_inspector, mock_prod_inspector)

    # Проверяем, что fetch_rows был вызван для тестовой базы
    mock_test_inspector.fetch_rows.assert_called_once_with("users", ["id", "name"])
    # Проверяем, что insert_rows был вызван только для строки "Bob"
    mock_prod_inspector.insert_rows.assert_called_once_with(
        "users", ["id", "name"], [{"id": 2, "name": "Bob"}]
    )
    # Проверяем, что update_rows не был вызван
    mock_prod_inspector.update_rows.assert_not_called()


def test_sync_data_skips_if_no_common_columns(
    mock_inspectors, mock_select_sync_column
) -> None:
    """
    Тестирует функцию _sync_data на пропуск синхронизации данных, если нет общих колонок.
    """
    mock_test_inspector, mock_prod_inspector = mock_inspectors

    # Мокируем _select_sync_column, чтобы он возвращал "id" как ключ синхронизации
    mock_select_sync_column.return_value = ("id",)

    # Схемы таблиц
    test_table = _mk_table("users", ["id", "name"])
    prod_table = _mk_table("users", ["email", "address"])  # Нет общих колонок

    test_schema = SchemaSnapshot.from_tables([test_table])
    prod_schema = SchemaSnapshot.from_tables([prod_table])

    _sync_data(test_schema, prod_schema, mock_test_inspector, mock_prod_inspector)

    # Проверяем, что _select_sync_column был вызван
    mock_select_sync_column.assert_called_once()
    # Проверяем, что fetch_rows, insert_rows и update_rows не были вызваны
    mock_test_inspector.fetch_rows.assert_not_called()
    mock_prod_inspector.insert_rows.assert_not_called()
    mock_prod_inspector.update_rows.assert_not_called()


def test_select_sync_column_returns_none_if_no_common_columns(mock_inspectors) -> None:
    """
    Тестирует _select_sync_column на возврат None, если нет общих колонок.
    """
    mock_test_inspector, mock_prod_inspector = mock_inspectors

    test_table = _mk_table("users", ["id", "name"])
    prod_table = _mk_table("users", ["email", "address"])

    result = _select_sync_column(
        "users", test_table, prod_table, mock_test_inspector, mock_prod_inspector
    )
    assert result is None


def test_select_sync_column_returns_prod_pk_if_no_unique_common_columns(
    mock_inspectors, monkeypatch
) -> None:
    """
    Тестирует _select_sync_column на возврат PK продуктовой базы, если нет уникальных общих колонок.
    """
    mock_test_inspector, mock_prod_inspector = mock_inspectors

    test_table = _mk_table("users", ["id", "name"])
    prod_table = TableSchema(
        name="users",
        columns=(
            ColumnSchema(name="id", column_type="text", is_nullable=True, default=None),
            ColumnSchema(
                name="name", column_type="text", is_nullable=True, default=None
            ),
        ),
        primary_key=("id",),
    )  # prod_table имеет PK "id"

    # Мокируем is_column_unique, чтобы ни одна колонка не была уникальной
    mock_test_inspector.is_column_unique.return_value = False
    mock_prod_inspector.is_column_unique.return_value = False

    # Мокируем input, чтобы не было интерактивного ввода
    monkeypatch.setattr("builtins.input", lambda _: "n")

    result = _select_sync_column(
        "users", test_table, prod_table, mock_test_inspector, mock_prod_inspector
    )
    assert result == ("id",)


def test_select_sync_column_user_selects_column(mock_inspectors, monkeypatch) -> None:
    """
    Тестирует _select_sync_column на корректный выбор колонки пользователем.
    """
    mock_test_inspector, mock_prod_inspector = mock_inspectors

    test_table = _mk_table("users", ["id", "name"])
    prod_table = _mk_table("users", ["id", "name"])

    mock_test_inspector.is_column_unique.return_value = True
    mock_prod_inspector.is_column_unique.return_value = True

    # Мокируем input, чтобы пользователь выбрал первую колонку ("id")
    monkeypatch.setattr("builtins.input", lambda _: "1")

    result = _select_sync_column(
        "users", test_table, prod_table, mock_test_inspector, mock_prod_inspector
    )
    assert result == ("id",)


def test_select_sync_column_user_selects_existing_pk(
    mock_inspectors, monkeypatch
) -> None:
    """
    Тестирует _select_sync_column на корректный выбор существующего PK пользователем.
    """
    mock_test_inspector, mock_prod_inspector = mock_inspectors

    test_table = _mk_table("users", ["id", "name"])
    prod_table = TableSchema(
        name="users",
        columns=(
            ColumnSchema(name="id", column_type="text", is_nullable=True, default=None),
            ColumnSchema(
                name="name", column_type="text", is_nullable=True, default=None
            ),
        ),
        primary_key=("id",),
    )

    mock_test_inspector.is_column_unique.return_value = True
    mock_prod_inspector.is_column_unique.return_value = True

    # Мокируем input, чтобы пользователь выбрал существующий PK ('p')
    monkeypatch.setattr("builtins.input", lambda _: "p")

    result = _select_sync_column(
        "users", test_table, prod_table, mock_test_inspector, mock_prod_inspector
    )
    assert result == ("id",)
