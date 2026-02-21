"""
Основная логика для сравнения и синхронизации кластеров PostgreSQL.

Этот модуль содержит функции для вычисления различий между схемами баз данных,
интерактивного взаимодействия с пользователем для применения изменений схемы
и синхронизации данных между тестовой и продуктовой базами данных.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass

from loguru import logger
from rich.console import Console

from .config import SyncConfig, mask_dsn
from .database import (
    ColumnSchema,
    ForeignKeySchema,
    PostgresInspector,
    SchemaSnapshot,
    TableSchema,
)
from .decisions import AutoDecider, Decider, InteractiveDecider, SyncAbortedError


@dataclass
class SchemaDiff:
    """
    Представляет различия между двумя схемами баз данных.

    Атрибуты:
        new_tables (list[TableSchema]): Таблицы, которые нужно добавить в продовую базу.
        missing_tables (list[TableSchema]): Таблицы, которые есть в продовой базе, но отсутствуют в тестовой.
        orphan_columns (dict[str, list[str]]): "Лишние" колонки в продовой схеме.
        missing_columns (dict[str, list[ColumnSchema]]): Колонки, отсутствующие в продовой схеме.
        missing_foreign_keys (dict[str, list[ForeignKeySchema]]): Внешние ключи, которых нет в продовой базе.
    """

    new_tables: list[TableSchema]
    missing_tables: list[TableSchema]
    orphan_columns: dict[str, list[str]]
    missing_columns: dict[str, list[ColumnSchema]]
    missing_foreign_keys: dict[str, list[ForeignKeySchema]]


def _fk_signature(fk: ForeignKeySchema) -> tuple[tuple[str, ...], str, tuple[str, ...]]:
    """
    Подпись внешнего ключа для сравнения.
    """
    return fk.columns, fk.referenced_table, fk.referenced_columns


def _compute_diff(
    test_schema: SchemaSnapshot, prod_schema: SchemaSnapshot
) -> SchemaDiff:
    """
    Вычисляет различия между тестовой и продуктовой схемами баз данных.

    Сравнивает таблицы и колонки, выявляя отсутствующие или "лишние" элементы.

    Аргументы:
        test_schema (SchemaSnapshot): Снимок схемы тестовой базы данных.
        prod_schema (SchemaSnapshot): Снимок схемы продуктовой базы данных.

    Возвращает:
        SchemaDiff: Объект, содержащий все выявленные различия схемы.
    """
    logger.debug("Вычисление различий схемы между тестовой и продуктовой базами.")
    new_tables: list[TableSchema] = []
    missing_tables: list[TableSchema] = []
    orphan_columns: dict[str, list[str]] = {}
    missing_columns: dict[str, list[ColumnSchema]] = {}
    missing_foreign_keys: dict[str, list[ForeignKeySchema]] = {}

    # Проверяем таблицы в тестовой схеме на наличие в продуктовой.
    for table in test_schema.tables.values():
        if table.name not in prod_schema.tables:
            if table.foreign_keys:
                missing_foreign_keys.setdefault(table.name, []).extend(
                    table.foreign_keys
                )
            logger.info("Обнаружена новая таблица в тестовой схеме", table=table.name)
            new_tables.append(table)
            continue
        prod_table = prod_schema.tables[table.name]
        prod_col_names = {col.name for col in prod_table.columns}
        test_col_names = {col.name for col in table.columns}

        # Ищем "лишние" колонки в продуктовой таблице.
        extra = prod_col_names - test_col_names
        if extra:
            logger.info(
                "Обнаружены лишние колонки в продуктовой таблице",
                table=table.name,
                columns=sorted(extra),
            )
            orphan_columns[table.name] = sorted(extra)
        # Ищем отсутствующие колонки в продуктовой таблице.
        missing = [col for col in table.columns if col.name not in prod_col_names]
        if missing:
            logger.info(
                "Обнаружены отсутствующие колонки в продуктовой таблице",
                table=table.name,
                columns=[c.name for c in missing],
            )
            missing_columns.setdefault(table.name, []).extend(missing)

        prod_fk_signatures = {_fk_signature(fk): fk for fk in prod_table.foreign_keys}
        for fk in table.foreign_keys:
            signature = _fk_signature(fk)
            if signature not in prod_fk_signatures:
                logger.info(
                    "Обнаружен отсутствующий внешний ключ в продуктовой базе",
                    table=table.name,
                    constraint=fk.constraint_name,
                )
                missing_foreign_keys.setdefault(table.name, []).append(fk)

    # Проверяем таблицы в продуктовой схеме на наличие в тестовой.
    for table in prod_schema.tables.values():
        if table.name not in test_schema.tables:
            logger.info(
                "Обнаружена таблица только в продуктовой схеме", table=table.name
            )
            missing_tables.append(table)

    diff = SchemaDiff(
        new_tables,
        missing_tables,
        orphan_columns,
        missing_columns,
        missing_foreign_keys,
    )
    logger.debug("Различия схемы вычислены.", diff=diff)
    return diff


def _dependency_order(schema: SchemaSnapshot) -> list[str]:
    """
    Порядок таблиц, в котором родительские таблицы идут раньше ссылающихся на них.

    Нужен, чтобы при вставке данных не нарушались внешние ключи. Таблицы из циклов
    и самоссылки не блокируют обход: такие связи пропускаются.
    """
    order: list[str] = []
    state: dict[str, str] = {}

    def visit(name: str) -> None:
        if state.get(name) is not None:
            return
        state[name] = "visiting"
        for fk in schema.tables[name].foreign_keys:
            parent = fk.referenced_table
            if parent in schema.tables and state.get(parent) is None:
                visit(parent)
        state[name] = "done"
        order.append(name)

    for name in sorted(schema.tables):
        visit(name)
    return order


def _select_sync_column(
    table_name: str,
    test_table: TableSchema,
    prod_table: TableSchema,
    test_inspector: PostgresInspector,
    prod_inspector: PostgresInspector,
    decider: Decider | None = None,
) -> tuple[str, ...] | None:
    """
    Выбирает столбец (или комбинацию столбцов) для синхронизации данных между
    тестовой и продуктовой таблицами; выбор среди кандидатов делает `decider`.

    Приоритет отдается существующим первичным ключам и уникальным колонкам.

    Аргументы:
        table_name (str): Имя таблицы, для которой выбирается ключ синхронизации.
        test_table (TableSchema): Схема таблицы в тестовой базе.
        prod_table (TableSchema): Схема таблицы в продуктовой базе.
        test_inspector (PostgresInspector): Инспектор для тестовой базы данных.
        prod_inspector (PostgresInspector): Инспектор для продуктовой базы данных.

    Возвращает:
        tuple[str, ...] | None: Кортеж имен колонок, выбранных в качестве ключа синхронизации,
                                или None, если подходящий ключ не найден или не выбран.
    """
    logger.debug("Выбор ключа синхронизации для таблицы", table=table_name)
    test_cols = {col.name for col in test_table.columns}
    prod_cols = {col.name for col in prod_table.columns}
    common_cols = sorted(test_cols & prod_cols)

    if not common_cols:
        logger.warning(
            "Нет общих колонок для выбора ключа синхронизации.", table=table_name
        )
        return None

    potential_keys = []
    for col in common_cols:
        # Проверяем уникальность колонки в обеих таблицах, чтобы она могла быть ключом синхронизации.
        if test_inspector.is_column_unique(
            table_name, col
        ) and prod_inspector.is_column_unique(table_name, col):
            potential_keys.append(col)

    if not potential_keys:
        # Если нет уникальных общих колонок, но есть PK в prod, предлагаем его.
        if prod_table.primary_key:
            logger.info(
                "Нет уникальных общих колонок, предлагаю использовать существующий PK prod.",
                table=table_name,
                pk=prod_table.primary_key,
            )
            return prod_table.primary_key
        logger.warning(
            "Не найдено подходящих уникальных колонок или PK для синхронизации.",
            table=table_name,
        )
        return None

    choice = (decider or InteractiveDecider()).choose_sync_key(
        table_name, potential_keys, prod_table.primary_key
    )
    logger.info("Выбран ключ синхронизации.", table=table_name, sync_key=choice)
    return choice


def _sync_data(
    test_schema: SchemaSnapshot,
    prod_schema: SchemaSnapshot,
    test_inspector: PostgresInspector,
    prod_inspector: PostgresInspector,
    decider: Decider | None = None,
) -> None:
    """
    Синхронизирует данные между тестовой и продуктовой базами данных.

    Для каждой таблицы, присутствующей в обеих схемах, запрашивает у пользователя
    ключ синхронизации, затем вставляет новые строки и обновляет существующие.

    Аргументы:
        test_schema (SchemaSnapshot): Снимок схемы тестовой базы данных.
        prod_schema (SchemaSnapshot): Снимок схемы продуктовой базы данных.
        test_inspector (PostgresInspector): Инспектор для тестовой базы данных.
        prod_inspector (PostgresInspector): Инспектор для продуктовой базы данных.
    """
    logger.info("Начало синхронизации данных.")
    inserted = 0
    updated = 0
    for table_name in _dependency_order(test_schema):
        test_table = test_schema.tables[table_name]
        prod_table = prod_schema.tables.get(table_name)
        if not prod_table:
            logger.debug(
                "Таблица отсутствует в продуктовой базе, пропускаю синхронизацию данных.",
                table=table_name,
            )
            continue

        sync_key = _select_sync_column(
            table_name, test_table, prod_table, test_inspector, prod_inspector, decider
        )

        if not sync_key:
            logger.warning(
                "Пропускаю синхронизацию данных для таблицы: не найден подходящий ключ синхронизации.",
                table=table_name,
            )
            continue

        logger.info(
            "Используется ключ синхронизации для таблицы",
            table=table_name,
            sync_key=sync_key,
        )

        prod_columns = {col.name for col in prod_table.columns}
        shared_columns = [
            col.name for col in test_table.columns if col.name in prod_columns
        ]
        if not shared_columns:
            logger.warning(
                "Нет общих колонок между тестовой и продуктовой таблицами, пропускаю синхронизацию данных.",
                table=table_name,
            )
            continue

        prod_sync_values = prod_inspector.fetch_primary_key_values(table_name, sync_key)

        # Также получаем значения реального Primary Key в prod, чтобы избежать UniqueViolation
        prod_real_pk = prod_table.primary_key
        prod_real_pk_values = set()
        if prod_real_pk:
            prod_real_pk_values = prod_inspector.fetch_primary_key_values(
                table_name, prod_real_pk
            )

        rows = test_inspector.fetch_rows(table_name, shared_columns)
        missing_rows = []
        existing_rows = []
        for row in rows:
            sync_val = tuple(row[col] for col in sync_key)
            if sync_val in prod_sync_values:
                existing_rows.append(row)
                continue

            # Проверка на конфликт по реальному PK
            if prod_real_pk:
                real_pk_val = tuple(row[col] for col in prod_real_pk)
                if real_pk_val in prod_real_pk_values:
                    logger.warning(
                        "Строка с ключом синхронизации отсутствует в prod, "
                        "но ее первичный ключ уже существует. Пропускаю.",
                        table=table_name,
                        sync_key_value=sync_val,
                        primary_key_value=real_pk_val,
                    )
                    continue

            missing_rows.append(row)

        if missing_rows:
            logger.info(
                "Вставка новых строк.", table=table_name, count=len(missing_rows)
            )
            batch_inserted = prod_inspector.insert_rows(
                table_name, shared_columns, missing_rows
            )
            inserted += batch_inserted

        if existing_rows:
            logger.info(
                "Обновление существующих строк.",
                table=table_name,
                count=len(existing_rows),
            )
            batch_updated = prod_inspector.update_rows(
                table_name, sync_key, shared_columns, existing_rows
            )
            updated += batch_updated

    logger.info(
        "Сводка синхронизации данных", rows_inserted=inserted, rows_updated=updated
    )


def print_plan(diff: SchemaDiff, console: Console | None = None) -> None:
    """
    Печатает план изменений схемы prod без их применения.
    """
    console = console or Console()
    lines: list[str] = []
    lines += [f"[green]+ CREATE TABLE[/] {table.name}" for table in diff.new_tables]
    for table_name, columns in diff.missing_columns.items():
        lines += [
            f"[green]+ ADD COLUMN[/] {table_name}.{column.name} {column.column_type}"
            for column in columns
        ]
    for table_name, foreign_keys in diff.missing_foreign_keys.items():
        lines += [
            f"[green]+ ADD FOREIGN KEY[/] {table_name}({', '.join(fk.columns)}) → "
            f"{fk.referenced_table}({', '.join(fk.referenced_columns)})"
            for fk in foreign_keys
        ]
    for table_name, columns in diff.orphan_columns.items():
        lines += [f"[red]- DROP COLUMN?[/] {table_name}.{column}" for column in columns]
    lines += [f"[red]- DROP TABLE?[/] {table.name}" for table in diff.missing_tables]
    if not lines:
        console.print("Схемы совпадают, изменений нет.")
        return
    console.print("План изменений схемы prod:")
    for line in lines:
        console.print(f"  {line}")


def run_sync(
    config: SyncConfig, decider: Decider | None = None, dry_run: bool = False
) -> None:
    """
    Выполняет полный цикл синхронизации схемы и данных между базами данных.

    Включает в себя:
    1. Извлечение схем тестовой и продуктовой баз данных.
    2. Вычисление различий схемы.
    3. Применение изменений схемы (создание/удаление таблиц/колонок, внешние ключи).
    4. Синхронизацию данных между общими таблицами.

    Аргументы:
        config (SyncConfig): Объект конфигурации, содержащий DSN баз данных и уровень логирования.
        decider (Decider | None): Стратегия ответов на вопросы; по умолчанию интерактивная.
        dry_run (bool): Только показать план изменений схемы, ничего не меняя.
    """
    decider = decider or InteractiveDecider()
    logger.info(
        "Запуск синхронизации схемы и данных.",
        test_dsn=mask_dsn(config.test_dsn),
        prod_dsn=mask_dsn(config.prod_dsn),
    )

    test_snap = PostgresInspector(config.test_dsn, decider=decider).fetch_schema()
    prod_snap = PostgresInspector(config.prod_dsn, decider=decider).fetch_schema()

    diff = _compute_diff(test_snap, prod_snap)
    logger.debug("Вычисленные различия схемы", diff=diff)
    if dry_run:
        print_plan(diff)
        return

    inspector = PostgresInspector(config.prod_dsn, decider=decider)
    for table in diff.new_tables:
        logger.info(
            "Создание отсутствующей таблицы в продуктовой базе.", table=table.name
        )
        inspector.create_table(table)

    for table_name, cols in diff.missing_columns.items():
        for column in cols:
            logger.info(
                "Добавление отсутствующей колонки в продуктовой таблице.",
                table=table_name,
                column=column.name,
            )
            inspector.add_column(table_name, column)

    for table_name, columns in diff.orphan_columns.items():
        if decider.confirm_drop([f"{table_name}.{column}" for column in columns]):
            for column in columns:
                logger.info(
                    "Удаление лишней колонки из продуктовой таблицы по запросу пользователя.",
                    table=table_name,
                    column=column,
                )
                inspector.drop_column(table_name, column)
        else:
            logger.warning(
                "Лишние колонки сохранены в продуктовой таблице по запросу пользователя.",
                table=table_name,
                columns=columns,
            )

    for table in diff.missing_tables:
        if decider.confirm_drop([table.name]):
            logger.warning(
                "Удаление таблицы из продуктовой базы по запросу пользователя.",
                table=table.name,
            )
            inspector.drop_table(table.name)
        else:
            logger.warning(
                "Таблица, существующая только в продуктовой базе, сохранена по запросу пользователя.",
                table=table.name,
            )

    for table_name, foreign_keys in diff.missing_foreign_keys.items():
        for fk in foreign_keys:
            logger.info(
                "Добавление отсутствующего внешнего ключа в продуктовую базу.",
                table=table_name,
                constraint=fk.constraint_name,
            )
            inspector.add_foreign_key(table_name, fk)

    # Повторно получаем схему продуктовой базы после применения изменений.
    prod_snap = inspector.fetch_schema()
    test_inspector = PostgresInspector(config.test_dsn, decider=decider)
    _sync_data(test_snap, prod_snap, test_inspector, inspector, decider)

    logger.info("Синхронизация схемы и данных завершена успешно.")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="dbsync",
        description="Синхронизация схемы и данных PostgreSQL: test → prod",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="показать план изменений схемы и выйти"
    )
    parser.add_argument(
        "-y",
        "--yes",
        action="store_true",
        help="не задавать вопросов, решать по политике",
    )
    parser.add_argument(
        "--allow-drop",
        action="store_true",
        help="с --yes: удалять лишние таблицы и колонки в prod",
    )
    parser.add_argument(
        "--not-null-default", help="с --yes: значение для NULL в NOT NULL-колонках prod"
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    """
    Точка входа CLI: читает конфигурацию из окружения и запускает `run_sync`.
    """
    args = parse_args(argv)
    config = SyncConfig.from_env()
    logger.remove()
    logger.add(sys.stderr, level=config.log_level)
    logger.add("dbsync.log", rotation="1 MB", level="DEBUG")
    decider: Decider = (
        AutoDecider(allow_drop=args.allow_drop, not_null_default=args.not_null_default)
        if args.yes
        else InteractiveDecider()
    )
    try:
        run_sync(config, decider=decider, dry_run=args.dry_run)
    except SyncAbortedError as exc:
        logger.error(str(exc))
        sys.exit(2)
