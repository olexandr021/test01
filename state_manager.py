# File: modules/bot_core/state_manager.py

# BLOCK 1: Imports, Error Class, Constants
import asyncio
import aiosqlite # Асинхронна бібліотека для SQLite
import os
import copy
import json # Для можливої серіалізації/десеріалізації в майбутньому
from decimal import Decimal, InvalidOperation, Context, ROUND_HALF_UP # Додано Context, ROUND_HALF_UP
from datetime import datetime, timezone
from modules.logger import logger
from typing import List, Dict, Any, Optional # Додано для type hinting

# Шлях до файлу бази даних (можна винести в конфігурацію пізніше)
DB_FILE = 'bot_database.db'

# Створюємо контекст для Decimal, якщо потрібне специфічне округлення при конвертації
# DEC_CTX = Context(prec=28, rounding=ROUND_HALF_UP) # Приклад

class StateManagerError(Exception):
    """Спеціальний клас винятків для Async StateManager."""
    pass
# END BLOCK 1

# BLOCK 2: Class Definition and __init__
class StateManager:
    """
    Асинхронний менеджер стану бота.
    Використовує SQLite для персистентного стану (позиції, історія, ордери)
    та asyncio.Lock для синхронізації доступу.
    Баланси зберігаються в пам'яті для швидкості.
    """
    def __init__(self, db_path: str = DB_FILE):
        """
        Ініціалізує менеджер стану.

        Args:
            db_path (str): Шлях до файлу бази даних SQLite.
        """
        self.db_path = db_path
        # Асинхронне блокування для захисту доступу до БД та стану в пам'яті
        self.lock = asyncio.Lock()
        # З'єднання з БД буде встановлено асинхронно в initialize()
        self.db_conn: aiosqlite.Connection | None = None
        # Баланси зберігаємо в пам'яті для швидкого доступу (захищено тим же локом)
        self._balances: dict = {}
        # Прапорець ініціалізації
        self._initialized: bool = False

        logger.info(f"AsyncStateManager: Ініціалізовано (DB: '{self.db_path}'). Потрібен виклик initialize().")

    # Наступні методи будуть додані в наступних блоках...

# END BLOCK 2

