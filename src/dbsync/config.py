"""
Модуль для управления конфигурацией приложения dbsync.

Предоставляет класс `SyncConfig` для инкапсуляции настроек подключения к базам данных
и уровня логирования, загружаемых из переменных окружения или файла `.env`.
"""

import os
from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

from dotenv import load_dotenv

# Загрузка переменных окружения из файла .env, если он существует.
# Это позволяет гибко настраивать параметры без изменения кода.
load_dotenv()


@dataclass(frozen=True)
class SyncConfig:
    """
    Класс конфигурации для синхронизации баз данных.

    Содержит строки подключения к тестовой и продуктовой базам данных,
    а также уровень логирования. Экземпляры этого класса являются неизменяемыми (frozen=True).

    Атрибуты:
        test_dsn (str): Строка подключения (DSN) к тестовой базе данных PostgreSQL.
        prod_dsn (str): Строка подключения (DSN) к продуктовой базе данных PostgreSQL.
        log_level (str): Уровень логирования (например, "INFO", "DEBUG", "WARNING").
                         По умолчанию "INFO".
    """

    test_dsn: str
    prod_dsn: str
    log_level: str = "INFO"

    @classmethod
    def from_env(cls, env: Mapping[str, str] = os.environ) -> "SyncConfig":
        """
        Создает экземпляр `SyncConfig`, извлекая параметры из переменных окружения.

        Если переменные окружения `TEST_DB_DSN` или `PROD_DB_DSN` не установлены,
        будет вызвано исключение `KeyError`. Уровень логирования `LOG_LEVEL`
        является опциональным и по умолчанию устанавливается в "INFO".

        Аргументы:
            env (Mapping[str, str]): Словарь, представляющий переменные окружения.
                                     По умолчанию используется `os.environ`.

        Возвращает:
            SyncConfig: Инициализированный объект конфигурации.

        Исключения:
            KeyError: Если обязательные переменные окружения (TEST_DB_DSN, PROD_DB_DSN)
                      не найдены.
        """
        return cls(
            test_dsn=env["TEST_DB_DSN"],
            prod_dsn=env["PROD_DB_DSN"],
            log_level=env.get("LOG_LEVEL", "INFO").upper(),
        )


def mask_dsn(dsn: str) -> str:
    """
    Скрывает пароль в DSN перед записью в лог.
    """
    parts = urlsplit(dsn)
    if not parts.password:
        return dsn
    netloc = parts.netloc.replace(f":{parts.password}@", ":***@", 1)
    return urlunsplit(parts._replace(netloc=netloc))
