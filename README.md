# dbsync

[![CI](https://github.com/mellettan/test_18022026/actions/workflows/ci.yml/badge.svg)](https://github.com/mellettan/test_18022026/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/python-3.11%20|%203.12%20|%203.13-3776AB?logo=python&logoColor=white)
![PostgreSQL](https://img.shields.io/badge/PostgreSQL-psycopg%203-4169E1?logo=postgresql&logoColor=white)

CLI для синхронизации схемы и данных между двумя базами PostgreSQL, например тестовой и продуктовой. Показывает план изменений, переносит недостающие таблицы, колонки и внешние ключи, затем переносит и обновляет строки по выбранному ключу. Работает интерактивно или без вопросов, по заданной политике, — для CI.

```
$ dbsync --dry-run
План изменений схемы prod:
  + CREATE TABLE orders
  + ADD COLUMN users.name text
  + ADD FOREIGN KEY orders(user_id) → users(id)
  - DROP COLUMN? users.legacy_flag
  - DROP TABLE? audit_log
```

## Как работает

```mermaid
flowchart TD
    A[Снимки схем test и prod<br/>pg_catalog: колонки, PK, FK] --> B[Diff]
    B --> C{--dry-run?}
    C -->|да| P[Печать плана]
    C -->|нет| D[Создать таблицы и колонки]
    D --> E{Лишние объекты в prod}
    E -->|подтверждено| F[Удалить]
    E -->|нет| G[Оставить]
    F --> H[Добавить внешние ключи]
    G --> H
    H --> I[Данные: таблицы в порядке зависимостей FK]
    I --> J[Ключ синхронизации:<br/>PK или уникальная колонка]
    J --> K[INSERT новых строк, UPDATE существующих]
```

- **Схема** читается из `pg_catalog`: типы с модификаторами (`numeric(10,2)`, `varchar(255)`), `NOT NULL`, значения по умолчанию, первичные и составные внешние ключи с правилами `ON UPDATE`/`ON DELETE` и `MATCH FULL`.
- **Аддитивные изменения** (новые таблицы, колонки, внешние ключи) применяются всегда. **Разрушающие** (удаление таблиц и колонок, которых нет в test) — только после подтверждения.
- **Данные** переносятся в порядке зависимостей: родительские таблицы раньше дочерних, поэтому внешние ключи не нарушаются; циклы и самоссылки не приводят к зависанию.
- **Ключ синхронизации** выбирается из колонок, уникальных в обеих базах, или из первичного ключа prod. Строки, которых нет в prod, вставляются, существующие обновляются. Если ключ синхронизации новый, а PK уже занят, строка пропускается с предупреждением.
- **NOT NULL-конфликты** при вставке: снять ограничение или подставить значение по умолчанию — решение запоминается на всю сессию.
- **Пароли не попадают в логи**: DSN маскируется (`postgresql://user:***@host/db`).

## Режимы

| Команда | Поведение |
|---|---|
| `dbsync --dry-run` | только показать план изменений схемы |
| `dbsync` | интерактивно: спрашивает про удаление, ключ синхронизации и NOT NULL |
| `dbsync --yes` | без вопросов: ключ — PK или первая уникальная колонка, лишние объекты сохраняются, на NOT NULL-конфликте остановка с кодом `2` |
| `dbsync --yes --allow-drop` | то же, но лишние таблицы и колонки в prod удаляются |
| `dbsync --yes --not-null-default '—'` | NULL в NOT NULL-колонках заменяется значением |

Решения принимает объект-стратегия (`InteractiveDecider` или `AutoDecider`), поэтому логика синхронизации не зависит от того, есть ли у процесса терминал.

## Запуск

```bash
cp .env.example .env                                   # TEST_DB_DSN, PROD_DB_DSN, LOG_LEVEL
docker compose -f docker-compose.test.yml up -d --wait # две базы для экспериментов
uv sync
uv run python scripts/seed_db.py                       # демо-схемы с расхождениями
uv run dbsync --dry-run
uv run dbsync
uv run python scripts/show_db.py                       # содержимое обеих баз
```

## Тесты

```bash
docker compose -f docker-compose.test.yml up -d --wait
TEST_DB_DSN=postgresql://test_user:test_pass@localhost:5432/test \
PROD_DB_DSN=postgresql://prod_user:prod_pass@localhost:5433/prod \
uv run pytest
```

- Юнит-тесты: diff схем, выбор ключа, порядок таблиц, стратегии решений, план, маскирование DSN.
- Интеграционные тесты на двух настоящих PostgreSQL: `--dry-run` ничего не меняет; после полной синхронизации схема и данные prod совпадают с test, внешний ключ создан; без `--allow-drop` лишние объекты сохраняются; повторный запуск не находит изменений.

CI прогоняет тесты на Python 3.11–3.13 с двумя service-контейнерами PostgreSQL.

## Структура

```
src/dbsync/
├── runner.py      # diff, применение изменений, синхронизация данных, CLI
├── database.py    # PostgresInspector: чтение схемы и DDL/DML через psycopg 3
├── decisions.py   # стратегии решений: интерактивная и по политике
└── config.py      # DSN из окружения, маскирование паролей
scripts/           # демо-данные и просмотр баз
```
