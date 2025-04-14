# File: modules/data_manager.py

# BLOCK 1: Imports, Class Definition, __init__
import asyncio
import pandas as pd
from decimal import Decimal
from datetime import datetime
from typing import List, Dict, Any, Optional

# --- ЗМІНА: Імпортуємо StateManager з нового місця ---
from modules.bot_core.state_manager import StateManager, StateManagerError
# --- КІНЕЦЬ ЗМІНИ ---
# Імпортуємо API для майбутнього використання (завантаження нових даних)
from modules.binance_integration import BinanceAPI, BinanceAPIError # Імпортуємо асинхронний API
from modules.logger import logger

class DataManagerError(Exception):
    """Спеціальний клас винятків для DataManager."""
    pass

class DataManager:
    """
    Асинхронний менеджер для отримання та, потенційно, кешування
    ринкових даних (напр., Klines) з бази даних або API.
    """

    def __init__(self, state_manager: StateManager, binance_api: Optional[BinanceAPI] = None):
        """
        Ініціалізує DataManager.

        Args:
            state_manager (StateManager): Екземпляр асинхронного StateManager для доступу до БД.
            binance_api (Optional[BinanceAPI]): Екземпляр асинхронного BinanceAPI (для завантаження нових даних, якщо потрібно).
        """
        if not isinstance(state_manager, StateManager):
             err_msg = "DataManager: Отримано невалідний екземпляр StateManager."
             logger.error(err_msg)
             raise DataManagerError(err_msg)
        # Перевіряємо, чи API (якщо передано) є асинхронним
        if binance_api is not None and not isinstance(binance_api, BinanceAPI):
             err_msg = "DataManager: Отримано невалідний (не асинхронний?) екземпляр BinanceAPI."
             logger.error(err_msg)
             # Можна або генерувати помилку, або просто ігнорувати API
             # raise DataManagerError(err_msg)
             binance_api = None # Ігноруємо невалідний API

        self.state_manager = state_manager
        self.binance_api = binance_api
        logger.info("AsyncDataManager: Ініціалізовано.")

    # Наступні методи будуть додані в наступних блоках...

# END BLOCK 1

# BLOCK 2: Get Klines Method (Reading from DB)
    async def get_klines(self,
                         symbol: str,
                         interval: str,
                         start_ts: Optional[int] = None,
                         end_ts: Optional[int] = None,
                         limit: Optional[int] = None) -> pd.DataFrame:
        """
        Асинхронно отримує історичні дані Klines з бази даних SQLite.

        Args:
            symbol (str): Торговий символ.
            interval (str): Таймфрейм.
            start_ts (Optional[int]): Початковий timestamp (ms UTC).
            end_ts (Optional[int]): Кінцевий timestamp (ms UTC, включно).
            limit (Optional[int]): Обмеження кількості свічок.

        Returns:
            pd.DataFrame: DataFrame з даними Klines (індекс - timestamp),
                          або порожній DataFrame у разі помилки чи відсутності даних.
        """
        logger.debug(f"DataManager: Запит Klines з БД: {symbol} {interval} (Start: {start_ts}, End: {end_ts}, Limit: {limit})")
        params = [symbol, interval]
        sql = "SELECT * FROM klines WHERE symbol = ? AND interval = ?"

        # Додаємо умови для часу
        if start_ts is not None:
            sql += " AND timestamp >= ?"
            params.append(start_ts)
        if end_ts is not None:
            sql += " AND timestamp <= ?"
            params.append(end_ts)

        sql += " ORDER BY timestamp ASC" # Сортуємо за зростанням часу

        # Додаємо LIMIT, якщо вказано
        # Увага: Якщо є і start/end, і limit, LIMIT може обрізати дані не так, як очікується.
        # Краще використовувати LIMIT для отримання останніх N свічок.
        # Якщо потрібен діапазон + ліміт, можливо, краще робити LIMIT в pandas.
        if limit is not None and start_ts is None: # Застосовуємо LIMIT тільки якщо немає start_ts
             sql += " LIMIT ?"
             params.append(limit)
        elif limit is not None:
             logger.warning("DataManager: Параметр 'limit' ігнорується при одночасному використанні 'start_ts'.")


        try:
            # Використовуємо допоміжний метод StateManager для виконання запиту
            # _fetch_all вже використовує lock та перевірку ініціалізації
            rows = await self.state_manager._fetch_all(sql, tuple(params))

            if not rows:
                logger.warning(f"DataManager: Не знайдено Klines у БД для {symbol} {interval} з заданими параметрами.")
                # TODO: Додати логіку завантаження даних з API, якщо їх немає в БД?
                return pd.DataFrame()

            # Конвертація списку рядків aiosqlite.Row у DataFrame
            # Спочатку конвертуємо кожен рядок у словник
            data_list = [dict(row) for row in rows]
            df = pd.DataFrame(data_list)

            # Конвертація типів
            if 'timestamp' in df.columns:
                # Конвертуємо timestamp (int ms) в datetime та встановлюємо як індекс
                df['timestamp_dt'] = df['timestamp'].apply(self.state_manager._from_db_timestamp)
                df = df.set_index('timestamp_dt')
                df.index.name = 'timestamp' # Перейменовуємо індекс
                df = df.drop(columns=['timestamp']) # Видаляємо оригінальний стовпець timestamp
            else:
                 logger.error("DataManager: Стовпець 'timestamp' відсутній після запиту до БД.")
                 return pd.DataFrame() # Повертаємо порожній DF

            # Конвертуємо стовпці, що зберігаються як TEXT, назад у Decimal
            decimal_columns = ['open', 'high', 'low', 'close', 'volume',
                               'quote_asset_volume', 'taker_base_vol', 'taker_quote_vol']
            for col in decimal_columns:
                if col in df.columns:
                    # Використовуємо статичний метод StateManager для конвертації
                    df[col] = df[col].apply(self.state_manager._from_db_decimal)
                # else: Стовпець може бути відсутнім, якщо схема змінилася

            # Конвертуємо num_trades в int (про всяк випадок)
            if 'num_trades' in df.columns:
                 df['num_trades'] = pd.to_numeric(df['num_trades'], errors='coerce').fillna(0).astype(int)

            logger.info(f"DataManager: Успішно отримано {len(df)} Klines з БД для {symbol} {interval}.")
            return df

        except StateManagerError as e:
            logger.error(f"DataManager: Помилка StateManager при отриманні Klines: {e}")
            return pd.DataFrame()
        except Exception as e:
            logger.error(f"DataManager: Загальна помилка при отриманні Klines з БД: {e}", exc_info=True)
            return pd.DataFrame()

    # Метод save_klines буде додано пізніше
    # async def save_klines(self, klines_data: List[List[Any]], symbol: str, interval: str): ...

# END BLOCK 2
