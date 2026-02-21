"""
Скрипт для отображения содержимого таблиц PostgreSQL в консоли.

Использует Docker Compose для выполнения команд psql внутри контейнеров
и библиотеку rich для красивого форматированного вывода таблиц.
"""

import subprocess

from rich.console import Console
from rich.table import Table

console = Console()


def show_table(container: str, user: str, database: str, table_name: str):
    """
    Выводит содержимое указанной таблицы из базы данных в форматированном виде.

    Функция сначала извлекает имена колонок, а затем данные таблицы,
    используя `psql` внутри Docker-контейнера. Результат отображается
    с помощью `rich.table.Table`.

    Аргументы:
        container (str): Имя Docker-контейнера базы данных (например, 'test-db').
        user (str): Имя пользователя PostgreSQL для подключения.
        database (str): Имя базы данных PostgreSQL.
        table_name (str): Имя таблицы, содержимое которой необходимо отобразить.
    """
    try:
        # 1. Получаем имена колонок таблицы.
        # Используем psql с флагами -A (unaligned), -F | (field separator),
        # и -c "SELECT * FROM {table_name} LIMIT 0" для получения только заголовков.
        # Флаг -t (tuples only) НЕ используется, чтобы получить строку с заголовками.
        col_result = subprocess.run(
            [
                "docker",
                "compose",
                "-f",
                "docker-compose.test.yml",
                "exec",
                "-T",
                container,
                "psql",
                "-U",
                user,
                "-d",
                database,
                "-A",
                "-F",
                "|",
                "-c",
                f"SELECT * FROM {table_name} LIMIT 0",
            ],
            capture_output=True,
            text=True,
            check=True,
        )

        # Ожидаемый вывод psql без -t:
        # header1|header2|...
        # (0 rows)
        lines = col_result.stdout.strip().splitlines()

        if (
            not lines or "(0 rows)" in lines[0]
        ):  # Проверяем, что есть заголовки и не только "(0 rows)"
            console.print(
                f"[yellow]Таблица '{table_name}' в базе '{database}' не содержит данных или заголовков.[/yellow]"
            )
            return

        # Первая строка содержит заголовки колонок, разделенные '|'.
        columns = lines[0].split("|")

        # 2. Получаем данные таблицы.
        # Здесь флаг -t (tuples only) используется, чтобы получить только строки данных
        # без заголовков и футеров psql.
        data_result = subprocess.run(
            [
                "docker",
                "compose",
                "-f",
                "docker-compose.test.yml",
                "exec",
                "-T",
                container,
                "psql",
                "-U",
                user,
                "-d",
                database,
                "-A",
                "-F",
                "|",
                "-t",
                "-c",
                f"SELECT * FROM {table_name}",
            ],
            capture_output=True,
            text=True,
            check=True,
        )

        rows = data_result.stdout.strip().splitlines()

        # Создаем объект rich.table.Table для форматированного вывода.
        table = Table(
            title=f"База: [bold blue]{database}[/bold blue] -> Таблица: [bold green]{table_name}[/bold green]"
        )
        for col in columns:
            table.add_column(col)

        for row in rows:
            if row:  # Проверка на пустую строку
                # Добавляем строку в таблицу, заменяя пустые значения на пустые строки для rich.
                table.add_row(*[cell if cell else "" for cell in row.split("|")])

        console.print(table)

    except subprocess.CalledProcessError as e:
        # Обработка ошибок, например, если таблица не существует.
        console.print(
            f"[red]Ошибка при выводе таблицы '{table_name}' из базы '{database}' (возможно, она не существует или нет доступа): {e.stderr.decode().strip()}[/red]"
        )
    except Exception as e:
        console.print(
            f"[red]Непредвиденная ошибка при обработке таблицы '{table_name}': {e}[/red]"
        )


def get_tables(container: str, user: str, database: str) -> list[str]:
    """
    Динамически получает список всех таблиц в схеме 'public' указанной базы данных.

    Аргументы:
        container (str): Имя Docker-контейнера базы данных.
        user (str): Имя пользователя PostgreSQL.
        database (str): Имя базы данных PostgreSQL.

    Возвращает:
        list[str]: Список имен таблиц. Возвращает пустой список в случае ошибки.
    """
    try:
        result = subprocess.run(
            [
                "docker",
                "compose",
                "-f",
                "docker-compose.test.yml",
                "exec",
                "-T",
                container,
                "psql",
                "-U",
                user,
                "-d",
                database,
                "-t",
                "-A",
                "-c",
                "SELECT tablename FROM pg_catalog.pg_tables WHERE schemaname = 'public';",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        # Фильтруем пустые строки и возвращаем очищенные имена таблиц.
        return [
            line.strip() for line in result.stdout.strip().splitlines() if line.strip()
        ]
    except subprocess.CalledProcessError as e:
        console.print(
            f"[red]Ошибка при получении списка таблиц для базы '{database}': {e.stderr.decode().strip()}[/red]"
        )
        return []
    except Exception as e:
        console.print(
            f"[red]Непредвиденная ошибка при получении списка таблиц для базы '{database}': {e}[/red]"
        )
        return []


def main():
    """
    Основная функция скрипта `show_db.py`.

    Отображает текущее состояние таблиц в тестовой и продуктовой базах данных,
    используя функцию `show_table`.
    """
    console.print(
        "\n[bold yellow]Текущее состояние таблиц в базах данных:[/bold yellow]"
    )

    # Конфигурация для подключения к тестовой и продуктовой базам.
    databases = [
        {"container": "test-db", "user": "test_user", "db": "test"},
        {"container": "prod-db", "user": "prod_user", "db": "prod"},
    ]

    for db_info in databases:
        container = db_info["container"]
        user = db_info["user"]
        db_name = db_info["db"]

        console.print(f"\n[bold magenta]--- База данных: {db_name} ---[/bold magenta]")
        tables = get_tables(container, user, db_name)
        if not tables:
            console.print(f"[yellow]В базе '{db_name}' не найдено таблиц.[/yellow]")
            continue

        for table in sorted(tables):
            show_table(container, user, db_name, table)


if __name__ == "__main__":
    main()
