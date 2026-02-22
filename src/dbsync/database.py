"""
Модуль для взаимодействия с базами данных PostgreSQL, предоставляющий утилиты
для интроспекции схемы, выполнения DDL-операций и манипуляции данными.

Он включает в себя классы для представления схемы базы данных (таблиц, колонок)
и `PostgresInspector` для выполнения операций над базой данных.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import psycopg
from loguru import logger
from psycopg import sql
from psycopg.rows import dict_row

from .decisions import Decider, InteractiveDecider


@dataclass(frozen=True)
class ColumnSchema:
    """
    Представляет схему отдельной колонки в таблице PostgreSQL.

    Атрибуты:
        name (str): Имя колонки.
        column_type (str): Тип данных колонки (например, 'TEXT', 'INTEGER', 'TIMESTAMP').
        is_nullable (bool): Указывает, может ли колонка содержать NULL значения.
        default (str | None): Выражение по умолчанию для колонки, если оно задано.
    """

    name: str
    column_type: str
    is_nullable: bool
    default: str | None


@dataclass(frozen=True)
class TableSchema:
    """
    Представляет схему отдельной таблицы в базе данных PostgreSQL.

    Атрибуты:
        name (str): Имя таблицы.
        columns (tuple[ColumnSchema, ...]): Кортеж объектов `ColumnSchema`,
                                            описывающих колонки таблицы.
        primary_key (tuple[str, ...]): Кортеж имен колонок, составляющих первичный ключ таблицы.
        foreign_keys (tuple[ForeignKeySchema, ...]): Кортеж внешних ключей в таблице.
    """

    name: str
    columns: tuple[ColumnSchema, ...]
    primary_key: tuple[str, ...]
    foreign_keys: tuple[ForeignKeySchema, ...] = ()


@dataclass(frozen=True)
class ForeignKeySchema:
    """Представляет внешнюю ссылку между таблицами PostgreSQL."""

    constraint_name: str
    columns: tuple[str, ...]
    referenced_table: str
    referenced_columns: tuple[str, ...]
    on_update: str | None
    on_delete: str | None
    match_option: str | None


@dataclass(frozen=True)
class SchemaSnapshot:
    """
        Представляет снимок (snapshot) схемы базы данных, содержащий информацию обо всех таблицах.

        Атрибуты:
    tables (Mapping[str, TableSchema]): Словарь, где ключом является имя таблицы,
                                                а значением — объект `TableSchema`.
    """

    tables: Mapping[str, TableSchema]

    @classmethod
    def from_tables(cls, tables: Iterable[TableSchema]) -> SchemaSnapshot:
        """
        Создает `SchemaSnapshot` из итерируемого объекта `TableSchema`.

        Аргументы:
            tables (Iterable[TableSchema]): Итерируемый объект, содержащий схемы таблиц.

        Возвращает:
            SchemaSnapshot: Новый объект снимка схемы.
        """
        return cls({table.name: table for table in tables})


# SQL-запрос для получения информации о колонках всех таблиц в схеме 'public'.
# Извлекает имя таблицы, имя колонки, тип данных, признак nullable и выражение по умолчанию.
COLUMNS_QUERY = """
SELECT c.relname AS table_name,
       a.attname AS column_name,
       format_type(a.atttypid, a.atttypmod) AS column_type,
       NOT a.attnotnull AS is_nullable,
       pg_get_expr(ad.adbin, ad.adrelid) AS default_expression
FROM pg_attribute a
JOIN pg_class c ON c.oid = a.attrelid
JOIN pg_namespace n ON n.oid = c.relnamespace
LEFT JOIN pg_attrdef ad ON ad.adrelid = a.attrelid AND ad.adnum = a.attnum
WHERE n.nspname = 'public'
  AND c.relkind = 'r'
  AND a.attnum > 0
  AND NOT a.attisdropped