# BLOCK 3: Async Initialization Method
    async def initialize(self):
        """
        Асинхронно підключається до бази даних та створює таблиці, якщо вони не існують.
        Має бути викликаний перед використанням інших методів, що працюють з БД.
        """
        # Перевіряємо, чи вже ініціалізовано
        if self._initialized:
            logger.debug("AsyncStateManager: Вже ініціалізовано.")
            return

        # Використовуємо асинхронне блокування
        async with self.lock:
            # Додаткова перевірка всередині блокування
            if self._initialized:
                logger.debug("AsyncStateManager: Вже ініціалізовано (перевірка всередині lock).")
                return
            if self.db_conn is not None:
                 logger.warning("AsyncStateManager: Спроба ініціалізації при вже існуючому з'єднанні.")
                 # Можливо, закрити старе з'єднання? Поки що просто виходимо.
                 # await self.close() # Можна додати метод закриття
                 return

            logger.info(f"AsyncStateManager: Ініціалізація бази даних: {self.db_path}...")
            cursor: aiosqlite.Cursor | None = None # Ініціалізуємо None
            try:
                # Встановлюємо з'єднання
                self.db_conn = await aiosqlite.connect(self.db_path)
                # Встановлюємо row_factory для отримання результатів у вигляді словників
                self.db_conn.row_factory = aiosqlite.Row
                logger.info("AsyncStateManager: З'єднання з БД встановлено.")

                # Отримуємо курсор
                cursor = await self.db_conn.cursor()

                # --- SQL запити для створення таблиць (якщо не існують) ---
                # (Скопійовано зі схеми db_schema_v1)
                await cursor.execute("""
                    CREATE TABLE IF NOT EXISTS klines (
                        symbol TEXT NOT NULL, interval TEXT NOT NULL, timestamp INTEGER NOT NULL,
                        open TEXT NOT NULL, high TEXT NOT NULL, low TEXT NOT NULL, close TEXT NOT NULL,
                        volume TEXT NOT NULL, quote_asset_volume TEXT NOT NULL, num_trades INTEGER NOT NULL,
                        taker_base_vol TEXT NOT NULL, taker_quote_vol TEXT NOT NULL,
                        PRIMARY KEY (symbol, interval, timestamp)
                    )
                """)
                await cursor.execute("CREATE INDEX IF NOT EXISTS idx_klines_symbol_interval_timestamp ON klines (symbol, interval, timestamp)")

                await cursor.execute("""
                    CREATE TABLE IF NOT EXISTS trades (
                        trade_id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp INTEGER NOT NULL, symbol TEXT NOT NULL,
                        order_id_binance INTEGER NOT NULL, client_order_id TEXT, order_list_id_binance INTEGER,
                        side TEXT NOT NULL, type TEXT NOT NULL, status TEXT NOT NULL, price TEXT NOT NULL,
                        quantity TEXT NOT NULL, commission TEXT, commission_asset TEXT, is_maker INTEGER,
                        purpose TEXT, pnl TEXT
                    )
                """)
                await cursor.execute("CREATE INDEX IF NOT EXISTS idx_trades_timestamp ON trades (timestamp)")
                await cursor.execute("CREATE INDEX IF NOT EXISTS idx_trades_symbol_timestamp ON trades (symbol, timestamp)")
                await cursor.execute("CREATE INDEX IF NOT EXISTS idx_trades_order_id ON trades (order_id_binance)")

                await cursor.execute("""
                    CREATE TABLE IF NOT EXISTS positions (
                        symbol TEXT PRIMARY KEY NOT NULL, quantity TEXT NOT NULL, entry_price TEXT NOT NULL,
                        entry_timestamp INTEGER NOT NULL, entry_order_id_binance INTEGER, entry_client_order_id TEXT,
                        entry_cost TEXT, entry_commission TEXT, entry_commission_asset TEXT,
                        status TEXT NOT NULL DEFAULT 'OPEN', oco_list_id TEXT
                    )
                """)

                await cursor.execute("""
                    CREATE TABLE IF NOT EXISTS orders (
                        internal_id TEXT PRIMARY KEY NOT NULL, symbol TEXT NOT NULL, order_id_binance INTEGER,
                        client_order_id TEXT, list_client_order_id TEXT, order_list_id_binance INTEGER,
                        side TEXT NOT NULL, type TEXT NOT NULL, quantity_req TEXT NOT NULL,
                        quantity_filled TEXT DEFAULT '0', price TEXT, stop_price TEXT, status TEXT NOT NULL,
                        purpose TEXT, timestamp INTEGER NOT NULL
                    )
                """)
                await cursor.execute("CREATE INDEX IF NOT EXISTS idx_orders_symbol_status ON orders (symbol, status)")
                await cursor.execute("CREATE INDEX IF NOT EXISTS idx_orders_order_id ON orders (order_id_binance)")

                # Зберігаємо зміни
                await self.db_conn.commit()
                logger.info("AsyncStateManager: Таблиці БД перевірено/створено.")

                # Встановлюємо прапорець успішної ініціалізації
                self._initialized = True

            except aiosqlite.Error as e:
                logger.critical(f"AsyncStateManager: Помилка SQLite під час ініціалізації: {e}", exc_info=True)
                # Якщо виникла помилка, закриваємо з'єднання, якщо воно було створено
                if self.db_conn:
                    await self.db_conn.close()
                    self.db_conn = None
                # Скидаємо прапорець ініціалізації
                self._initialized = False
                # Перекидаємо помилку далі, щоб сигналізувати про невдачу
                raise StateManagerError(f"Помилка ініціалізації БД: {e}") from e
            except Exception as e:
                 logger.critical(f"AsyncStateManager: Невідома помилка під час ініціалізації БД: {e}", exc_info=True)
                 if self.db_conn:
                    await self.db_conn.close()
                    self.db_conn = None
                 self._initialized = False
                 raise StateManagerError(f"Невідома помилка ініціалізації БД: {e}") from e
            finally:
                # Завжди закриваємо курсор, якщо він був створений
                if cursor:
                    await cursor.close()
                    logger.debug("AsyncStateManager: Курсор закрито.")

    # Наступні методи...
# END BLOCK 3

