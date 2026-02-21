"""
Скрипт для инициализации тестовой и продуктовой баз данных PostgreSQL
с предопределенными схемами и данными.

Использует Docker Compose для взаимодействия с базами данных,
а также библиотеку rich для форматированного вывода в консоль.
"""

import subprocess
import sys
from time import sleep

from rich.console import Console
from show_db import show_table

console = Console()


def run_sql(container: str, user: str, database: str, sql: str):
    """
    Выполняет SQL-скрипт в указанной базе данных внутри Docker-контейнера.

    Аргументы:
        container (str): Имя Docker-контейнера базы данных (например, 'test-db').
        user (str): Имя пользователя PostgreSQL для подключения.
        database (str): Имя базы данных PostgreSQL.
        sql (str): SQL-сккрипт для выполнения.

    Исключения:
        SystemExit: Если выполнение SQL-скрипта завершается ошибкой.
    """
    try:
        subprocess.run(
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
            ],
            input=sql.encode("utf-8"),
            check=True,
            capture_output=True,
        )
        console.print(
            f"[green][+] База данных '{container}' успешно подготовлена.[/green]"
        )
    except subprocess.CalledProcessError as e:
        console.print(
            f"[red][!] Ошибка при подготовке базы данных '{container}': {e.stderr.decode()}[/red]"
        )
        sys.exit(1)


def reset_schema(container: str, user: str, database: str):
    """
    Обнуляет схему public для базы данных, чтобы начать с чистого состояния.
    """
    reset_sql = """
    DROP SCHEMA public CASCADE;
    CREATE SCHEMA public;
    GRANT ALL ON SCHEMA public TO public;
    GRANT ALL ON SCHEMA public TO %s;
    """
    run_sql(container, user, database, reset_sql % user)


def main():
    """
    Основная функция скрипта для заполнения баз данных.

    Выполняет следующие шаги:
    1. Обнуляет схемы test и prod.
    2. Подготавливает тестовую базу данных с таблицами `users`, `products`, `orders`, `test_only_logs`.
    3. Подготавливает продуктовую базу данных с таблицами `users`, `products`, `orders`, `prod_legacy_archive`.
    4. Вставляет тестовые данные в обе базы (в тестовой - с внешними ключами).
    5. Выводит текущее состояние всех таблиц для визуальной проверки.
    """
    console.print(
        "[bold cyan]Сброс схемы и подготовка тестовой базы данных (TEST DB)[/bold cyan]"
    )
    reset_schema("test-db", "test_user", "test")
    test_sql = """
    -- Создание таблицы пользователей в тестовой базе
    CREATE TABLE public.users (
        id SERIAL PRIMARY KEY,
        username TEXT NOT NULL,
        email TEXT UNIQUE,
        is_active BOOLEAN DEFAULT TRUE
    );

    -- Создание таблицы продуктов в тестовой базе
    CREATE TABLE public.products (
        id SERIAL PRIMARY KEY,
        title TEXT NOT NULL,
        sku TEXT,
        price NUMERIC(10, 2)
    );

    -- Создание таблицы заказов с внешними ключами
    CREATE TABLE public.orders (
        id SERIAL PRIMARY KEY,
        user_id INTEGER NOT NULL,
        product_id INTEGER,
        quantity INTEGER DEFAULT 1,
        CONSTRAINT fk_orders_user FOREIGN KEY (user_id) REFERENCES public.users(id),
        CONSTRAINT fk_orders_product FOREIGN KEY (product_id) REFERENCES public.products(id)
    );

    -- Создание таблицы логов, специфичной для тестовой среды
    CREATE TABLE public.test_only_logs (
        id SERIAL PRIMARY KEY,
        message TEXT,
        created_at TIMESTAMP DEFAULT NOW()
    );

    -- Вставка тестовых данных в таблицу пользователей
    INSERT INTO public.users (username, email) VALUES
        ('admin', 'admin@test.com'),
        ('developer', 'dev@test.com'),
        ('tester', 'test@test.com');

    -- Вставка тестовых данных в таблицу продуктов
    INSERT INTO public.products (title, sku, price) VALUES
        ('Laptop Pro', 'LPT-001', 1500.00),
        ('Mechanical Keyboard', 'KBD-42', 120.50);

    -- Вставка тестовых данных в таблицу заказов
    INSERT INTO public.orders (user_id, product_id, quantity) VALUES
        (1, 1, 1),
        (2, 2, 2);

    -- Вставка тестовых данных в таблицу логов
    INSERT INTO public.test_only_logs (message) VALUES
        ('Database seeded'),
        ('Test log entry');
    """
    run_sql("test-db", "test_user", "test", test_sql)

    console.print(
        "\n[bold cyan]Сброс схемы и подготовка продуктовой базы данных (PROD DB)[/bold cyan]"
    )
    reset_schema("prod-db", "prod_user", "prod")
    prod_sql = """
    -- Создание таблицы пользователей в продуктовой базе
    CREATE TABLE public.users (
        id SERIAL PRIMARY KEY,
        username TEXT NOT NULL,
        phone TEXT
    );

    -- Создание таблицы продуктов в продуктовой базе
    CREATE TABLE public.products (
        id SERIAL PRIMARY KEY,
        title TEXT NOT NULL,
        stock_count INTEGER DEFAULT 0
    );

    -- Создание таблицы заказов (без внешних ключей, чтобы смоделировать различие)
    CREATE TABLE public.orders (
        id SERIAL PRIMARY KEY,
        user_id INTEGER NOT NULL,
        product_id INTEGER,
        status TEXT DEFAULT 'new'
    );

    -- Создание таблицы для архивных данных, специфичной для продуктовой среды
    CREATE TABLE public.prod_legacy_archive (
        id SERIAL PRIMARY KEY,
        old_data TEXT,
        archived_at DATE DEFAULT CURRENT_DATE
    );

    -- Вставка данных пользователей
    INSERT INTO public.users (id, username, phone) VALUES
        (1, 'admin', '+79991234567'),
        (5, 'old_manager', '+70001112233');

    -- Вставка данных продуктов
    INSERT INTO public.products (title, stock_count) VALUES
        ('Laptop Pro', 5),
        ('Old Mouse', 100);

    -- Вставка данных заказов без FK
    INSERT INTO public.orders (user_id, product_id, status) VALUES
        (1, 1, 'completed'),
        (5, 2, 'pending');

    -- Вставка тестовых данных в таблицу архивных данных
    INSERT INTO public.prod_legacy_archive (old_data) VALUES
        ('Legacy record 2023');
    """
    run_sql("prod-db", "prod_user", "prod", prod_sql)

    sleep(1)  # Небольшая задержка для стабильности вывода
    console.print(
        "\n[bold yellow]Текущее состояние таблиц после заполнения:[/bold yellow]"
    )
    show_table("test-db", "test_user", "test", "users")
    show_table("test-db", "test_user", "test", "products")
    show_table("test-db", "test_user", "test", "orders")
    show_table("test-db", "test_user", "test", "test_only_logs")
    show_table("prod-db", "prod_user", "prod", "users")
    show_table("prod-db", "prod_user", "prod", "products")
    show_table("prod-db", "prod_user", "prod", "orders")
    show_table("prod-db", "prod_user", "prod", "prod_legacy_archive")

    console.print(
        "\n[bold green]Готово! Обе базы сброшены и наполнены данными (в тестовой — с внешним ключом в orders).[/bold green]"
    )


if __name__ == "__main__":
    main()