ORDER BY c.relname, a.attnum;
"""


FOREIGN_KEYS_QUERY = """
SELECT
    rel.relname AS table_name,
    con.conname AS constraint_name,
    col.attname AS column_name,
    conf.relname AS referenced_table,
    conf_col.attname AS referenced_column,
    rc.update_rule,
    rc.delete_rule,
    rc.match_option,
    cols.ordinality AS column_position
FROM pg_constraint con
JOIN pg_class rel ON rel.oid = con.conrelid
JOIN pg_namespace nsp ON nsp.oid = con.connamespace
JOIN LATERAL unnest(con.conkey) WITH ORDINALITY AS cols(attnum, ordinality) ON TRUE
JOIN pg_attribute col ON col.attrelid = con.conrelid AND col.attnum = cols.attnum
JOIN pg_class conf ON conf.oid = con.confrelid
JOIN LATERAL unnest(con.confkey) WITH ORDINALITY AS conf_cols(attnum, ordinality) ON conf_cols.ordinality = cols.ordinality
JOIN pg_attribute conf_col ON conf_col.attrelid = con.confrelid AND conf_col.attnum = conf_cols.attnum
LEFT JOIN information_schema.referential_constraints rc
    ON rc.constraint_name = con.conname
    AND rc.constraint_schema = nsp.nspname
WHERE con.contype = 'f'
  AND nsp.nspname = 'public'
ORDER BY rel.relname, con.conname, cols.ordinality;
"""

# SQL-запрос для получения информации о первичном ключе для всех таблиц в схеме 'public'.
# Извлекает имя таблицы, имя колонки, входящей в первичный ключ, и ее позицию.
PK_QUERY = """
SELECT tc.table_name,
       kcu.column_name,
       kcu.ordinal_position
FROM information_schema.table_constraints tc
JOIN information_schema.key_column_usage kcu
  ON tc.constraint_name = kcu.constraint_name
  AND tc.table_schema = kcu.table_schema
WHERE tc.table_schema = 'public'
  AND tc.constraint_type = 'PRIMARY KEY'