# BLOCK 4: Async DB Helpers and Data Conversion
    async def _ensure_initialized(self):
        """Перевіряє, чи StateManager ініціалізовано (БД підключено)."""
        if not self._initialized or self.db_conn is None:
            err_msg = "AsyncStateManager не ініціалізовано. Викличте initialize() перед використанням."
            logger.error(err_msg)
            raise StateManagerError(err_msg)

    async def _execute_sql(self, sql: str, params: tuple | list | None = None) -> int:
        """
        Асинхронно виконує SQL запит (INSERT, UPDATE, DELETE).
        Повертає lastrowid для INSERT або rowcount для UPDATE/DELETE.
        """
        async with self.lock: # Захоплюємо блокування
            await self._ensure_initialized() # Перевіряємо ініціалізацію
            cursor: aiosqlite.Cursor | None = None
            try:
                logger.debug(f"SQL Execute: {sql} | Params: {params}")
                cursor = await self.db_conn.cursor()
                await cursor.execute(sql, params or ())
                await self.db_conn.commit()
                # Повертаємо ID останнього вставленого рядка або кількість змінених рядків
                result_id = cursor.lastrowid if cursor.lastrowid != 0 else cursor.rowcount
                return result_id if result_id is not None else 0
            except aiosqlite.Error as e:
                logger.error(f"Помилка виконання SQL: {e}. Запит: {sql}", exc_info=True)
                # Можна спробувати відкат транзакції, хоча commit() міг не викликатись
                # await self.db_conn.rollback()
                raise StateManagerError(f"Помилка виконання SQL: {e}") from e
            finally:
                if cursor:
                    await cursor.close()

    async def _fetch_one(self, sql: str, params: tuple | list | None = None) -> aiosqlite.Row | None:
        """Асинхронно виконує SQL запит та повертає один рядок (або None)."""
        async with self.lock:
            await self._ensure_initialized()
            cursor: aiosqlite.Cursor | None = None
            try:
                logger.debug(f"SQL Fetch One: {sql} | Params: {params}")
                cursor = await self.db_conn.cursor()
                await cursor.execute(sql, params or ())
                row = await cursor.fetchone()
                return row # Повертає aiosqlite.Row або None
            except aiosqlite.Error as e:
                logger.error(f"Помилка отримання одного запису: {e}. Запит: {sql}", exc_info=True)
                raise StateManagerError(f"Помилка отримання одного запису: {e}") from e
            finally:
                if cursor:
                    await cursor.close()

    async def _fetch_all(self, sql: str, params: tuple | list | None = None) -> list[aiosqlite.Row]:
        """Асинхронно виконує SQL запит та повертає список рядків."""
        async with self.lock:
            await self._ensure_initialized()
            cursor: aiosqlite.Cursor | None = None
            try:
                logger.debug(f"SQL Fetch All: {sql} | Params: {params}")
                cursor = await self.db_conn.cursor()
                await cursor.execute(sql, params or ())
                rows = await cursor.fetchall()
                return rows # Повертає список aiosqlite.Row
            except aiosqlite.Error as e:
                logger.error(f"Помилка отримання всіх записів: {e}. Запит: {sql}", exc_info=True)
                raise StateManagerError(f"Помилка отримання всіх записів: {e}") from e
            finally:
                if cursor:
                    await cursor.close()

    # --- Функції конвертації типів (не асинхронні) ---
    @staticmethod
    def _to_db_decimal(value: Decimal | None) -> str | None:
        """Конвертує Decimal в рядок для збереження в БД."""
        if value is None:
            return None
        if not isinstance(value, Decimal):
            try:
                # Спробувати конвертувати в Decimal перед конвертацією в рядок
                value = Decimal(str(value))
            except (InvalidOperation, TypeError, ValueError):
                logger.warning(f"Не вдалося конвертувати '{value}' ({type(value)}) в Decimal перед збереженням. Збережено як None.")
                return None
        # Використовуємо 'f' для стандартного представлення, уникаючи експоненти
        return format(value, 'f')

    @staticmethod
    def _from_db_decimal(value: str | None) -> Decimal | None:
        """Конвертує рядок з БД в Decimal. Повертає None у разі помилки."""
        if value is None:
            return None
        try:
            return Decimal(value)
        except (InvalidOperation, TypeError, ValueError) as e:
            logger.warning(f"Не вдалося конвертувати '{value}' з БД в Decimal: {e}. Повернено None.")
            return None

    @staticmethod
    def _to_db_timestamp(value: datetime | None) -> int | None:
        """Конвертує datetime (aware, UTC) в мілісекунди Unix timestamp."""
        if value is None:
            return None
        if not isinstance(value, datetime):
            logger.warning(f"Спроба конвертувати не datetime ({type(value)}) в timestamp. Повернено None.")
            return None
        # Переконуємось, що є часова зона
        if value.tzinfo is None:
             value = value.replace(tzinfo=timezone.utc) # Припускаємо UTC
        # Конвертуємо в UTC та отримуємо timestamp
        return int(value.astimezone(timezone.utc).timestamp() * 1000)

    @staticmethod
    def _from_db_timestamp(value: int | None) -> datetime | None:
        """Конвертує мілісекунди Unix timestamp в datetime (aware, UTC)."""
        if value is None:
            return None
        try:
            # Перевірка типу
            if not isinstance(value, int):
                value = int(value)
            # Конвертуємо мілісекунди в секунди
            timestamp_sec = value / 1000.0
            # Створюємо datetime об'єкт в UTC
            dt_utc = datetime.fromtimestamp(timestamp_sec, tz=timezone.utc)
            return dt_utc
        except (ValueError, TypeError, OverflowError) as e:
            logger.warning(f"Не вдалося конвертувати timestamp '{value}' з БД в datetime: {e}. Повернено None.")
            return None

    # Наступні методи...
# END BLOCK 4