ORDER BY tc.table_name, kcu.ordinal_position;
"""


class PostgresInspector:
    """
    Класс для интроспекции и модификации схемы и данных в базе данных PostgreSQL.

    Предоставляет методы для получения снимков схемы, создания/удаления таблиц и колонок,
    а также для вставки и обновления данных.
    """

    def __init__(self, dsn: str, decider: Decider | None = None) -> None:
        """
        Инициализирует `PostgresInspector` с заданной строкой подключения.

        Аргументы:
            dsn (str): Строка подключения (DSN) к базе данных PostgreSQL.
            decider (Decider | None): Кто решает, что делать при нарушении NOT NULL.
                                      По умолчанию вопрос задаётся в терминале.
        """
        self._dsn = dsn
        self._decider = decider or InteractiveDecider()
        # Словарь для хранения решений пользователя по обработке NOT NULL конфликтов.
        # Ключ: (имя_таблицы, имя_колонки), Значение: ("drop" | "default", значение_по_умолчанию | None)
        self._not_null_decisions: dict[tuple[str, str], tuple[str, str | None]] = {}

    def fetch_schema(self) -> SchemaSnapshot:
        """
        Извлекает полную схему базы данных (таблицы, колонки, первичные ключи)
        из схемы 'public'.

        Возвращает:
            SchemaSnapshot: Объект, представляющий текущий снимок схемы базы данных.
        """
        columns_by_table: dict[str, list[ColumnSchema]] = {}
        # Устанавливаем row_factory в dict_row для получения результатов в виде словарей.
        with psycopg.connect(self._dsn, row_factory=dict_row) as conn:
            with conn.cursor() as cur:
                # Выполняем запрос для получения информации о колонках.
                cur.execute(COLUMNS_QUERY)
                for row in cur:
                    # Группируем колонки по имени таблицы.
                    columns_by_table.setdefault(row["table_name"], []).append(
                        ColumnSchema(
                            name=row["column_name"],
                            column_type=row["column_type"],
                            is_nullable=bool(row["is_nullable"]),
                            default=row["default_expression"],
                        )
                    )
                # Выполняем запрос для получения информации о первичных ключах.
                cur.execute(PK_QUERY)
                primary_keys: dict[str, list[str]] = {}
                for row in cur:
                    # Группируем колонки первичного ключа по имени таблицы.
                    primary_keys.setdefault(row["table_name"], []).append(
                        row["column_name"]
                    )
                cur.execute(FOREIGN_KEYS_QUERY)
                foreign_key_rows = list(cur)

        foreign_keys = _group_foreign_keys(foreign_key_rows)
        tables = [
            TableSchema(
                name=name,
                columns=tuple(cols),
                primary_key=tuple(primary_keys.get(name, [])),
                foreign_keys=tuple(foreign_keys.get(name, [])),
            )
            for name, cols in columns_by_table.items()
        ]
        return SchemaSnapshot.from_tables(tables)

    def add_foreign_key(self, table_name: str, fk: ForeignKeySchema) -> None:
        """
        Создаёт внешний ключ в таблице с теми же правилами ON UPDATE/ON DELETE и MATCH,
        что и в исходной базе.

        Аргументы:
            table_name (str): Таблица, в которую добавляется ограничение.
            fk (ForeignKeySchema): Описание внешнего ключа.
        """
        statement = sql.SQL(
            "ALTER TABLE {} ADD CONSTRAINT {} FOREIGN KEY ({}) REFERENCES {} ({}){} "
            "ON UPDATE {} ON DELETE {}"
        ).format(
            sql.Identifier(table_name),
            sql.Identifier(fk.constraint_name),
            sql.SQL(", ").join(sql.Identifier(col) for col in fk.columns),
            sql.Identifier(fk.referenced_table),
            sql.SQL(", ").join(sql.Identifier(col) for col in fk.referenced_columns),
            sql.SQL(" MATCH FULL" if fk.match_option == "FULL" else ""),
            sql.SQL(_referential_action(fk.on_update)),
            sql.SQL(_referential_action(fk.on_delete)),
        )
        self._execute(statement)

    def list_tables(self) -> list[str]:
        """
        Возвращает список имен всех таблиц в схеме 'public'.

        Возвращает:
            list[str]: Список имен таблиц.
        """
        query = (
            "SELECT tablename FROM pg_catalog.pg_tables WHERE schemaname = 'public';"
        )
        with psycopg.connect(self._dsn) as conn, conn.cursor() as cur:
            cur.execute(query)
            return [row[0] for row in cur.fetchall()]

    def create_table(self, table: TableSchema) -> None:
        """
        Создает новую таблицу в базе данных на основе предоставленной схемы.

        Аргументы:
            table (TableSchema): Объект схемы таблицы, которую необходимо создать.
        """
        # Генерируем определения колонок.
        column_defs = [self._column_definition(col) for col in table.columns]
        if table.primary_key:
            # Добавляем определение первичного ключа, если он задан.
            pk_clause = sql.SQL("PRIMARY KEY ({})").format(
                sql.SQL(", ").join(sql.Identifier(col) for col in table.primary_key)
            )
            column_defs.append(pk_clause)

        # Формируем SQL-оператор CREATE TABLE.
        statement = sql.SQL("CREATE TABLE {} ({})").format(
            sql.Identifier(table.name), sql.SQL(", ").join(column_defs)
        )
        self._execute(statement)

    def add_column(self, table_name: str, column: ColumnSchema) -> None:
        """
        Добавляет новую колонку в существующую таблицу.

        Если таблица содержит данные, колонка добавляется как NULLABLE, чтобы не нарушить
        существующие строки. Если задано значение по умолчанию, оно также применяется.

        Аргументы:
            table_name (str): Имя таблицы, в которую добавляется колонка.
            column (ColumnSchema): Объект схемы колонки, которую необходимо добавить.
        """
        parts = [sql.Identifier(column.name), sql.SQL(column.column_type)]
        if column.default:
            parts.append(sql.SQL("DEFAULT ") + sql.SQL(column.default))
        # Формируем SQL-оператор ALTER TABLE ADD COLUMN.
        statement = sql.SQL("ALTER TABLE {} ADD COLUMN {}").format(
            sql.Identifier(table_name), sql.SQL(" ").join(parts)
        )
        self._execute(statement)

    def drop_table(self, table_name: str) -> None:
        """
        Удаляет таблицу из базы данных. Использует CASCADE для удаления зависимых объектов.

        Аргументы:
            table_name (str): Имя таблицы, которую необходимо удалить.
        """
        statement = sql.SQL("DROP TABLE {} CASCADE").format(sql.Identifier(table_name))
        self._execute(statement)

    def drop_column(self, table_name: str, column_name: str) -> None:
        """
        Удаляет колонку из таблицы. Использует CASCADE для удаления зависимых объектов.

        Аргументы:
            table_name (str): Имя таблицы, из которой удаляется колонка.
            column_name (str): Имя колонки, которую необходимо удалить.
        """
        statement = sql.SQL("ALTER TABLE {} DROP COLUMN {} CASCADE").format(
            sql.Identifier(table_name), sql.Identifier(column_name)
        )
        self._execute(statement)

    def fetch_primary_key_values(
        self, table_name: str, primary_key: Sequence[str]
    ) -> set[tuple[Any, ...]]:
        """
        Извлекает все значения первичного ключа для указанной таблицы.

        Аргументы:
            table_name (str): Имя таблицы.
            primary_key (Sequence[str]): Последовательность имен колонок, составляющих первичный ключ.

        Возвращает:
            set[tuple[Any, ...]]: Множество кортежей, где каждый кортеж представляет
                                  значения первичного ключа одной строки.
                                  Возвращает пустое множество, если первичный ключ не задан.
        """
        if not primary_key:
            return set()

        # Формируем SQL-оператор SELECT для извлечения значений первичного ключа.
        statement = sql.SQL("SELECT {} FROM {};").format(
            sql.SQL(", ").join(sql.Identifier(col) for col in primary_key),
            sql.Identifier(table_name),
        )

        with psycopg.connect(self._dsn, row_factory=dict_row) as conn:
            with conn.cursor() as cur:
                cur.execute(statement)
                return {
                    tuple(row[col] for col in primary_key) for row in cur.fetchall()
                }

    def is_column_unique(self, table_name: str, column_name: str) -> bool:
        """
        Проверяет, являются ли все значения в указанном столбце таблицы уникальными.

        Эта функция полезна для определения потенциальных ключей синхронизации данных.

        Аргументы:
            table_name (str): Имя таблицы.
            column_name (str): Имя колонки для проверки уникальности.

        Возвращает:
            bool: True, если все значения в колонке уникальны и колонка не пуста,
                  иначе False.
        """
        statement = sql.SQL(
            "SELECT COUNT({col}) = COUNT(DISTINCT {col}) AND COUNT({col}) > 0 FROM {table}"
        ).format(col=sql.Identifier(column_name), table=sql.Identifier(table_name))
        with psycopg.connect(self._dsn) as conn, conn.cursor() as cur:
            cur.execute(statement)
            row = cur.fetchone()
            return row[0] if row else False

    def fetch_rows(
        self, table_name: str, columns: Sequence[str]
    ) -> list[Mapping[str, Any]]:
        """
        Извлекает строки из указанной таблицы, выбирая только заданные колонки.

        Аргументы:
            table_name (str): Имя таблицы.
            columns (Sequence[str]): Последовательность имен колонок для извлечения.

        Возвращает:
            list[Mapping[str, Any]]: Список словарей, где каждый словарь представляет
                                     одну строку с парами "имя_колонки": "значение".
                                     Возвращает пустой список, если колонки не заданы.
        """
        if not columns:
            return []

        # Формируем SQL-оператор SELECT для извлечения данных.
        statement = sql.SQL("SELECT {} FROM {};").format(
            sql.SQL(", ").join(sql.Identifier(col) for col in columns),
            sql.Identifier(table_name),
        )

        with psycopg.connect(self._dsn, row_factory=dict_row) as conn:
            with conn.cursor() as cur:
                cur.execute(statement)
                return [dict(row) for row in cur.fetchall()]

    def insert_rows(
        self, table_name: str, columns: Sequence[str], rows: Sequence[Mapping[str, Any]]
    ) -> int:
        """
        Вставляет несколько строк в указанную таблицу.

        Обрабатывает конфликты `NotNullViolation`, интерактивно запрашивая у пользователя
        действие: удалить ограничение NOT NULL или применить значение по умолчанию.

        Аргументы:
            table_name (str): Имя таблицы, в которую вставляются строки.
            columns (Sequence[str]): Последовательность имен колонок для вставки.
            rows (Sequence[Mapping[str, Any]]): Последовательность словарей, где каждый
                                                словарь представляет одну строку для вставки.

        Возвращает:
            int: Количество успешно вставленных строк.
        """
        if not columns or not rows:
            logger.debug(
                "Нет колонок или строк для вставки, пропускаю операцию.",
                table=table_name,
            )
            return 0

        # Формируем SQL-оператор INSERT.
        statement = sql.SQL("INSERT INTO {} ({}) VALUES ({})").format(
            sql.Identifier(table_name),
            sql.SQL(", ").join(sql.Identifier(col) for col in columns),
            sql.SQL(", ").join(sql.Placeholder() for _ in columns),
        )

        params = [tuple(row[col] for col in columns) for row in rows]
        try:
            with psycopg.connect(self._dsn, autocommit=True) as conn:
                with conn.cursor() as cur:
                    cur.executemany(statement, params)
                    logger.info(
                        "Успешно вставлено строк.", table=table_name, count=len(params)
                    )
        except psycopg.errors.NotNullViolation as exc:
            table = exc.diag.table_name or table_name
            column = exc.diag.column_name or ""
            if not column:
                logger.error("NotNullViolation без указания колонки.", exception=exc)
                raise
            logger.warning(
                "Обнаружено нарушение ограничения NOT NULL.",
                table=table,
                column=column,
                error=str(exc),
            )
            action, payload = self._resolve_not_null_decision(table, column)
            if action == "drop":
                logger.warning(
                    "Удаляю NOT NULL-ограничение по запросу пользователя.",
                    table=table,
                    column=column,
                )
                self._drop_not_null_constraint(table, column)
                # Повторяем вставку после изменения схемы.
                return self.insert_rows(table_name, columns, rows)
            updated_rows = self._replace_null_with_default(column, rows, payload)
            logger.info(
                "Применено значение по умолчанию для NULL-значений в колонке.",
                table=table,
                column=column,
                value=payload,
            )
            # Повторяем вставку с обновленными данными.
            return self.insert_rows(table_name, columns, updated_rows)
        except Exception as exc:
            logger.error("Ошибка при вставке строк.", table=table_name, exception=exc)
            raise

        return len(params)

    def update_rows(
        self,
        table_name: str,
        sync_key: Sequence[str],
        columns: Sequence[str],
        rows: Sequence[Mapping[str, Any]],
    ) -> int:
        """
        Обновляет существующие строки в таблице на основе заданного ключа синхронизации.

        Аргументы:
            table_name (str): Имя таблицы, в которой обновляются строки.
            sync_key (Sequence[str]): Последовательность имен колонок, используемых как ключ синхронизации
                                      для идентификации строк.
            columns (Sequence[str]): Последовательность имен колонок, которые могут быть обновлены.
            rows (Sequence[Mapping[str, Any]]): Последовательность словарей, где каждый словарь
                                                представляет одну строку для обновления.

        Возвращает:
            int: Количество успешно обновленных строк.
        """
        if not columns or not rows or not sync_key:
            logger.debug(
                "Нет колонок, строк или ключа синхронизации для обновления, пропускаю операцию.",
                table=table_name,
            )
            return 0

        # Исключаем столбцы, входящие в ключ синхронизации, из SET-части UPDATE-запроса,
        # так как они используются в WHERE-части для идентификации строк.
        update_cols = [col for col in columns if col not in sync_key]
        if not update_cols:
            logger.warning(
                "Нет колонок для обновления, кроме ключа синхронизации.",
                table=table_name,
            )
            return 0

        # Формируем SET-часть SQL-оператора UPDATE.
        set_clause = sql.SQL(", ").join(
            sql.SQL("{} = {}").format(sql.Identifier(col), sql.Placeholder())
            for col in update_cols
        )
        # Формируем WHERE-часть SQL-оператора UPDATE.
        where_clause = sql.SQL(" AND ").join(
            sql.SQL("{} = {}").format(sql.Identifier(col), sql.Placeholder())
            for col in sync_key
        )

        # Формируем полный SQL-оператор UPDATE.
        statement = sql.SQL("UPDATE {} SET {} WHERE {}").format(
            sql.Identifier(table_name), set_clause, where_clause
        )

        params = []
        for row in rows:
            # Параметры для SET-части.
            row_params = [row[col] for col in update_cols]
            # Параметры для WHERE-части.
            row_params.extend([row[col] for col in sync_key])
            params.append(tuple(row_params))

        with psycopg.connect(self._dsn, autocommit=True) as conn:
            with conn.cursor() as cur:
                cur.executemany(statement, params)
                logger.info(
                    "Успешно обновлено строк.", table=table_name, count=len(params)
                )

        return len(params)

    def _drop_not_null_constraint(self, table_name: str, column_name: str) -> None:
        """
        Удаляет ограничение NOT NULL с указанной колонки в таблице.

        Аргументы:
            table_name (str): Имя таблицы.
            column_name (str): Имя колонки, с которой снимается ограничение.
        """
        statement = sql.SQL("ALTER TABLE {} ALTER COLUMN {} DROP NOT NULL").format(
            sql.Identifier(table_name),
            sql.Identifier(column_name),
        )
        self._execute(statement)

    def _resolve_not_null_decision(
        self, table_name: str, column_name: str
    ) -> tuple[str, str | None]:
        """
        Решение по конфликту NOT NULL: удалить ограничение или вставить значение по умолчанию.

        Решение принимает `Decider` и кэшируется на время сессии.

        Аргументы:
            table_name (str): Имя таблицы, где произошел конфликт.
            column_name (str): Имя колонки, вызвавшей конфликт.

        Возвращает:
            tuple[str, str | None]: Кортеж, содержащий выбранное действие ("drop" или "default")
                                    и, если выбрано "default", значение по умолчанию.
        """
        key = (table_name, column_name)
        if key in self._not_null_decisions:
            return self._not_null_decisions[key]

        decision = self._decider.resolve_not_null(table_name, column_name)
        self._not_null_decisions[key] = decision
        return decision

    def _replace_null_with_default(
        self,
        column_name: str,
        rows: Sequence[Mapping[str, Any]],
        value: str | None,
    ) -> list[Mapping[str, Any]]:
        """
        Заменяет NULL-значения в указанной колонке на заданное значение по умолчанию
        для списка строк.

        Аргументы:
            column_name (str): Имя колонки, в которой заменяются NULL-значения.
            rows (Sequence[Mapping[str, Any]]): Список строк (словарей), которые необходимо обработать.
            value (str | None): Значение, на которое будут заменены NULL.

        Возвращает:
            list[Mapping[str, Any]]: Новый список строк с замененными NULL-значениями.
        """
        updated_rows: list[Mapping[str, Any]] = []
        for row in rows:
            if row.get(column_name) is None:
                new_row = dict(row)
                new_row[column_name] = value
                updated_rows.append(new_row)
            else:
                updated_rows.append(row)
        return updated_rows

    def _column_definition(self, column: ColumnSchema) -> sql.SQL:
        """
        Генерирует SQL-определение для колонки на основе ее схемы.

        Особый случай: если колонка является INTEGER и имеет default-выражение с 'nextval',
        она преобразуется в тип SERIAL для упрощения в PostgreSQL.

        Аргументы:
            column (ColumnSchema): Объект схемы колонки.

        Возвращает:
            sql.SQL: Объект SQL, представляющий определение колонки.
        """
        column_type = column.column_type
        default_clause = []

        # Если это целочисленный тип с default-выражением, использующим nextval,
        # преобразуем в SERIAL для упрощения в PostgreSQL.
        if (
            column.default
            and "nextval" in column.default.lower()
            and "integer" in column.column_type.lower()
        ):
            column_type = "SERIAL"
        else:
            # Добавляем NOT NULL, если колонка не nullable.
            if not column.is_nullable:
                default_clause.append(sql.SQL("NOT NULL"))
            # Добавляем DEFAULT-выражение, если оно задано.
            if column.default:
                default_clause.append(sql.SQL("DEFAULT ") + sql.SQL(column.default))

        parts = [sql.Identifier(column.name), sql.SQL(column_type)]
        parts.extend(default_clause)
        return sql.SQL(" ").join(parts)

    def _execute(self, statement: sql.Composed) -> None:
        """
        Выполняет SQL-оператор в базе данных.

        Использует autocommit=True для немедленного применения изменений.
        Логирует выполняемый SQL-запрос на уровне DEBUG.

        Аргументы:
            statement (sql.Composed): Объект SQL-оператора, который необходимо выполнить.
        """
        with psycopg.connect(self._dsn, autocommit=True) as conn:
            # Логируем SQL-запрос для отладки.
            logger.debug("Выполнение SQL-запроса", sql=statement.as_string(conn))
            with conn.cursor() as cur:
                cur.execute(statement)


_REFERENTIAL_ACTIONS = frozenset(
    {"NO ACTION", "RESTRICT", "CASCADE", "SET NULL", "SET DEFAULT"}
)


def _referential_action(rule: str | None) -> str:
    """
    Проверяет правило ON UPDATE/ON DELETE по белому списку перед подстановкой в SQL.
    """
    return rule if rule in _REFERENTIAL_ACTIONS else "NO ACTION"


def _group_foreign_keys(
    rows: Iterable[Mapping[str, Any]],
) -> dict[str, list[ForeignKeySchema]]:
    """
    Собирает строки FOREIGN_KEYS_QUERY (по одной на колонку) в составные внешние ключи.
    """
    grouped: dict[tuple[str, str], list[Mapping[str, Any]]] = {}
    for row in rows:
        grouped.setdefault((row["table_name"], row["constraint_name"]), []).append(row)

    result: dict[str, list[ForeignKeySchema]] = {}
    for (table_name, constraint_name), parts in grouped.items():
        parts.sort(key=lambda part: part["column_position"])
        first = parts[0]
        result.setdefault(table_name, []).append(
            ForeignKeySchema(
                constraint_name=constraint_name,
                columns=tuple(part["column_name"] for part in parts),
                referenced_table=first["referenced_table"],
                referenced_columns=tuple(part["referenced_column"] for part in parts),
                on_update=first["update_rule"],
                on_delete=first["delete_rule"],
                match_option=first["match_option"],
            )
        )
    return result