# BLOCK 5: Position Management Methods (Async)
    # --- ЗМІНА: Модифіковано get_open_position ---
    async def get_open_position(self, symbol: str) -> dict | None:
        """
        Асинхронно отримує дані відкритої позиції для вказаного символу з БД.
        Повертає словник з даними позиції або None, якщо позиції немає.

        Args:
            symbol (str): Торговий символ (напр., 'BTCUSDT').
        """
        # Модифіковано SQL-запит для фільтрації за символом
        sql = "SELECT * FROM positions WHERE symbol = ?"
        # Передаємо символ як параметр
        row = await self._fetch_one(sql, (symbol,))

        if row:
            # Конвертуємо aiosqlite.Row в словник
            position_dict = dict(row)
            # Конвертуємо значення з БД у типи Python
            position_dict['quantity'] = self._from_db_decimal(position_dict.get('quantity'))
            position_dict['entry_price'] = self._from_db_decimal(position_dict.get('entry_price'))
            position_dict['entry_timestamp'] = self._from_db_timestamp(position_dict.get('entry_timestamp'))
            position_dict['entry_cost'] = self._from_db_decimal(position_dict.get('entry_cost'))
            position_dict['entry_commission'] = self._from_db_decimal(position_dict.get('entry_commission'))
            # oco_list_id залишається рядком або None
            # status залишається рядком
            # entry_order_id_binance залишається int або None
            # entry_client_order_id залишається рядком або None
            # entry_commission_asset залишається рядком або None

            # Оновлено логування для включення символу
            logger.debug(f"AsyncStateManager: Отримано позицію для {symbol} з БД.")
            return position_dict
        else:
            # Оновлено логування для включення символу
            logger.debug(f"AsyncStateManager: Відкрита позиція для {symbol} не знайдена в БД.")
            return None
    # --- КІНЕЦЬ ЗМІНИ ---

    async def set_open_position(self, position_data: dict):
        """
        Асинхронно зберігає або оновлює дані відкритої позиції в БД.
        Використовує INSERT OR REPLACE.
        """
        if not isinstance(position_data, dict):
            logger.error("AsyncStateManager: set_open_position отримав не словник.")
            raise StateManagerError("position_data має бути словником.")

        symbol = position_data.get('symbol')
        if not symbol:
            logger.error("AsyncStateManager: set_open_position не отримав 'symbol' в даних.")
            raise StateManagerError("Відсутній 'symbol' в даних позиції.")

        # Готуємо дані для запису в БД (конвертуємо типи)
        db_data = (
            symbol,
            self._to_db_decimal(position_data.get('quantity')),
            self._to_db_decimal(position_data.get('entry_price')),
            self._to_db_timestamp(position_data.get('entry_timestamp')),
            position_data.get('entry_order_id_binance'), # int or None
            position_data.get('entry_client_order_id'), # str or None
            self._to_db_decimal(position_data.get('entry_cost')),
            self._to_db_decimal(position_data.get('entry_commission')),
            position_data.get('entry_commission_asset'), # str or None
            position_data.get('status', 'OPEN'), # str
            position_data.get('oco_list_id') # str or None
        )

        sql = """
            INSERT OR REPLACE INTO positions (
                symbol, quantity, entry_price, entry_timestamp,
                entry_order_id_binance, entry_client_order_id, entry_cost,
                entry_commission, entry_commission_asset, status, oco_list_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        await self._execute_sql(sql, db_data)
        logger.info(f"AsyncStateManager: Позицію для {symbol} збережено/оновлено в БД.")

    async def update_open_position_field(self, symbol: str, field: str, value: any):
        """
        Асинхронно оновлює конкретне поле у відкритій позиції в БД.
        """
        # Список дозволених полів для оновлення
        allowed_fields = [
            'quantity', 'entry_price', 'entry_timestamp', 'entry_order_id_binance',
            'entry_client_order_id', 'entry_cost', 'entry_commission',
            'entry_commission_asset', 'status', 'oco_list_id'
        ]
        if field not in allowed_fields:
            logger.error(f"AsyncStateManager: Спроба оновити недозволене поле позиції: '{field}'")
            raise StateManagerError(f"Недозволене поле для оновлення: {field}")

        # Конвертуємо значення в залежності від поля
        db_value = None
        if field in ['quantity', 'entry_price', 'entry_cost', 'entry_commission']:
            db_value = self._to_db_decimal(value)
        elif field == 'entry_timestamp':
            db_value = self._to_db_timestamp(value)
        elif field == 'entry_order_id_binance':
            db_value = int(value) if value is not None else None
        else: # Для status, oco_list_id, entry_client_order_id, entry_commission_asset
            db_value = str(value) if value is not None else None

        # Формуємо SQL запит безпечно
        # Не використовуємо f-string для імені поля, щоб уникнути ін'єкції
        sql = f"UPDATE positions SET {field} = ? WHERE symbol = ?"
        params = (db_value, symbol)

        rows_affected = await self._execute_sql(sql, params)
        if rows_affected > 0:
            logger.info(f"AsyncStateManager: Поле '{field}' позиції {symbol} оновлено в БД.")
        else:
            logger.warning(f"AsyncStateManager: Позиція {symbol} не знайдена для оновлення поля '{field}'.")

    async def delete_open_position(self, symbol: str):
        """Асинхронно видаляє відкриту позицію з БД."""
        sql = "DELETE FROM positions WHERE symbol = ?"
        rows_affected = await self._execute_sql(sql, (symbol,))
        if rows_affected > 0:
            logger.info(f"AsyncStateManager: Позицію для {symbol} видалено з БД.")
        else:
            logger.debug(f"AsyncStateManager: Позиція {symbol} не знайдена для видалення.")

    # Наступні методи...
# END BLOCK 5

# BLOCK 6: Trade History Methods (Async)
    async def add_trade_history_entry(self, trade_data: Dict[str, Any]):
        """
        Асинхронно додає новий запис про виконану угоду (fill) до історії в БД.
        """
        if not isinstance(trade_data, dict):
            logger.error("AsyncStateManager: add_trade_history_entry отримав не словник.")
            raise StateManagerError("trade_data має бути словником.")

        # Перевірка наявності обов'язкових полів (приклад)
        required_keys = ['timestamp', 'symbol', 'order_id_binance', 'side', 'type', 'status', 'price', 'quantity']
        for key in required_keys:
            if key not in trade_data:
                 logger.error(f"AsyncStateManager: Відсутній ключ '{key}' в trade_data для історії.")
                 raise StateManagerError(f"Відсутній ключ '{key}' в trade_data.")

        # Готуємо дані для вставки, конвертуючи типи
        db_data = (
            self._to_db_timestamp(trade_data.get('timestamp')),
            trade_data.get('symbol'),
            trade_data.get('order_id_binance'),
            trade_data.get('client_order_id'),
            trade_data.get('order_list_id_binance'),
            trade_data.get('side'),
            trade_data.get('type'),
            trade_data.get('status'),
            self._to_db_decimal(trade_data.get('price')),
            self._to_db_decimal(trade_data.get('quantity')), # Використовуємо quantity замість total_filled_qty
            self._to_db_decimal(trade_data.get('commission')),
            trade_data.get('commission_asset'),
            int(trade_data.get('is_maker', 0)), # Конвертуємо bool в int (0 або 1)
            trade_data.get('purpose'),
            self._to_db_decimal(trade_data.get('pnl'))
        )

        sql = """
            INSERT INTO trades (
                timestamp, symbol, order_id_binance, client_order_id, order_list_id_binance,
                side, type, status, price, quantity, commission, commission_asset,
                is_maker, purpose, pnl
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """

        trade_id = await self._execute_sql(sql, db_data)
        logger.info(f"AsyncStateManager: Додано запис до історії угод. Trade ID: {trade_id}, Order ID: {trade_data.get('order_id_binance')}")
        # Повертаємо ID створеного запису
        return trade_id

    async def get_trade_history(self, symbol: Optional[str] = None, count: int = 50) -> List[Dict[str, Any]]:
        """
        Асинхронно отримує останні N записів історії угод з БД.
        Можна фільтрувати за символом.
        """
        params = []
        sql = "SELECT * FROM trades"
        if symbol:
            sql += " WHERE symbol = ?"
            params.append(symbol)

        sql += " ORDER BY timestamp DESC" # Спочатку сортуємо за часом

        # Додаємо LIMIT тільки якщо count > 0
        if count > 0:
            sql += " LIMIT ?"
            params.append(count)

        rows = await self._fetch_all(sql, tuple(params))
        history_list = []
        for row in rows:
            trade_dict = dict(row)
            # Конвертуємо значення з БД у типи Python
            trade_dict['timestamp'] = self._from_db_timestamp(trade_dict.get('timestamp'))
            trade_dict['price'] = self._from_db_decimal(trade_dict.get('price'))
            trade_dict['quantity'] = self._from_db_decimal(trade_dict.get('quantity'))
            trade_dict['commission'] = self._from_db_decimal(trade_dict.get('commission'))
            trade_dict['pnl'] = self._from_db_decimal(trade_dict.get('pnl'))
            trade_dict['is_maker'] = bool(trade_dict.get('is_maker', 0)) # Конвертуємо int назад в bool
            history_list.append(trade_dict)

        logger.debug(f"AsyncStateManager: Отримано {len(history_list)} записів історії (count={count}, symbol={symbol}).")
        # Список вже відсортований DESC за timestamp з БД, повертаємо як є
        return history_list

    async def update_trade_pnl(self, trade_id: int, pnl: Decimal):
        """
        Асинхронно оновлює поле PnL для конкретного запису в історії угод.
        """
        db_pnl = self._to_db_decimal(pnl)
        if db_pnl is None:
            logger.warning(f"AsyncStateManager: Некоректне значення PnL ({pnl}) для оновлення trade_id={trade_id}.")
            return # Не оновлюємо, якщо PnL некоректний

        sql = "UPDATE trades SET pnl = ? WHERE trade_id = ?"
        params = (db_pnl, trade_id)
        rows_affected = await self._execute_sql(sql, params)
        if rows_affected > 0:
            logger.info(f"AsyncStateManager: Оновлено PnL для trade_id={trade_id}.")
        else:
            logger.warning(f"AsyncStateManager: Запис trade_id={trade_id} не знайдено для оновлення PnL.")

    # Наступні методи...
# END BLOCK 6

# BLOCK 7: Active Order Methods (Async)
    async def get_active_order(self, internal_id: str) -> Optional[Dict[str, Any]]:
        """
        Асинхронно отримує дані активного ордера за його internal_id з БД.
        """
        sql = "SELECT * FROM orders WHERE internal_id = ?"
        row = await self._fetch_one(sql, (internal_id,))
        if row:
            order_dict = dict(row)
            # Конвертуємо значення з БД
            order_dict['timestamp'] = self._from_db_timestamp(order_dict.get('timestamp'))
            order_dict['quantity_req'] = self._from_db_decimal(order_dict.get('quantity_req'))
            order_dict['quantity_filled'] = self._from_db_decimal(order_dict.get('quantity_filled'))
            order_dict['price'] = self._from_db_decimal(order_dict.get('price'))
            order_dict['stop_price'] = self._from_db_decimal(order_dict.get('stop_price'))
            logger.debug(f"AsyncStateManager: Отримано активний ордер {internal_id} з БД.")
            return order_dict
        else:
            logger.debug(f"AsyncStateManager: Активний ордер {internal_id} не знайдено в БД.")
            return None

    async def get_all_active_orders(self, symbol: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        Асинхронно отримує список всіх активних ордерів з БД.
        Можна фільтрувати за символом.
        """
        params = []
        sql = "SELECT * FROM orders"
        if symbol:
            sql += " WHERE symbol = ?"
            params.append(symbol)
        sql += " ORDER BY timestamp DESC" # Сортуємо за часом

        rows = await self._fetch_all(sql, tuple(params))
        orders_list = []
        for row in rows:
            order_dict = dict(row)
            # Конвертуємо значення з БД
            order_dict['timestamp'] = self._from_db_timestamp(order_dict.get('timestamp'))
            order_dict['quantity_req'] = self._from_db_decimal(order_dict.get('quantity_req'))
            order_dict['quantity_filled'] = self._from_db_decimal(order_dict.get('quantity_filled'))
            order_dict['price'] = self._from_db_decimal(order_dict.get('price'))
            order_dict['stop_price'] = self._from_db_decimal(order_dict.get('stop_price'))
            orders_list.append(order_dict)

        logger.debug(f"AsyncStateManager: Отримано {len(orders_list)} активних ордерів (symbol={symbol}).")
        return orders_list

    async def add_or_update_active_order(self, internal_id: str, order_data: Dict[str, Any]):
        """
        Асинхронно додає або оновлює активний ордер в БД.
        Використовує INSERT OR REPLACE.
        """
        if not isinstance(order_data, dict):
            logger.error("AsyncStateManager: add_or_update_active_order отримав не словник.")
            raise StateManagerError("order_data має бути словником.")
        if not internal_id:
             logger.error("AsyncStateManager: add_or_update_active_order отримав порожній internal_id.")
             raise StateManagerError("internal_id не може бути порожнім.")

        # Готуємо дані для запису, конвертуючи типи
        db_data = (
            internal_id,
            order_data.get('symbol'),
            order_data.get('order_id_binance'),
            order_data.get('client_order_id'),
            order_data.get('list_client_order_id'),
            order_data.get('order_list_id_binance'),
            order_data.get('side'),
            order_data.get('type'),
            self._to_db_decimal(order_data.get('quantity_req')),
            self._to_db_decimal(order_data.get('quantity_filled', Decimal(0))), # Дефолт 0
            self._to_db_decimal(order_data.get('price')),
            self._to_db_decimal(order_data.get('stop_price')),
            order_data.get('status'),
            order_data.get('purpose'),
            self._to_db_timestamp(order_data.get('timestamp', datetime.now(timezone.utc))) # Поточний час як дефолт
        )

        sql = """
            INSERT OR REPLACE INTO orders (
                internal_id, symbol, order_id_binance, client_order_id, list_client_order_id,
                order_list_id_binance, side, type, quantity_req, quantity_filled,
                price, stop_price, status, purpose, timestamp
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        await self._execute_sql(sql, db_data)
        logger.info(f"AsyncStateManager: Активний ордер {internal_id} збережено/оновлено в БД.")

    async def remove_active_order(self, internal_id: str) -> Optional[Dict[str, Any]]:
        """
        Асинхронно видаляє активний ордер з БД та повертає його дані.
        """
        # Спочатку отримуємо дані ордера, щоб повернути їх
        order_to_return = await self.get_active_order(internal_id)

        if order_to_return:
            # Якщо ордер знайдено, видаляємо його
            sql = "DELETE FROM orders WHERE internal_id = ?"
            rows_affected = await self._execute_sql(sql, (internal_id,))
            if rows_affected > 0:
                logger.info(f"AsyncStateManager: Активний ордер {internal_id} видалено з БД.")
            else:
                # Це не мало б статися, якщо get_active_order щойно його знайшов, але про всяк випадок
                logger.warning(f"AsyncStateManager: Ордер {internal_id} знайдено, але не вдалося видалити?")
            return order_to_return
        else:
            # Якщо ордер не знайдено спочатку
            logger.debug(f"AsyncStateManager: Активний ордер {internal_id} не знайдено для видалення.")
            return None

    async def clear_active_orders_for_symbol(self, symbol: str):
        """
        Асинхронно видаляє всі активні ордери для вказаного символу з БД.
        """
        sql = "DELETE FROM orders WHERE symbol = ?"
        rows_affected = await self._execute_sql(sql, (symbol,))
        if rows_affected > 0:
            logger.info(f"AsyncStateManager: Видалено {rows_affected} активних ордерів для {symbol} з БД.")
        else:
            logger.debug(f"AsyncStateManager: Не знайдено активних ордерів для видалення для {symbol}.")

# END BLOCK 7

# BLOCK 8: Balance Management Methods (In-Memory, Async Lock)
    async def get_balances(self) -> Dict[str, Any]:
        """Повертає глибоку копію словника балансів (потокобезпечно)."""
        async with self.lock:
            # Повертаємо глибоку копію, щоб уникнути зовнішніх модифікацій
            balances_copy = copy.deepcopy(self._balances)
            return balances_copy

    async def set_balances(self, balance_data: Dict[str, Any]):
        """Повністю замінює словник балансів (потокобезпечно)."""
        if not isinstance(balance_data, dict):
            logger.error("AsyncStateManager: set_balances отримав не словник.")
            raise StateManagerError("balance_data має бути словником.")

        async with self.lock:
            # Зберігаємо глибоку копію
            new_balance_copy = copy.deepcopy(balance_data)
            # --- Додаткова конвертація в Decimal при встановленні ---
            converted_balances = {}
            for asset, data in new_balance_copy.items():
                if isinstance(data, dict):
                    # --- ВИПРАВЛЕННЯ ФОРМАТУВАННЯ: Розділено логіку ---
                    free_str = str(data.get('free', '0'))
                    free_decimal = self._from_db_decimal(free_str)
                    free_val = free_decimal if free_decimal is not None else Decimal(0)

                    locked_str = str(data.get('locked', '0'))
                    locked_decimal = self._from_db_decimal(locked_str)
                    locked_val = locked_decimal if locked_decimal is not None else Decimal(0)
                    # --- КІНЕЦЬ ВИПРАВЛЕННЯ ФОРМАТУВАННЯ ---

                    converted_balances[asset] = {'free': free_val, 'locked': locked_val}
                else:
                     logger.warning(f"Некоректний формат даних для активу {asset} в set_balances.")
            # --- Кінець конвертації ---
            self._balances = converted_balances # Зберігаємо вже з Decimal
            logger.info("AsyncStateManager: Словник балансів (в пам'яті) повністю оновлено.")
            logger.debug(f"AsyncStateManager: Нові баланси: {self._balances}")


    async def update_balance_asset(self, asset: str, free: Decimal, locked: Decimal):
        """Оновлює баланс для конкретного активу (в пам'яті, потокобезпечно)."""
        if not isinstance(free, Decimal) or not isinstance(locked, Decimal):
            logger.error(f"AsyncStateManager: Некоректні типи для update_balance_asset {asset}. Очікується Decimal.")
            raise StateManagerError("free та locked мають бути Decimal.")

        async with self.lock:
            asset_exists = asset in self._balances
            self._balances[asset] = {'free': free, 'locked': locked}
            balance_removed = False
            # Перевірка на нульовий баланс
            if free == 0 and locked == 0:
                self._balances.pop(asset, None)
                balance_removed = True

            # Логування
            if balance_removed:
                logger.info(f"AsyncStateManager: Баланс активу {asset} оновлено та видалено (став 0).")
            elif asset_exists:
                logger.debug(f"AsyncStateManager: Баланс активу {asset} оновлено.")
            else:
                logger.info(f"AsyncStateManager: Додано новий актив {asset} до балансу.")


    async def update_balance_delta(self, asset: str, delta: Decimal) -> bool:
        """
        Оновлює вільний баланс активу на дельту (в пам'яті, потокобезпечно).
        Повертає True, якщо потрібен повний запит балансу.
        """
        if not isinstance(delta, Decimal):
            logger.error(f"AsyncStateManager: Некоректний тип дельти ({type(delta)}) для {asset}.")
            raise StateManagerError("delta має бути Decimal.")

        needs_full_fetch = False
        balance_updated = False
        balance_removed = False
        current_free_str = "N/A"

        async with self.lock:
            if asset in self._balances:
                current_free = self._balances[asset].get('free', Decimal(0))
                new_free = current_free + delta
                self._balances[asset]['free'] = new_free
                current_free_str = format(new_free, 'f') # Для логу
                balance_updated = True
                if new_free < 0:
                    logger.warning(f"AsyncStateManager: Баланс {asset} став від'ємним ({new_free})! Потрібна синхронізація.")
                    needs_full_fetch = True
                # Перевірка на нульовий баланс
                current_locked = self._balances[asset].get('locked', Decimal(0))
                if new_free == 0 and current_locked == 0:
                    self._balances.pop(asset)
                    balance_removed = True
            else:
                # Актив не відстежується, але прийшла дельта
                if delta > 0:
                    self._balances[asset] = {'free': delta, 'locked': Decimal(0)}
                    current_free_str = format(delta, 'f')
                    balance_updated = True
                    needs_full_fetch = True # Потрібен повний запит, бо не знаємо locked
                    logger.info(f"AsyncStateManager: Додано невідстежуваний актив {asset} з балансом {delta} через delta.")
                else:
                    # Від'ємна дельта для невідстежуваного - ігноруємо
                    logger.debug(f"AsyncStateManager: Ігноруємо від'ємну дельту {delta} для невідстежуваного активу {asset}.")

        # Логування поза блокуванням
        if balance_removed:
            logger.info(f"AsyncStateManager: Актив {asset} видалено з балансу (став 0).")
        elif balance_updated and asset in self._balances: # Перевіряємо, чи актив ще існує
             logger.debug(f"AsyncStateManager: Оновлено баланс {asset}. Новий free: {current_free_str}")

        return needs_full_fetch

    # --- НОВИЙ МЕТОД: update_balances ---
    async def update_balances(self, balances_update: Dict[str, Dict[str, Any]]):
        """
        Оновлює баланси в пам'яті на основі наданого словника.

        Args:
            balances_update (Dict[str, Dict[str, Any]]): Словник, де ключ - назва активу,
                а значення - словник {'asset': str, 'free': Decimal, 'locked': Decimal}.
        """
        if not isinstance(balances_update, dict):
            logger.error("AsyncStateManager: update_balances отримав не словник.")
            # Можна повернути або викликати виняток
            # raise StateManagerError("balances_update має бути словником.")
            return

        if not balances_update:
            logger.debug("AsyncStateManager: update_balances отримав порожній словник, оновлення не потрібне.")
            return

        updated_count = 0
        skipped_count = 0

        async with self.lock: # Захищаємо доступ до self._balances
            for asset, data in balances_update.items():
                if not isinstance(data, dict):
                    logger.warning(f"AsyncStateManager: Некоректний формат даних для активу '{asset}' в update_balances. Пропущено.")
                    skipped_count += 1
                    continue

                free_val = data.get('free')
                locked_val = data.get('locked')

                # Перевіряємо типи отриманих значень
                if not isinstance(asset, str) or not isinstance(free_val, Decimal) or not isinstance(locked_val, Decimal):
                    logger.warning(f"AsyncStateManager: Некоректні типи даних для активу '{asset}' в update_balances "
                                   f"(free: {type(free_val)}, locked: {type(locked_val)}). Пропущено.")
                    skipped_count += 1
                    continue

                # Оновлюємо або додаємо запис
                # Використовуємо ті ж ключі 'free' та 'locked', що й в інших методах цього блоку
                self._balances[asset] = {'free': free_val, 'locked': locked_val}
                logger.debug(f"AsyncStateManager: Оновлено баланс в пам'яті для {asset}: free={free_val}, locked={locked_val}")
                updated_count += 1

        # Логуємо загальний результат поза блокуванням
        if updated_count > 0:
            logger.info(f"AsyncStateManager: Баланси в пам'яті оновлено для {updated_count} активів (пропущено: {skipped_count}).")
        elif skipped_count > 0:
             logger.warning(f"AsyncStateManager: update_balances не оновив жодного балансу (пропущено: {skipped_count}).")
    # --- КІНЕЦЬ НОВОГО МЕТОДУ ---

# END BLOCK 8

# BLOCK 9: Cleanup Method
    async def close(self):
        """Асинхронно закриває з'єднання з базою даних."""
        async with self.lock:
            if self.db_conn:
                try:
                    await self.db_conn.close()
                    logger.info("AsyncStateManager: З'єднання з БД закрито.")
                    self.db_conn = None
                    self._initialized = False
                except aiosqlite.Error as e:
                    logger.error(f"AsyncStateManager: Помилка закриття з'єднання з БД: {e}")
            else:
                logger.debug("AsyncStateManager: З'єднання з БД вже було закрито або не встановлено.")
            # Скидаємо прапорець у будь-якому випадку при спробі закриття
            self._initialized = False

# END BLOCK 9
