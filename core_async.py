# File: modules/bot_core/core_async.py

# BLOCK 1: Imports, Error Class, Constants
import asyncio
import queue # Використовуємо стандартну чергу для зв'язку з UI/Telegram
import threading # Потрібен для command processor thread
# --- ЗМІНА: Додано List ---
from typing import Dict, Any, Optional, Type, Callable, List # Додано Callable, List
# --- КІНЕЦЬ ЗМІНИ ---
import time
import pandas as pd
from decimal import Decimal, InvalidOperation # Додано InvalidOperation
from datetime import datetime, timezone # Додано datetime, timezone

from modules.logger import logger
from modules.config_manager import ConfigManager
# --- ЗМІНА: Імпортуємо асинхронні версії ---
from modules.bot_core.state_manager import StateManager, StateManagerError
# --- ЗМІНА: BinanceWebSocketHandler тепер також імпортується звідси ---
from modules.binance_integration import BinanceAPI, BinanceAPIError, BinanceWebSocketHandler
from modules.data_manager import DataManager, DataManagerError
# --- КІНЕЦЬ ЗМІНИ ---
# Імпортуємо BaseStrategy та функцію завантаження
try:
    from strategies.base_strategy import BaseStrategy
    from modules.strategies_tab import load_strategies
except ImportError:
    logger.error("AsyncBotCore: Не вдалося імпортувати BaseStrategy або load_strategies.")
    # Створюємо заглушки
    class BaseStrategy:
        def calculate_signals(self, df: pd.DataFrame) -> Any: # Додамо метод-заглушку
             return None
    def load_strategies(): return {}

class AsyncBotCoreError(Exception):
    """Спеціальний клас винятків для AsyncBotCore."""
    pass

# Можна додати константи за потреби
# UPDATE_INTERVAL = 1 # Секунди для основного циклу (приклад)

# END BLOCK 1

# BLOCK 2: Class Definition and __init__
class AsyncBotCore:
    """
    Асинхронне ядро торгового бота.
    Керує життєвим циклом, взаємодією асинхронних компонентів,
    обробкою команд та генерацією подій.
    """

    def __init__(self,
                 config_manager: ConfigManager,
                 state_manager: StateManager,
                 binance_api: BinanceAPI,
                 data_manager: DataManager,
                 command_queue: Optional[queue.Queue] = None,
                 event_queue: Optional[queue.Queue] = None):
        """
        Ініціалізація асинхронного ядра.

        Args:
            config_manager (ConfigManager): Менеджер конфігурації.
            state_manager (StateManager): Асинхронний менеджер стану (з БД).
            binance_api (BinanceAPI): Асинхронний API клієнт Binance.
            data_manager (DataManager): Асинхронний менеджер даних.
            command_queue (Optional[queue.Queue]): Черга команд від UI/Telegram.
            event_queue (Optional[queue.Queue]): Черга подій до UI/Telegram.
        """
        logger.info("AsyncBotCore: Ініціалізація...")

        # --- Перевірка типів залежностей ---
        if not isinstance(config_manager, ConfigManager): raise AsyncBotCoreError("Invalid ConfigManager")
        if not isinstance(state_manager, StateManager): raise AsyncBotCoreError("Invalid StateManager")
        if not isinstance(binance_api, BinanceAPI): raise AsyncBotCoreError("Invalid BinanceAPI")
        if not isinstance(data_manager, DataManager): raise AsyncBotCoreError("Invalid DataManager")

        # --- Збереження залежностей ---
        self.config_manager = config_manager
        self.state_manager = state_manager
        self.binance_api = binance_api
        self.data_manager = data_manager

        # --- Ініціалізація черг (стандартні для зв'язку з синхронним світом) ---
        self.command_queue = command_queue if command_queue is not None else queue.Queue()
        self.event_queue = event_queue if event_queue is not None else queue.Queue()

        # --- Асинхронне блокування ---
        self.lock = asyncio.Lock() # Додано лок

        # --- Внутрішній стан ядра ---
        self._is_running: bool = False # Прапорець роботи основного циклу
        self._initialized: bool = False # Прапорець завершення асинхронної ініціалізації
        self._main_task: Optional[asyncio.Task] = None # Головна асинхронна задача
        self._command_processor_thread: Optional[threading.Thread] = None # Синхронний потік для обробки команд
        self._stop_command_processor: bool = False # Прапорець для зупинки command processor
        # --- ЗМІНА: Додано зберігання основного циклу asyncio ---
        self._main_event_loop: Optional[asyncio.AbstractEventLoop] = None # Цикл asyncio, де працює ядро
        # --- КІНЕЦЬ ЗМІНИ ---


        # --- Компоненти, що створюються/керуються ядром ---
        self._ws_handler: Optional[BinanceWebSocketHandler] = None
        self._strategy_instance: Optional[BaseStrategy] = None

        # --- Поточні налаштування (завантажаться в initialize) ---
        self.config: Dict[str, Any] = {}
        self.active_mode: str = "TestNet"
        self.symbol: str = "BTCUSDT"
        self.interval: str = "1m"
        self.selected_strategy_name: Optional[str] = None
        self.use_testnet: bool = True
        # --- Додано параметри торгівлі ---
        self.position_size_percent: Decimal = Decimal('0.1') # 10%
        self.commission_taker: Decimal = Decimal('0.00075') # 0.075%
        self.tp_percent: Decimal = Decimal('0.01') # 1%
        self.sl_percent: Decimal = Decimal('0.005') # 0.5%
        self.quote_asset: str = 'USDT'
        self.base_asset: str = 'BTC'
        self.price_precision: int = 8
        self.qty_precision: int = 8
        # --- Кінець доданих параметрів ---
        self.required_klines_length: int = 100 # Буде оновлено
        # --- ЗМІНА: Додано кеш для symbol_info ---
        self._symbol_info: Optional[Dict[str, Any]] = None # Кеш для symbol_info
        # --- КІНЕЦЬ ЗМІНИ ---

        # --- Інші змінні стану ---
        self.last_kline_update_time: int = 0 # Час останньої обробленої свічки (ms)
        self.session_stats: Dict[str, Any] = self._reset_session_stats() # Статистика сесії
        self.start_time: Optional[datetime] = None # Час запуску ядра

        logger.info("AsyncBotCore: __init__ завершено. Потрібен виклик initialize().")

    def _reset_session_stats(self) -> Dict[str, Any]:
        """Скидає статистику поточної сесії."""
        stats: Dict[str, Any] = {
            'total_trades': 0, 'winning_trades': 0, 'losing_trades': 0,
            'total_pnl': Decimal(0), 'total_commission': Decimal(0),
            'win_rate': 0.0, 'profit_factor': 0.0,
            'total_profit': Decimal(0), 'total_loss': Decimal(0),
            'avg_profit': Decimal(0), 'avg_loss': Decimal(0),
            'session_duration': "N/A"
        }
        logger.debug("Статистику сесії скинуто.")
        return stats

# END BLOCK 2

# BLOCK 3: Async Initialization and Helper Methods
    async def initialize(self):
        """
        Виконує основну асинхронну ініціалізацію ядра та його залежностей.
        Має бути викликаний перед запуском бота.
        """
        async with self.lock: # Захищаємо процес ініціалізації
            if self._initialized:
                logger.debug("AsyncBotCore: Вже ініціалізовано.")
                return

            logger.info("AsyncBotCore: Початок асинхронної ініціалізації...")
            try:
                # 1. Ініціалізуємо залежні менеджери (якщо вони ще не ініціалізовані)
                # StateManager (БД)
                if hasattr(self.state_manager, 'initialize') and callable(self.state_manager.initialize):
                         await self.state_manager.initialize()
                else: raise AsyncBotCoreError("StateManager не має методу initialize() або він не callable")
                # BinanceAPI (сесія, час)
                if hasattr(self.binance_api, 'initialize') and callable(self.binance_api.initialize):
                         await self.binance_api.initialize()
                else: raise AsyncBotCoreError("BinanceAPI не має методу initialize() або він не callable")
                # DataManager (залежить від StateManager, API) - може не мати свого initialize
                # if hasattr(self.data_manager, 'initialize') and callable(self.data_manager.initialize):
                #     await self.data_manager.initialize()

                # 2. Завантажуємо налаштування бота
                self._load_bot_settings() # Синхронний метод

                # --- ЗМІНА: Отримуємо symbol_info тут, щоб оновити параметри ---
                logger.info("AsyncBotCore: Попереднє отримання symbol_info для оновлення параметрів...")
                await self._get_symbol_info_async() # Оновлює self.price/qty_precision, base/quote_asset
                # --- КІНЕЦЬ ЗМІНИ ---

                # 3. Завантажуємо та ініціалізуємо стратегію
                self._load_strategy() # Синхронний метод
                if not self._strategy_instance:
                    raise AsyncBotCoreError("Не вдалося завантажити або ініціалізувати стратегію.")

                # 4. Створюємо обробник WebSocket
                self._create_ws_handler() # Синхронний метод
                if not self._ws_handler:
                    raise AsyncBotCoreError("Не вдалося створити обробник WebSocket.")

                # --- ЗМІНА: Запускаємо обробник команд ---
                # 5. Запускаємо обробник команд у фоновому потоці
                logger.info("AsyncBotCore: Запуск обробника команд...")
                self._start_command_processor() # Запускаємо потік
                # --- КІНЕЦЬ ЗМІНИ ---

                # 6. Встановлюємо прапорець успішної ініціалізації
                self._initialized = True
                logger.info("AsyncBotCore: Асинхронну ініціалізацію завершено успішно.")

            except (StateManagerError, BinanceAPIError, DataManagerError, AsyncBotCoreError) as e:
                logger.critical(f"AsyncBotCore: Помилка під час асинхронної ініціалізації: {e}", exc_info=True)
                self._initialized = False # Переконуємось, що прапорець False
                # Закриваємо ресурси, якщо вони були відкриті
                await self._cleanup_resources() # Виклик допоміжного методу очищення
                # Перекидаємо помилку далі
                raise AsyncBotCoreError(f"Не вдалося ініціалізувати ядро: {e}") from e
            except Exception as e:
                logger.critical(f"AsyncBotCore: Невідома помилка під час асинхронної ініціалізації: {e}", exc_info=True)
                self._initialized = False
                await self._cleanup_resources() # Виклик допоміжного методу очищення
                raise AsyncBotCoreError(f"Невідома помилка ініціалізації: {e}") from e

    # --- Допоміжні методи для initialize ---

    def _load_bot_settings(self):
        """Завантажує та валідує налаштування бота (синхронний метод)."""
        logger.info("AsyncBotCore: Завантаження налаштувань бота...")
        bot_config_data = self.config_manager.get_bot_config()
        self.config = bot_config_data # Зберігаємо повний конфіг
        try:
            # --- Розмір позиції ---
            pos_size_conf = self.config.get('position_size_percent', 10.0)
            self.position_size_percent = Decimal(str(pos_size_conf)) / Decimal(100)

            # --- Комісія ---
            comm_taker_conf = self.config.get('commission_taker', 0.075)
            self.commission_taker = Decimal(str(comm_taker_conf)) / Decimal(100)

            # --- Take Profit ---
            tp_conf = self.config.get('take_profit_percent', 1.0)
            self.tp_percent = Decimal(str(tp_conf)) / Decimal(100)

            # --- Stop Loss ---
            sl_conf = self.config.get('stop_loss_percent', 0.5)
            self.sl_percent = Decimal(str(sl_conf)) / Decimal(100)

            # --- Символ ---
            symbol_conf = self.config.get('trading_symbol','BTCUSDT')
            self.symbol = symbol_conf.upper()
            # Скидаємо кеш symbol_info, якщо символ змінився (порівняння з попереднім значенням?)
            # Поки що просто скидаємо при кожному завантаженні налаштувань
            self._symbol_info = None
            logger.debug("Кеш symbol_info скинуто через завантаження налаштувань.")


            # --- Стратегія ---
            self.selected_strategy_name = self.config.get('selected_strategy')

            # --- Режим роботи ---
            active_mode_conf = self.config.get('active_mode','TestNet')
            self.active_mode = active_mode_conf
            self.use_testnet = (self.active_mode == 'TestNet')
            # Перевіряємо, чи режим API співпадає з конфігом
            if self.binance_api and self.binance_api._initialized and self.binance_api.use_testnet != self.use_testnet:
                # Це може статися, якщо API вже було ініціалізовано з іншим режимом
                logger.critical(f"Невідповідність режимів! Config: {self.active_mode}, API: {'TestNet' if self.binance_api.use_testnet else 'MainNet'}. Потрібен перезапуск з правильним API!")
                # Генеруємо помилку, щоб зупинити некоректну роботу
                raise AsyncBotCoreError("Невідповідність режимів API та конфігурації.")

            # --- Інтервал Klines ---
            self.interval = self.config.get('kline_interval', '1m')

            # --- Активи та Точність ---
            # Оновлюються асинхронно в _get_symbol_info_async,
            # яке тепер викликається в initialize після _load_bot_settings.
            # Залишаємо тут дефолтні значення на випадок, якщо symbol_info не отримано.
            if self._symbol_info:
                 # Якщо _get_symbol_info_async вже викликався і успішно оновив
                 logger.info("Параметри точності/активів вже оновлено з symbol_info.")
            else:
                 # Встановлюємо дефолтні значення, якщо symbol_info ще не отримано
                 logger.warning("Встановлення точності та активів за замовчуванням. Оновлення буде після отримання Symbol Info.")
                 if 'USDT' in self.symbol: self.quote_asset = 'USDT'; self.base_asset = self.symbol.replace('USDT','')
                 elif 'BUSD' in self.symbol: self.quote_asset = 'BUSD'; self.base_asset = self.symbol.replace('BUSD','')
                 # ... додати інші популярні ...
                 else: self.quote_asset = 'QUOTE'; self.base_asset = 'BASE' # Generic fallback
                 self.price_precision = 8 # Default
                 self.qty_precision = 8 # Default


            # Логування
            pos_size_disp = self.position_size_percent * 100
            tp_disp = self.tp_percent * 100
            sl_disp = self.sl_percent * 100
            logger.info(f" Налаштування OK: Mode={self.active_mode}, Sym={self.symbol}, Strat={self.selected_strategy_name}, KlineInterval={self.interval}")
            logger.info(f"  Trading Params: PosSize={pos_size_disp:.1f}%, TP={tp_disp:.2f}%, SL={sl_disp:.2f}%")

        except Exception as e:
            logger.error(f"Помилка завантаження/конвертації налаштувань бота: {e}", exc_info=True)
            raise AsyncBotCoreError(f"Помилка налаштувань бота: {e}") from e

    def _load_strategy(self):
        """Завантажує та ініціалізує обрану стратегію (синхронний метод)."""
        strategy_name = self.selected_strategy_name
        logger.info(f"AsyncBotCore: Завантаження стратегії: {strategy_name}...")
        self._strategy_instance = None # Скидаємо попередню

        if not strategy_name:
            logger.warning("Ім'я стратегії не обрано в конфігурації.")
            # Можливо, варто мати стратегію за замовчуванням або генерувати помилку?
            # Поки що просто виходимо, бот не зможе запуститися без стратегії (перевірка в start)
            return

        try:
            # Завантажуємо доступні стратегії
            available_strategies = load_strategies() # Синхронна функція
            strategy_class = available_strategies.get(strategy_name)

            if strategy_class:
                try:
                    # Створюємо екземпляр, передаючи ConfigManager
                    # Переконуємось, що передаємо актуальний символ
                    strategy_instance = strategy_class(
                        symbol=self.symbol, # Передаємо актуальний символ
                        config_manager=self.config_manager
                        # config=... # Передаємо, якщо потрібно перевизначити для конкретного запуску
                    )
                    self._strategy_instance = strategy_instance
                    logger.info(f"Екземпляр стратегії '{strategy_name}' створено для символу {self.symbol}.")

                    # Визначаємо необхідну довжину історії Klines
                    req_len = getattr(self._strategy_instance, 'required_klines_length', 100)
                    self.required_klines_length = req_len
                    logger.info(f"Встановлено required_klines_length = {self.required_klines_length} для стратегії {strategy_name}.")

                except Exception as e_init:
                    logger.error(f"Помилка ініціалізації екземпляра стратегії '{strategy_name}': {e_init}", exc_info=True)
                    self._strategy_instance = None
            else:
                logger.error(f"Клас стратегії '{strategy_name}' не знайдено.")
                self._strategy_instance = None
        except Exception as e_load:
             logger.error(f"Помилка під час завантаження списку стратегій: {e_load}", exc_info=True)
             self._strategy_instance = None


    def _create_ws_handler(self):
        """Створює екземпляр асинхронного BinanceWebSocketHandler."""
        logger.info(f"AsyncBotCore: Створення обробника WebSocket для {self.symbol} / {self.interval}...")
        # Якщо обробник вже існує (наприклад, при перезавантаженні налаштувань), його треба зупинити
        if self._ws_handler:
            logger.warning("AsyncBotCore: Існуючий WS Handler буде замінено. Зупинка старого...")
            # Потрібно зупинити асинхронно, але цей метод синхронний. Це проблема.
            # TODO: Рефакторити логіку перезавантаження, щоб WS зупинявся/створювався асинхронно.
            # Поки що просто скидаємо посилання.
            # await self._ws_handler.stop() # Так не можна
            self._ws_handler = None

        # Формуємо список потоків
        stream_symbol = self.symbol.lower()
        kline_stream_name = f"{stream_symbol}@kline_{self.interval}"
        # Можна додати інші потоки за потреби (напр., глибина, угоди)
        # depth_stream_name = f"{stream_symbol}@depth5@100ms"
        streams = [kline_stream_name] # Поки що тільки klines

        # Словник асинхронних callback-функцій
        callbacks: Dict[str, Callable] = {
            'kline': self._handle_kline_update_async,
            'executionReport': self._handle_order_update_async, # Назва потоку для ордерів
            'outboundAccountPosition': self._handle_balance_update_async, # Назва потоку для балансів
            # 'trade': self._handle_trade_update_async, # Приклад
            # 'depth': self._handle_depth_update_async, # Приклад
        }

        try:
            # Створюємо екземпляр, передаючи асинхронний API
            ws_handler_instance = BinanceWebSocketHandler(
                streams=streams,
                callbacks=callbacks,
                api=self.binance_api # Передаємо асинхронний API
            )
            self._ws_handler = ws_handler_instance
            logger.info("AsyncWebSocketHandler успішно створено.")
        except Exception as e:
            logger.error(f"Помилка створення BinanceWebSocketHandler: {e}", exc_info=True)
            self._ws_handler = None

    async def _cleanup_resources(self):
        """Допоміжний метод для закриття ресурсів при помилці ініціалізації."""
        logger.debug("AsyncBotCore: Спроба очищення ресурсів...")
        # Зупиняємо обробник команд, якщо він встиг запуститися
        self._stop_command_processor()
        # Закриваємо API та StateManager
        if self.binance_api:
             try: await self.binance_api.close()
             except Exception as e: logger.error(f"Помилка закриття BinanceAPI при очищенні: {e}")
        if self.state_manager:
             try: await self.state_manager.close()
             except Exception as e: logger.error(f"Помилка закриття StateManager при очищенні: {e}")
        logger.debug("AsyncBotCore: Очищення ресурсів завершено.")

# END BLOCK 3

# BLOCK 3.5: WebSocket Callback Handlers and Signal Processing
    def _safe_decimal(self, value: Any, default: Optional[Decimal] = None) -> Optional[Decimal]:
        """Безпечно конвертує значення в Decimal, повертає default або None у разі помилки."""
        if value is None:
            return default
        try:
            return Decimal(str(value))
        except (InvalidOperation, TypeError, ValueError):
            logger.warning(f"Не вдалося конвертувати '{value}' ({type(value)}) в Decimal.")
            return default

    async def _handle_kline_update_async(self, symbol: str, kline_data: dict, event_time: int):
        """ Обробляє дані закритої свічки (kline) з WebSocket. """
        if not self._is_running:
            return
        if symbol != self.symbol:
            return

        kline_start_time = kline_data.get('t')
        is_closed = kline_data.get('x')
        kline_close_price = kline_data.get('c')

        if kline_start_time is None:
            return
        if not is_closed:
            return
        if kline_start_time <= self.last_kline_update_time:
            return

        logger.debug(f"Kline CB: Отримано закриту свічку {symbol} [{self.interval}] StartTime={kline_start_time}, Close={kline_close_price}")
        self.last_kline_update_time = kline_start_time

        try:
            logger.debug(f"Kline CB: Запит {self.required_klines_length + 5} klines для стратегії...")
            df_klines = await self.data_manager.get_klines(
                symbol=self.symbol, interval=self.interval, limit=self.required_klines_length + 5
            )

            if df_klines is None or df_klines.empty:
                return
            if len(df_klines) < self.required_klines_length:
                return
            if self._strategy_instance is None:
                return

            logger.debug(f"Kline CB: Запуск strategy.calculate_signals в екзекуторі...")
            strategy = self._strategy_instance
            loop = asyncio.get_running_loop()
            signal = await loop.run_in_executor(None, strategy.calculate_signals, df_klines.copy())
            logger.debug(f"Kline CB: Результат calculate_signals: {signal}")

            if signal is not None and isinstance(signal, str):
                signal = signal.upper()
                logger.info(f"Kline CB: Стратегія {self.selected_strategy_name} згенерувала сигнал: {signal}")
                await self._process_signal_async(signal)

        except Exception as e:
             logger.error(f"Kline CB: Неочікувана помилка під час обробки kline: {e}", exc_info=True)


    async def _handle_order_update_async(self, order_data: dict, event_time: int):
        """
        Обробляє повідомлення про оновлення статусу ордера (executionReport) з WebSocket.
        Оновлює стан ордерів, позицій, логує угоди та статистику.
        """
        if not self._is_running:
            logger.debug("Order Update CB: Пропуск, бот не запущено.")
            return

        # Оголошуємо змінні заздалегідь
        internal_id = None
        symbol = None
        orderId = None
        clientOrderId = None
        trade_id_for_pnl = None

        try:
            # --- 1. Парсинг executionReport ---
            symbol = order_data.get('s')
            if symbol != self.symbol:
                logger.debug(f"Order Update CB: Отримано оновлення для іншого символу {symbol}. Ігноруємо.")
                return

            orderId = order_data.get('i')
            clientOrderId = order_data.get('c')
            orderListId = order_data.get('g', -1)
            side = order_data.get('S')
            orderType = order_data.get('o')
            orderStatus = order_data.get('X')
            rejectReason = order_data.get('r', 'NONE')

            origQty = self._safe_decimal(order_data.get('q', '0'))
            cumQtyFilled = self._safe_decimal(order_data.get('z', '0'))
            lastExecutedQty = self._safe_decimal(order_data.get('l', '0'))

            price = self._safe_decimal(order_data.get('p', '0'))
            stopPrice = self._safe_decimal(order_data.get('sp', '0'))
            lastPrice = self._safe_decimal(order_data.get('L', '0'))
            avgPrice = self._safe_decimal(order_data.get('ap', '0'))

            commission = self._safe_decimal(order_data.get('n', '0'))
            commissionAsset = order_data.get('N')

            transactTime = self.state_manager._from_db_timestamp(order_data.get('T'))

            if clientOrderId and (clientOrderId.startswith('entry_') or clientOrderId.startswith('exit_') or clientOrderId.startswith('oco_') or clientOrderId.startswith('tp_') or clientOrderId.startswith('sl_')):
                internal_id = clientOrderId
            elif orderId:
                internal_id = str(orderId)
            else:
                logger.error(f"Order Update CB ({symbol}): Неможливо визначити ID ордера з даних: {order_data}")
                return

            logger.info(f"Order Update: {symbol} ID:{internal_id}(BinanceID:{orderId}) Status:{orderStatus} Filled:{cumQtyFilled}/{origQty} LastPx:{lastPrice} LastQty:{lastExecutedQty} AvgPxOrder:{avgPrice}")

            # --- 2. Отримати 'purpose' ордера з БД ---
            order_details_from_db = await self.state_manager.get_active_order(internal_id=internal_id)
            purpose = order_details_from_db.get('purpose', 'UNKNOWN') if order_details_from_db else 'UNKNOWN'
            if not order_details_from_db and orderStatus not in {'NEW'}:
                logger.warning(f"Order Update ({symbol}): Отримано статус '{orderStatus}' для невідстежуваного або вже обробленого ордера ID:{internal_id}. Подальша обробка може бути неточною.")
                purpose = 'UNKNOWN'

            # --- 3. Сформувати дані для оновлення/збереження в БД ---
            db_order_data = {
                'symbol': symbol, 'order_id_binance': orderId, 'client_order_id': clientOrderId,
                'order_list_id_binance': orderListId, 'side': side, 'type': orderType,
                'quantity_req': origQty, 'quantity_filled': cumQtyFilled, 'price': price,
                'stop_price': stopPrice, 'status': orderStatus,
                'purpose': purpose,
                'timestamp': transactTime
            }

            # --- 4. Логування угоди (якщо відбулася) ---
            trade_occurred = (lastExecutedQty is not None and lastExecutedQty > Decimal(0) and
                              orderStatus in {'FILLED', 'PARTIALLY_FILLED'})
            if trade_occurred:
                logger.info(f"Trade Occurred: {side} {lastExecutedQty} {symbol} @ {lastPrice} (Order: {internal_id}) Comm: {commission} {commissionAsset}")
                trade_log_entry = {
                    'timestamp': transactTime, 'symbol': symbol, 'order_id_binance': orderId,
                    'client_order_id': clientOrderId, 'order_list_id_binance': orderListId,
                    'side': side, 'type': orderType, 'status': orderStatus,
                    'price': lastPrice,
                    'quantity': lastExecutedQty,
                    'commission': commission, 'commission_asset': commissionAsset,
                    'purpose': purpose, 'pnl': None
                }
                try:
                    trade_id_for_pnl = await self.state_manager.add_trade_history_entry(trade_log_entry)
                    logger.debug(f"Trade logged for order {internal_id}. Trade DB ID: {trade_id_for_pnl}")
                    self._put_event_sync({'type': 'TRADE_UPDATE', 'payload': {**trade_log_entry, 'trade_id': trade_id_for_pnl}})
                except StateManagerError as e_trade_log:
                     logger.error(f"Failed to log trade for order {internal_id}: {e_trade_log}", exc_info=True)

            # --- 5. Обробка статусів ---
            exit_purposes = {'EXIT_SIGNAL', 'EXIT_MANUAL', 'SL', 'TP', 'EXIT_PROTECTION', 'EXIT_UNKNOWN'}

            if orderStatus == 'FILLED':
                logger.info(f"Order FILLED: {symbol} ID:{internal_id} TotalQty:{cumQtyFilled} AvgPx:{avgPrice} Purpose:{purpose}")
                removed_details = await self.state_manager.remove_active_order(internal_id=internal_id)
                if removed_details:
                    purpose = removed_details.get('purpose', purpose)
                    logger.debug(f"Removed FILLED order {internal_id} from active state.")
                else:
                    logger.warning(f"FILLED order {internal_id} not found in active state for removal (maybe already processed).")

                if purpose == 'ENTRY':
                    existing_pos = await self.state_manager.get_open_position(symbol=self.symbol)
                    if existing_pos is not None:
                         logger.critical(f"CRITICAL ({symbol}): Received FILLED ENTRY order {internal_id}, but position already exists! State: {existing_pos}")
                         self._put_event_sync({'type': 'CRITICAL_ERROR', 'payload': {'reason': f'Position already exists on FILLED ENTRY order {internal_id} for {symbol}'}})
                    else:
                        entry_commission_value = commission
                        entry_commission_asset_value = commissionAsset
                        entry_cost_value = avgPrice * cumQtyFilled if avgPrice and cumQtyFilled else None
                        logger.info(f"Order Update ENTRY FILLED ({internal_id}): Creating position. Entry cost based on order avgPrice ({avgPrice}). Commission based on last fill.")

                        position_data = {
                            'symbol': symbol,
                            'quantity': cumQtyFilled if side == 'BUY' else -cumQtyFilled,
                            'entry_price': avgPrice,
                            'entry_timestamp': transactTime,
                            'entry_order_id_binance': orderId,
                            'entry_client_order_id': clientOrderId,
                            'status': 'OPEN',
                            'entry_cost': entry_cost_value,
                            'entry_commission': entry_commission_value,
                            'entry_commission_asset': entry_commission_asset_value,
                            'oco_list_id': None
                        }
                        await self.state_manager.set_open_position(position_data)
                        logger.info(f"Position OPENED (on Fill): {self.symbol} Qty: {position_data['quantity']}, EntryPx: {avgPrice}")
                        self._put_event_sync({'type': 'POSITION_UPDATE', 'payload': {'status': 'OPEN', 'symbol': symbol, 'details': self._convert_dict_types_for_event(position_data)}})
                        self._put_event_sync({'type': 'NOTIFICATION', 'payload': {'message': f"Position OPENED: {side} {cumQtyFilled:.{self.qty_precision}f} {symbol} @ {avgPrice:.{self.price_precision}f}"}})
                        await self._place_oco_order_async(position_data)

                elif purpose in exit_purposes:
                    logger.info(f"Exit order {internal_id} ({purpose}) FILLED for {symbol}.")
                    pos_before_exit = await self.state_manager.get_open_position(symbol=symbol)
                    if pos_before_exit:
                        pnl, total_commission_quote = self._calculate_trade_pnl(
                            position_data=pos_before_exit,
                            exit_avg_price=avgPrice,
                            exit_filled_qty=cumQtyFilled,
                            exit_commission=commission,
                            exit_commission_asset=commissionAsset
                        )

                        if pnl is None or total_commission_quote is None:
                            logger.error(f"Не вдалося розрахувати PnL/Comm для закритої угоди ордера {internal_id}. Використовуються нульові значення.")
                            pnl = Decimal(0)
                            total_commission_quote = Decimal(0)
                        else:
                            logger.info(f"PnL Calculated (on Fill): Net={pnl:.{self.price_precision}f} {self.quote_asset}, TotalComm={total_commission_quote:.{self.price_precision}f} {self.quote_asset}")

                        if trade_id_for_pnl is not None:
                            try:
                                await self.state_manager.update_trade_pnl(trade_id=trade_id_for_pnl, pnl=pnl)
                                logger.debug(f"Updated PnL={pnl} for trade_id={trade_id_for_pnl}.")
                            except StateManagerError as e_pnl_update:
                                logger.error(f"Failed to update PnL for trade_id={trade_id_for_pnl}: {e_pnl_update}", exc_info=True)
                        else:
                            logger.warning(f"Cannot update PnL in history for FILLED order {internal_id}: trade_id is None (trade logging failed or partial fill).")

                        await self.state_manager.delete_open_position(symbol=symbol)
                        logger.info(f"Position CLOSED for {symbol} (on Fill).")
                        pnl_str = f"{pnl:+.{self.price_precision}f}"
                        self._put_event_sync({'type': 'POSITION_UPDATE', 'payload': {'status': 'CLOSED', 'symbol': symbol, 'pnl': pnl_str}})
                        self._put_event_sync({'type': 'NOTIFICATION', 'payload': {'message': f"Position CLOSED for {symbol}. PnL: {pnl_str} {self.quote_asset}"}})

                        await self._update_session_stats_async(pnl, total_commission_quote)

                        if purpose in {'TP', 'SL'} and orderListId != -1:
                             logger.debug(f"Order {internal_id} ({purpose}) filled as part of OCO {orderListId}. Checking if cancellation needed.")

                    else:
                         logger.warning(f"Exit order {internal_id} ({purpose}) FILLED, but no open position was found for {symbol}! State might be inconsistent.")

                else:
                     logger.warning(f"Order {internal_id} FILLED with unhandled purpose '{purpose}'. Position status might be inconsistent.")

            elif orderStatus in {'CANCELED', 'REJECTED', 'EXPIRED'}:
                log_level = logger.warning if orderStatus == 'CANCELED' else logger.error
                log_level(f"Order {orderStatus}: {symbol} ID:{internal_id} Purpose:{purpose}. Reason: {rejectReason}")
                removed_details = await self.state_manager.remove_active_order(internal_id=internal_id)
                if removed_details:
                    purpose = removed_details.get('purpose', purpose)
                    logger.debug(f"Removed {orderStatus} order {internal_id} from active state.")
                else:
                    logger.warning(f"{orderStatus} order {internal_id} not found in active state for removal (maybe already processed).")

                event_type = 'WARNING' if orderStatus == 'CANCELED' else 'ERROR'
                self._put_event_sync({'type': event_type, 'payload': {'order_id': internal_id, 'status': orderStatus, 'reason': rejectReason, 'purpose': purpose}})

                if purpose in {'TP', 'SL'}:
                    pos = await self.state_manager.get_open_position(symbol=symbol)
                    if pos:
                        logger.critical(f"CRITICAL ({symbol}): Protective order {internal_id} ({purpose}) failed ({orderStatus}) "
                                        f"while position is still OPEN! Initiating emergency exit.")
                        self._put_event_sync({'type': 'CRITICAL_ERROR',
                                              'payload': {'reason': f'Protective order {purpose} failed ({orderStatus}) for open position {symbol}',
                                                          'order_id': internal_id}})
                        await self._initiate_exit_async(reason="Protection Order Failed")
                    else:
                         logger.info(f"Protective order {internal_id} ({purpose}) failed ({orderStatus}), but position for {symbol} is already closed. No action needed.")


            elif orderStatus == 'PARTIALLY_FILLED':
                logger.info(f"Order PARTIALLY_FILLED: {symbol} ID:{internal_id} Filled:{cumQtyFilled}/{origQty} LastQty:{lastExecutedQty} LastPx:{lastPrice} AvgPxOrder:{avgPrice}")
                await self.state_manager.add_or_update_active_order(internal_id=internal_id, order_data=db_order_data)
                logger.debug(f"Updated PARTIALLY_FILLED order {internal_id} in active state.")

                if purpose == 'ENTRY':
                    current_pos = await self.state_manager.get_open_position(self.symbol)
                    if current_pos is not None:
                        logger.info(f"Partial Fill ENTRY ({internal_id}): Оновлення існуючої позиції на основі останньої угоди...")
                        try:
                            original_qty = current_pos.get('quantity') or Decimal(0)
                            original_cost = current_pos.get('entry_cost') or Decimal(0)
                            original_commission = current_pos.get('entry_commission') or Decimal(0)

                            if lastExecutedQty is None or lastPrice is None or lastExecutedQty <= 0 or lastPrice <= 0:
                                 raise ValueError(f"Некоректні дані останньої угоди: Qty={lastExecutedQty}, Px={lastPrice}")

                            new_quantity = original_qty + (lastExecutedQty if side == 'BUY' else -lastExecutedQty)
                            last_trade_cost = lastPrice * lastExecutedQty
                            new_total_cost = original_cost + last_trade_cost

                            new_avg_entry_price = Decimal(0)
                            if new_quantity != 0:
                                new_avg_entry_price = new_total_cost / abs(new_quantity)
                            else:
                                logger.warning(f"Partial Fill ENTRY ({internal_id}): Нова кількість стала нульовою, неможливо розрахувати середню ціну.")

                            new_commission = original_commission + (commission or Decimal(0))
                            logger.warning(f"Partial Fill ENTRY ({internal_id}): Агрегація комісії спрощена (просте додавання) - {original_commission} + {commission or 0} = {new_commission}. Перевірте точність.")
                            new_commission_asset = commissionAsset

                            logger.info(f"Partial Fill ENTRY ({internal_id}): Оновлення полів позиції {self.symbol}...")
                            await self.state_manager.update_open_position_field(self.symbol, 'quantity', new_quantity)
                            await self.state_manager.update_open_position_field(self.symbol, 'entry_price', new_avg_entry_price)
                            await self.state_manager.update_open_position_field(self.symbol, 'entry_cost', new_total_cost)
                            await self.state_manager.update_open_position_field(self.symbol, 'entry_commission', new_commission)
                            await self.state_manager.update_open_position_field(self.symbol, 'entry_commission_asset', new_commission_asset)
                            logger.info(f"Position UPDATED (Partial Fill): {self.symbol} New Qty: {new_quantity}, New Avg Entry Px: {new_avg_entry_price}")

                            updated_pos_data = await self.state_manager.get_open_position(self.symbol)
                            self._put_event_sync({'type': 'POSITION_UPDATE', 'payload': {'status': 'PARTIAL_ENTRY', 'symbol': self.symbol, 'details': self._convert_dict_types_for_event(updated_pos_data)}})

                        except (StateManagerError, ValueError, InvalidOperation, TypeError) as e_upd_partial:
                            logger.error(f"Partial Fill ENTRY ({internal_id}): Помилка оновлення існуючої позиції: {e_upd_partial}", exc_info=True)

                    else:
                        logger.info(f"Partial Fill ENTRY ({internal_id}): Перше виконання, створення позиції...")
                        position_data = {
                            'symbol': symbol,
                            'quantity': cumQtyFilled if side == 'BUY' else -cumQtyFilled,
                            'entry_price': avgPrice,
                            'entry_timestamp': transactTime,
                            'entry_order_id_binance': orderId,
                            'entry_client_order_id': clientOrderId,
                            'status': 'OPEN',
                            'entry_cost': avgPrice * cumQtyFilled if avgPrice and cumQtyFilled else None,
                            'entry_commission': commission,
                            'entry_commission_asset': commissionAsset,
                            'oco_list_id': None
                        }
                        await self.state_manager.set_open_position(position_data)
                        logger.info(f"Position OPENED (Initial Partial Fill): {self.symbol} Qty: {position_data['quantity']}, EntryPx: {avgPrice}")
                        self._put_event_sync({'type': 'POSITION_UPDATE', 'payload': {'status': 'OPEN', 'symbol': symbol, 'details': self._convert_dict_types_for_event(position_data)}})

                elif purpose in exit_purposes:
                    current_pos = await self.state_manager.get_open_position(self.symbol)
                    if current_pos:
                        original_qty = current_pos.get('quantity') or Decimal(0)
                        change_qty = -lastExecutedQty if side == 'SELL' else lastExecutedQty

                        if lastExecutedQty is None or lastExecutedQty <= 0:
                            logger.error(f"Partial Fill EXIT ({internal_id}): Некоректна кількість останньої угоди: {lastExecutedQty}. Оновлення кількості позиції пропущено.")
                        else:
                            new_pos_qty = original_qty + change_qty
                            await self.state_manager.update_open_position_field(
                                symbol=self.symbol,
                                field='quantity',
                                value=new_pos_qty
                            )
                            logger.info(f"Position Quantity UPDATED (Partial Exit Fill): {self.symbol} Old Qty: {original_qty}, Change: {change_qty}, New Qty: {new_pos_qty}")

                            updated_pos_data = await self.state_manager.get_open_position(self.symbol)
                            self._put_event_sync({'type': 'POSITION_UPDATE', 'payload': {'status': 'PARTIAL_EXIT', 'symbol': self.symbol, 'details': self._convert_dict_types_for_event(updated_pos_data)}})
                    else:
                        logger.warning(f"Order Update ({symbol}): Отримано PARTIALLY_FILLED EXIT ордер {internal_id}, але позиція вже закрита.")
                else:
                     logger.warning(f"Order Update ({symbol}): Отримано PARTIALLY_FILLED ордер {internal_id} з невідомим purpose '{purpose}'. Стан позиції не оновлено.")

                filled_percent = (cumQtyFilled / origQty * 100) if origQty else Decimal(0)
                self._put_event_sync({'type': 'NOTIFICATION',
                                      'payload': {'message': f'Order PARTIALLY FILLED: {side} {cumQtyFilled}/{origQty} ({filled_percent:.1f}%) {symbol}', 'order_id': internal_id}})


            elif orderStatus == 'NEW':
                logger.info(f"Order NEW confirmed: {symbol} ID:{internal_id} Type:{orderType} Side:{side} Qty:{origQty}")
                await self.state_manager.add_or_update_active_order(internal_id=internal_id, order_data=db_order_data)
                logger.debug(f"Ensured order {internal_id} (Purpose: {purpose}) is saved in active state.")

            else:
                logger.debug(f"Unhandled order status '{orderStatus}' received for order {internal_id}. Saving state.")
                await self.state_manager.add_or_update_active_order(internal_id=internal_id, order_data=db_order_data)

        except StateManagerError as sme:
             logger.error(f"Order Update CB ({symbol}): Помилка StateManager для ордера {internal_id}: {sme}", exc_info=True)
             self._put_event_sync({'type': 'ERROR', 'payload': {'reason': 'StateManager error in order update handler', 'order_id': internal_id, 'details': str(sme)}})
        except InvalidOperation as ioe:
             logger.error(f"Order Update CB ({symbol}): Помилка конвертації Decimal для ордера {internal_id}: {ioe}", exc_info=True)
             self._put_event_sync({'type': 'ERROR', 'payload': {'reason': 'Decimal conversion error in order update handler', 'order_id': internal_id, 'details': str(ioe)}})
        except ValueError as ve:
             logger.error(f"Order Update CB ({symbol}): Помилка даних для ордера {internal_id}: {ve}", exc_info=True)
             self._put_event_sync({'type': 'ERROR', 'payload': {'reason': 'Data validation error in order update handler', 'order_id': internal_id, 'details': str(ve)}})
        except Exception as e:
             logger.error(f"Order Update CB ({symbol}): Неочікувана помилка для ордера {internal_id}: {e}", exc_info=True)
             self._put_event_sync({'type': 'ERROR', 'payload': {'reason': 'Unexpected error in order update handler', 'order_id': internal_id, 'details': str(e)}})


    async def _handle_balance_update_async(self, balance_data: dict, event_time: int):
        """
        Обробляє повідомлення про оновлення балансу (outboundAccountPosition) з WebSocket.
        """
        if not self._is_running:
            logger.debug("Balance Update CB: Пропуск, бот не запущено.")
            return

        event_type = balance_data.get('e')
        logger.debug(f"Balance Update CB: Отримано подію '{event_type}' о {event_time}. Data: {balance_data}")

        try:
            balances_to_update: Dict[str, Dict[str, Any]] = {}

            for item in balance_data.get('B', []):
                asset = item.get('a')
                free_str = item.get('f')
                locked_str = item.get('l')

                free_decimal = self._safe_decimal(free_str)
                locked_decimal = self._safe_decimal(locked_str)

                if asset and isinstance(asset, str) and free_decimal is not None and locked_decimal is not None:
                    balances_to_update[asset] = {
                        'asset': asset,
                        'free': free_decimal,
                        'locked': locked_decimal
                    }
                    logger.debug(f"Balance Update CB: Підготовлено оновлення для {asset}: free={free_decimal}, locked={locked_decimal}")
                else:
                    logger.warning(f"Balance Update CB: Пропущено некоректний запис балансу: {item}")

            if balances_to_update:
                logger.info(f"Balance Update CB: Надсилаємо оновлення для {len(balances_to_update)} активів в StateManager...")
                try:
                    await self.state_manager.update_balances(balances_to_update)
                    logger.info(f"Balance Update CB: StateManager.update_balances викликано успішно.")

                    payload_for_event = {}
                    for asset, data in balances_to_update.items():
                        free_str_val = self.state_manager._to_db_decimal(data.get('free'))
                        locked_str_val = self.state_manager._to_db_decimal(data.get('locked'))
                        payload_for_event[asset] = {
                            'asset': data['asset'],
                            'free': free_str_val if free_str_val is not None else '0',
                            'locked': locked_str_val if locked_str_val is not None else '0'
                        }

                    self._put_event_sync({'type': 'BALANCE_UPDATE', 'payload': payload_for_event})
                    logger.debug("Balance Update CB: Подію BALANCE_UPDATE надіслано.")

                except AttributeError:
                     logger.error("Balance Update CB: Метод 'update_balances' не знайдено в self.state_manager! Баланси не оновлено.")
                except StateManagerError as sme:
                     logger.error(f"Balance Update CB: Помилка StateManager при оновленні балансів: {sme}", exc_info=True)
                except Exception as e_update:
                     logger.error(f"Balance Update CB: Неочікувана помилка при виклику state_manager.update_balances: {e_update}", exc_info=True)

            else:
                logger.debug("Balance Update CB: Немає коректних даних для оновлення балансів у події.")

        except Exception as e:
            logger.error(f"Balance Update CB: Неочікувана помилка під час обробки даних балансу: {e}", exc_info=True)


    # --- ЗМІНА: Оновлення логіки _process_signal_async ---
    async def _process_signal_async(self, signal: str):
        """ Обробляє торговий сигнал ('BUY', 'CLOSE'), отриманий від стратегії. """
        logger.info(f"Process Signal: Отримано сигнал '{signal}'.")
        if not self._is_running:
            return

        try:
            logger.debug("Process Signal: Отримання поточного стану (позиція, ордери)...")
            current_position = await self.state_manager.get_open_position(symbol=self.symbol)
            active_orders: List[Dict[str, Any]] = await self.state_manager.get_all_active_orders(symbol=self.symbol)

            pending_entry = False
            pending_exit = False
            active_entry_purposes = {'ENTRY'}
            active_exit_purposes = {'TP', 'SL', 'EXIT_MANUAL', 'EXIT_SIGNAL', 'EXIT_PROTECTION', 'EXIT_UNKNOWN'}
            active_statuses = {'NEW', 'PARTIALLY_FILLED'}

            # Шукаємо активні ордери на вхід або вихід
            for order in active_orders:
                order_purpose = order.get('purpose')
                order_status = order.get('status')
                if order_status in active_statuses:
                    if order_purpose in active_entry_purposes:
                        pending_entry = True
                    elif order_purpose in active_exit_purposes:
                        pending_exit = True
                # Можна зупинити пошук раніше, якщо обидва знайдено (хоча це малоймовірно)
                # if pending_entry and pending_exit:
                #     break

            logger.debug(f"Process Signal ({self.symbol}): Стан: Position Active={current_position is not None}, Pending Entry={pending_entry}, Pending Exit={pending_exit}")

            if signal == 'BUY':
                if current_position is not None:
                    logger.info(f"Process Signal ({self.symbol}): Сигнал BUY, але позиція вже відкрита. Ігноруємо.")
                    return
                if pending_entry:
                    logger.info(f"Process Signal ({self.symbol}): Сигнал BUY, але вже є активний ордер на вхід. Ігноруємо.")
                    return
                if pending_exit:
                    logger.warning(f"Process Signal ({self.symbol}): Сигнал BUY, але є активний ордер на ВИХІД ({active_exit_purposes}). Новий вхід блоковано.")
                    return

                logger.info(f"Process Signal ({self.symbol}): Сигнал BUY: Ініціюємо вхід...")
                await self._initiate_entry_async()

            elif signal == 'CLOSE':
                if current_position is None:
                    logger.info(f"Process Signal ({self.symbol}): Сигнал CLOSE, але позиція не відкрита. Ігноруємо.")
                    return
                if pending_exit:
                    logger.info(f"Process Signal ({self.symbol}): Сигнал CLOSE, але вже є активний ордер на вихід. Ігноруємо.")
                    return

                # --- ЗМІНА: Логіка скасування активного входу ---
                if pending_entry:
                    logger.warning(f"Process Signal ({self.symbol}): Сигнал CLOSE, але є активний ордер на ВХІД. Спроба скасування входу...")
                    # Знаходимо конкретний активний ордер на вхід
                    order_to_cancel = None
                    for order in active_orders:
                         order_purpose = order.get('purpose')
                         order_status = order.get('status')
                         if order_purpose == 'ENTRY' and order_status in active_statuses:
                              order_to_cancel = order
                              break # Скасовуємо перший знайдений

                    if order_to_cancel:
                         cancel_success = False
                         order_id_to_cancel = order_to_cancel.get('order_id_binance')
                         client_id_to_cancel = order_to_cancel.get('client_order_id')
                         internal_id_to_remove = client_id_to_cancel if client_id_to_cancel else str(order_id_to_cancel)

                         logger.info(f"Process Signal ({self.symbol}): Спроба скасування ордера на вхід ID:{internal_id_to_remove} (BinanceID: {order_id_to_cancel})...")
                         try:
                             # Визначаємо, що використовувати для скасування: clientOrderId чи orderId
                             cancel_params = {'symbol': self.symbol}
                             if client_id_to_cancel and client_id_to_cancel.startswith('entry_'): # Якщо це наш ID
                                 cancel_params['origClientOrderId'] = client_id_to_cancel
                             elif order_id_to_cancel:
                                 cancel_params['orderId'] = order_id_to_cancel
                             else:
                                 logger.error(f"Process Signal ({self.symbol}): Неможливо визначити ID для скасування ордера: {order_to_cancel}")
                                 raise ValueError("Не вдалося визначити ID ордера для скасування.")

                             cancel_result = await self.binance_api.cancel_order(**cancel_params)
                             if cancel_result: # Припускаємо, що cancel_order повертає dict при успіху, None/Exception при помилці
                                  logger.info(f"Process Signal ({self.symbol}): Активний ордер на вхід ID:{internal_id_to_remove} успішно скасовано.")
                                  cancel_success = True
                                  # Видаляємо з локального стану
                                  await self.state_manager.remove_active_order(internal_id=internal_id_to_remove)
                             else:
                                  # Помилка API скасування (можливо, оброблена як None або виняток)
                                  logger.critical(f"Process Signal ({self.symbol}): Не вдалося скасувати активний ордер на вхід ID:{internal_id_to_remove}! Відповідь API: {cancel_result}")

                         except (BinanceAPIError, ValueError) as e_cancel:
                              logger.critical(f"Process Signal ({self.symbol}): Помилка API/даних при скасуванні ордера на вхід ID:{internal_id_to_remove}: {e_cancel}", exc_info=True)
                         except StateManagerError as e_remove:
                              logger.error(f"Process Signal ({self.symbol}): Помилка видалення скасованого ордера {internal_id_to_remove} з StateManager: {e_remove}", exc_info=True)
                              # Не критично для продовження, але стан БД може бути застарілим

                         # Якщо скасування пройшло успішно, ініціюємо вихід
                         if cancel_success:
                              logger.info(f"Process Signal ({self.symbol}): Ініціюємо вихід ПІСЛЯ скасування активного входу...")
                              await self._initiate_exit_async(reason="Strategy Signal After Entry Cancel")
                         else:
                              # Скасування не вдалося - критична ситуація
                              logger.critical(f"Process Signal ({self.symbol}): Скасування активного входу не вдалося. Вихід НЕ ініційовано для уникнення неконсистентного стану.")
                              self._put_event_sync({'type': 'CRITICAL_ERROR', 'payload': {'reason': f'Failed to cancel ENTRY order {internal_id_to_remove} before exiting {self.symbol}'}})
                         # У будь-якому випадку (успіх чи провал скасування), виходимо з обробки сигналу тут
                         return

                    else:
                         # pending_entry було True, але ордер не знайдено - невідповідність стану
                         logger.warning(f"Process Signal ({self.symbol}): Невідповідність стану! pending_entry=True, але активний ордер ENTRY не знайдено в списку active_orders. Все одно ініціюємо вихід...")
                         # Ініціюємо вихід, як і в стандартному випадку
                         logger.info(f"Process Signal ({self.symbol}): Ініціюємо вихід (після попередження про невідповідність)...")
                         await self._initiate_exit_async(reason="Strategy Signal")
                         return # Виходимо з обробки сигналу

                # --- Кінець ЗМІНИ ---
                else:
                     # Якщо не було pending_entry, виконуємо стандартний вихід
                     logger.info(f"Process Signal ({self.symbol}): Ініціюємо вихід (немає активного входу)...")
                     await self._initiate_exit_async(reason="Strategy Signal")


            else:
                logger.warning(f"Process Signal ({self.symbol}): Отримано невідомий сигнал від стратегії: '{signal}'.")

        except StateManagerError as sme:
             logger.error(f"Process Signal ({self.symbol}): Помилка StateManager при отриманні стану: {sme}", exc_info=True)
        except Exception as e:
            logger.error(f"Process Signal ({self.symbol}): Неочікувана помилка під час обробки сигналу '{signal}': {e}", exc_info=True)

# END BLOCK 3.5
# BLOCK 3.6: Trade Execution Helpers
    async def _initiate_entry_async(self):
        """
        Ініціює процес входу в позицію: розраховує розмір,
        перевіряє правила та розміщує MARKET ордер на купівлю.
        """
        logger.info(f"INITIATE ENTRY ({self.symbol}): Початок процесу ініціації входу...")

        # --- 1. Перевірки ---
        if not self._is_running:
            logger.warning(f"Initiate Entry ({self.symbol}): Пропуск, бот не запущено (_is_running=False).")
            return
        if not self.binance_api or not self.binance_api._initialized:
             logger.error(f"Initiate Entry ({self.symbol}): BinanceAPI відсутній або не ініціалізований. Вхід неможливий.")
             self._put_event_sync({'type': 'ERROR', 'payload': {'reason': f'BinanceAPI not ready for entry ({self.symbol}).'}})
             return

        try:
            # --- 2. Розрахунок розміру позиції ---
            logger.debug(f"Initiate Entry ({self.symbol}): Отримання балансів...")
            balances = await self.state_manager.get_balances()
            quote_asset_balance_data = balances.get(self.quote_asset, {})
            # Використовуємо 'free' баланс
            quote_balance = self._safe_decimal(quote_asset_balance_data.get('free'), Decimal(0))

            logger.info(f"Initiate Entry ({self.symbol}): Доступний баланс {self.quote_asset}: {quote_balance}")
            # Перевіряємо на достатність балансу (можливо, додати мінімальний поріг?)
            if quote_balance <= Decimal('0.00000001'): # Дуже мале значення, щоб уникнути помилок заокруглення
                logger.warning(f"Initiate Entry ({self.symbol}): Недостатній баланс {self.quote_asset} ({quote_balance}). Вхід неможливий.")
                self._put_event_sync({'type': 'TRADE_REJECTED', 'payload': {'symbol': self.symbol, 'reason': 'Insufficient quote balance', 'balance': str(quote_balance)}})
                return

            logger.debug(f"Initiate Entry ({self.symbol}): Отримання поточної ціни...")
            current_price = await self.binance_api.get_current_price(self.symbol)
            if current_price is None or current_price <= Decimal(0):
                logger.error(f"Initiate Entry ({self.symbol}): Не вдалося отримати коректну поточну ціну. Вхід скасовано.")
                self._put_event_sync({'type': 'ERROR', 'payload': {'symbol': self.symbol, 'reason': f'Failed to get current price for {self.symbol}'}})
                return
            logger.debug(f"Initiate Entry ({self.symbol}): Поточна ціна: {current_price}")

            # Використовуємо відсоток від доступного балансу
            quote_amount_to_invest = quote_balance * self.position_size_percent
            # Переконуємось, що не інвестуємо більше, ніж маємо
            quote_amount_to_invest = min(quote_amount_to_invest, quote_balance)
            logger.debug(f"Initiate Entry ({self.symbol}): Сума для інвестиції: {quote_amount_to_invest} {self.quote_asset}")

            # Розрахунок кількості базового активу
            quantity_gross = quote_amount_to_invest / current_price
            logger.debug(f"Initiate Entry ({self.symbol}): Розрахована 'груба' кількість: {quantity_gross} {self.base_asset}")

            # --- 3. Коригування кількості та перевірки ---
            logger.debug(f"Initiate Entry ({self.symbol}): Коригування кількості...")
            adjusted_quantity = await self._adjust_quantity_async(quantity_gross)

            if adjusted_quantity is None or adjusted_quantity <= Decimal(0):
                logger.error(f"Initiate Entry ({self.symbol}): Не вдалося скоригувати кількість ({quantity_gross}) або вона стала нульовою/негативною. Вхід скасовано.")
                self._put_event_sync({'type': 'TRADE_REJECTED', 'payload': {'symbol': self.symbol, 'reason': 'Quantity adjustment failed or resulted in zero', 'gross_qty': str(quantity_gross)}})
                return
            logger.debug(f"Initiate Entry ({self.symbol}): Скоригована кількість: {adjusted_quantity} {self.base_asset}")

            # Перевірка MIN_NOTIONAL (або NOTIONAL для MARKET)
            logger.debug(f"Initiate Entry ({self.symbol}): Перевірка NOTIONAL...")
            passes_notional = await self._check_min_notional_async(adjusted_quantity, current_price)
            if not passes_notional:
                # Логування помилки вже відбувається всередині _check_min_notional_async
                self._put_event_sync({'type': 'TRADE_REJECTED', 'payload': {'symbol': self.symbol, 'reason': 'Failed NOTIONAL check', 'adj_qty': str(adjusted_quantity), 'price': str(current_price)}})
                return
            logger.debug(f"Initiate Entry ({self.symbol}): Перевірку NOTIONAL пройдено.")

            # --- 4. Розміщення ордера ---
            order_type = 'MARKET'
            purpose = 'ENTRY'
            client_order_id = await self._generate_client_order_id_async(prefix=purpose.lower())
            logger.info(f"Initiate Entry ({self.symbol}): Розміщення {order_type} BUY ордера. Qty={adjusted_quantity}, ClientOrderId={client_order_id}")

            params = {
                'symbol': self.symbol,
                'side': 'BUY',
                'type': order_type,
                'quantity': adjusted_quantity, # Передаємо Decimal
                'newClientOrderId': client_order_id
            }

            order_result = await self.binance_api.place_order(**params)

            # --- 5. Обробка результату розміщення ---
            if order_result and isinstance(order_result, dict):
                binance_order_id = order_result.get('orderId')
                status = order_result.get('status')
                # Додаємо PENDING_NEW до можливих статусів, якщо API може так повертати
                accepted_statuses = {'NEW', 'PARTIALLY_FILLED', 'FILLED'}

                if binance_order_id and status in accepted_statuses:
                    logger.info(f"Initiate Entry ({self.symbol}): Ордер успішно розміщено/прийнято біржею. BinanceOrderId: {binance_order_id}, Status: {status}")

                    # --- ВИПРАВЛЕННЯ: Використовуємо метод з state_manager ---
                    transact_time = self.state_manager._from_db_timestamp(order_result.get('transactTime')) or datetime.now(timezone.utc)
                    # --- КІНЕЦЬ ВИПРАВЛЕННЯ ---

                    active_order_details = {
                        'symbol': self.symbol,
                        'order_id_binance': binance_order_id,
                        'client_order_id': client_order_id,
                        'list_client_order_id': order_result.get('listClientOrderId'), # Зазвичай None для простих ордерів
                        'order_list_id_binance': order_result.get('orderListId', -1), # Зазвичай -1 для простих ордерів
                        'side': 'BUY',
                        'type': order_type,
                        'quantity_req': adjusted_quantity, # Використовуємо скориговану кількість
                        'quantity_filled': self._safe_decimal(order_result.get('executedQty', '0')),
                        'price': self._safe_decimal(order_result.get('price', '0')), # Для MARKET може бути 0
                        'stop_price': self._safe_decimal(order_result.get('stopPrice', '0')), # Для MARKET 0
                        'status': status,
                        'purpose': purpose, # Встановлюємо 'ENTRY'
                        'timestamp': transact_time
                    }

                    try:
                        await self.state_manager.add_or_update_active_order(internal_id=client_order_id, order_data=active_order_details)
                        logger.debug(f"Initiate Entry ({self.symbol}): Дані ордера {client_order_id} (BinanceID: {binance_order_id}) збережено в StateManager.")
                    except StateManagerError as sme_save:
                        # КРИТИЧНО: Ордер на біржі, але не збережений локально
                        logger.critical(f"Initiate Entry CRITICAL ERROR ({self.symbol}): Помилка збереження ордера {client_order_id} в StateManager: {sme_save}! Потрібне ручне втручання.", exc_info=True)
                        self._put_event_sync({'type': 'CRITICAL_ERROR', 'payload': {'reason': f'Failed to save placed entry order {client_order_id} locally for {self.symbol}'}})
                        # Можливо, потрібно спробувати скасувати ордер на біржі?

                else:
                    # Ордер відхилено або неочікуваний статус
                    logger.error(f"Initiate Entry ({self.symbol}): Ордер відхилено біржею або отримано неочікуваний статус '{status}'. Response: {order_result}")
                    reject_reason = order_result.get('rejectReason', 'Unknown reason') # Спробувати отримати причину
                    self._put_event_sync({
                        'type': 'TRADE_ERROR',
                        'payload': {'symbol': self.symbol, 'reason': f'Entry order rejected or unexpected status ({status})', 'details': reject_reason or str(order_result)}
                    })
            else:
                 # Не вдалося розмістити ордер (відповідь API була None або не словник)
                 logger.error(f"Initiate Entry ({self.symbol}): Не вдалося розмістити ордер. Відповідь API: {order_result}")
                 self._put_event_sync({
                     'type': 'TRADE_ERROR',
                     'payload': {'symbol': self.symbol, 'reason': 'Failed to place entry order (Invalid API response)', 'details': str(order_result)}
                 })

        except StateManagerError as sme:
             logger.error(f"Initiate Entry ({self.symbol}): Помилка StateManager (ймовірно, при отриманні балансів): {sme}", exc_info=True)
             self._put_event_sync({'type': 'ERROR', 'payload': {'symbol': self.symbol, 'reason': 'StateManager error during entry attempt', 'details': str(sme)}})
        except BinanceAPIError as bae:
             logger.error(f"Initiate Entry ({self.symbol}): Помилка Binance API (ймовірно, при отриманні ціни або розміщенні ордера): {bae}", exc_info=True)
             self._put_event_sync({
                 'type': 'TRADE_ERROR',
                 'payload': {'symbol': self.symbol, 'reason': 'API Error during entry attempt', 'details': str(bae)}
             })
        except InvalidOperation as ioe:
             logger.error(f"Initiate Entry ({self.symbol}): Помилка конвертації Decimal: {ioe}", exc_info=True)
             self._put_event_sync({'type': 'ERROR', 'payload': {'symbol': self.symbol, 'reason': 'Decimal conversion error during entry', 'details': str(ioe)}})
        except Exception as e:
             logger.error(f"Initiate Entry ({self.symbol}): Неочікувана помилка: {e}", exc_info=True)
             self._put_event_sync({
                 'type': 'TRADE_ERROR',
                 'payload': {'symbol': self.symbol, 'reason': 'Unexpected error during entry attempt', 'details': str(e)}
             })

    # --- ЗМІНА: Реалізація _initiate_exit_async ---
    async def _initiate_exit_async(self, reason: str):
        """
        Ініціює процес виходу з позиції: скасовує активні ордери та
        розміщує MARKET ордер на продаж всієї поточної позиції.

        Args:
            reason (str): Причина виходу (напр., "Strategy Signal", "Manual", "OCO Placement Failed").
        """
        logger.info(f"INITIATE EXIT ({self.symbol}): Початок процесу ініціації виходу (Причина: {reason})...")

        # --- 1. Перевірки ---
        if not self._is_running:
            logger.warning(f"Initiate Exit ({self.symbol}, {reason}): Пропуск, бот не запущено (_is_running=False).")
            return
        if not self.binance_api or not self.binance_api._initialized:
             logger.error(f"Initiate Exit ({self.symbol}, {reason}): BinanceAPI відсутній або не ініціалізований. Вихід неможливий.")
             self._put_event_sync({'type': 'ERROR', 'payload': {'reason': f'BinanceAPI not ready for exit ({self.symbol}).'}})
             # Якщо API недоступний, скасувати ордери та вийти неможливо
             return

        try:
            # --- 2. Отримати поточну позицію ---
            logger.debug(f"Initiate Exit ({self.symbol}, {reason}): Запит відкритої позиції...")
            position = await self.state_manager.get_open_position(symbol=self.symbol)

            if position is None:
                logger.warning(f"Initiate Exit ({self.symbol}, {reason}): Не знайдено відкритої позиції. Вихід не потрібен.")
                # Перевірити, чи є активні ордери, які мали б бути скасовані?
                # Можливо, варто скасувати всі ордери, навіть якщо позиції немає.
                logger.info(f"Initiate Exit ({self.symbol}, {reason}): Скасування ВСІХ активних ордерів (навіть без позиції)...")
                await self._cancel_all_active_orders_safely(reason) # Викликаємо безпечне скасування
                return # Немає чого закривати

            # --- 3. Отримати кількість для закриття ---
            try:
                # Беремо абсолютне значення на випадок шортів у майбутньому
                quantity_to_close = abs(self._safe_decimal(position.get('quantity'), Decimal(0)))
            except (ValueError, TypeError, InvalidOperation) as e_qty:
                 # Критична помилка: дані в БД некоректні
                 logger.critical(f"Initiate Exit ({self.symbol}, {reason}): Некоректна кількість ({position.get('quantity')}) в даних позиції! Вихід неможливий.", exc_info=True)
                 self._put_event_sync({'type': 'CRITICAL_ERROR', 'payload': {'reason': f'Invalid position quantity for {self.symbol}', 'details': str(e_qty)}})
                 return # Виходимо, бо не знаємо, скільки продавати

            logger.info(f"Initiate Exit ({self.symbol}, {reason}): Знайдено позицію. Кількість для закриття: {quantity_to_close} {self.base_asset}")

            if quantity_to_close <= Decimal(0):
                 # Критична помилка: стан позиції некоректний
                 logger.critical(f"Initiate Exit ({self.symbol}, {reason}): Нульова або від'ємна кількість ({quantity_to_close}) для закриття позиції! Перевірте логіку оновлення позиції. Вихід неможливий.")
                 self._put_event_sync({'type': 'CRITICAL_ERROR', 'payload': {'reason': f'Zero or negative quantity to close for {self.symbol}', 'quantity': str(quantity_to_close)}})
                 # Скасувати ордери все одно варто
                 await self._cancel_all_active_orders_safely(reason + " (Zero Qty)")
                 return # Виходимо

            # --- 4. Скасувати активні ордери (винесено в окремий метод) ---
            cancel_success = await self._cancel_all_active_orders_safely(reason)
            if not cancel_success:
                # Якщо скасування не вдалося (критична помилка API/неочікувана), вихід неможливий
                logger.critical(f"Initiate Exit ({self.symbol}, {reason}): Вихід припинено через невдале скасування попередніх ордерів.")
                return # Не продовжуємо розміщення ордера на вихід

            # --- 5. Округлення К-сті для Виходу ---
            # Використовуємо _adjust_quantity_async, який вже враховує LOT_SIZE та округлення вниз
            adjusted_quantity = await self._adjust_quantity_async(quantity_to_close)

            # Перевіряємо результат коригування
            if adjusted_quantity is None or adjusted_quantity <= Decimal(0):
                 logger.error(f"Initiate Exit ({self.symbol}, {reason}): Кількість для виходу стала нульовою/некоректною після коригування ({quantity_to_close} -> {adjusted_quantity}). Розміщення ордера скасовано.")
                 self._put_event_sync({'type': 'TRADE_ERROR', 'payload': {'symbol': self.symbol, 'reason': f'Exit quantity zero/invalid after adjustment ({reason})', 'original_qty': str(quantity_to_close)}})
                 # Позиція все ще вважається відкритою в БД, але без ордера на вихід.
                 # Можливо, варто спробувати продати quantity_to_close? Але це може порушити LOT_SIZE.
                 # Найбезпечніше - закрити позицію локально і залогувати невідповідність.
                 logger.warning(f"Initiate Exit ({self.symbol}, {reason}): Позиція закривається локально через неможливість розмістити ордер з нульовою/некоректною кількістю.")
                 await self.state_manager.delete_open_position(symbol=self.symbol) # Закриваємо локально
                 self._put_event_sync({'type': 'POSITION_UPDATE', 'payload': {'status': 'CLOSED_LOCAL_ONLY', 'symbol': self.symbol, 'reason': 'Zero exit quantity'}})
                 return

            logger.debug(f"Initiate Exit ({self.symbol}, {reason}): Кількість для ордера після коригування: {adjusted_quantity}")

            # --- 6. Перевірка NOTIONAL для виходу (на випадок дуже малої позиції) ---
            logger.debug(f"Initiate Exit ({self.symbol}, {reason}): Перевірка NOTIONAL для виходу...")
            # Потрібна поточна ціна для перевірки
            current_price_exit = await self.binance_api.get_current_price(self.symbol)
            if current_price_exit is None or current_price_exit <= Decimal(0):
                logger.error(f"Initiate Exit ({self.symbol}, {reason}): Не вдалося отримати ціну для перевірки NOTIONAL. Розміщення ордера скасовано.")
                # Позиція залишається відкритою, ордери скасовані. КРИТИЧНО.
                self._put_event_sync({'type': 'CRITICAL_ERROR', 'payload': {'symbol': self.symbol, 'reason': f'Failed get price for exit NOTIONAL check ({reason})'}})
                return

            passes_notional_exit = await self._check_min_notional_async(adjusted_quantity, current_price_exit)
            if not passes_notional_exit:
                 # Логування помилки вже всередині _check_min_notional_async
                 self._put_event_sync({'type': 'TRADE_ERROR', 'payload': {'symbol': self.symbol, 'reason': f'Failed NOTIONAL check for exit ({reason})', 'adj_qty': str(adjusted_quantity), 'price': str(current_price_exit)}})
                 # Закриваємо позицію локально
                 logger.warning(f"Initiate Exit ({self.symbol}, {reason}): Позиція закривається локально через провал перевірки NOTIONAL ({adjusted_quantity} * {current_price_exit}).")
                 await self.state_manager.delete_open_position(symbol=self.symbol)
                 self._put_event_sync({'type': 'POSITION_UPDATE', 'payload': {'status': 'CLOSED_LOCAL_ONLY', 'symbol': self.symbol, 'reason': 'Failed exit NOTIONAL check'}})
                 return


            # --- 7. Розміщення ордера на вихід ---
            order_type = 'MARKET'
            # Мапуємо причину виходу на purpose ордера
            purpose_map = {
                "Strategy Signal": "EXIT_SIGNAL",
                "Manual": "EXIT_MANUAL",
                "Protection Order Failed": "EXIT_PROTECTION",
                "OCO Placement Failed": "EXIT_PROTECTION", # Вважаємо захистом
                "OCO Placement Failed API Error": "EXIT_PROTECTION",
                "OCO Calc Failed": "EXIT_PROTECTION",
                "OCO Prep Data Failed": "EXIT_PROTECTION",
                "OCO Qty Adjust Failed": "EXIT_PROTECTION", # Хоча тут ми виходимо раніше
                "OCO TickSize Failed": "EXIT_PROTECTION",
                "OCO PriceFilter Failed": "EXIT_PROTECTION",
                "OCO SymbolInfo Failed": "EXIT_PROTECTION",
                "Inconsistent Entry State": "EXIT_PROTECTION", # Як приклад
                # Додати інші причини за потреби
            }
            exit_purpose = purpose_map.get(reason, "EXIT_UNKNOWN") # Якщо причина невідома
            client_order_id = await self._generate_client_order_id_async(prefix=exit_purpose.lower())

            logger.info(f"Initiate Exit ({self.symbol}, {reason}): Розміщення {order_type} SELL ордера. Qty={adjusted_quantity}, ClientOrderId={client_order_id}, Purpose={exit_purpose}")

            params = {
                'symbol': self.symbol,
                'side': 'SELL',
                'type': order_type,
                'quantity': adjusted_quantity, # Передаємо Decimal
                'newClientOrderId': client_order_id
            }
            order_result = await self.binance_api.place_order(**params)

            # --- 8. Обробка результату розміщення ---
            if order_result and isinstance(order_result, dict):
                binance_order_id = order_result.get('orderId')
                status = order_result.get('status')
                accepted_statuses = {'NEW', 'PARTIALLY_FILLED', 'FILLED'}

                if binance_order_id and status in accepted_statuses:
                    logger.info(f"Initiate Exit ({self.symbol}, {reason}): Ордер на вихід успішно розміщено/прийнято біржею. BinanceOrderId: {binance_order_id}, Status: {status}")

                    # --- ВИПРАВЛЕННЯ: Використовуємо метод з state_manager ---
                    transact_time_exit = self.state_manager._from_db_timestamp(order_result.get('transactTime')) or datetime.now(timezone.utc)
                    # --- КІНЕЦЬ ВИПРАВЛЕННЯ ---

                    active_order_details = {
                        'symbol': self.symbol, 'order_id_binance': binance_order_id, 'client_order_id': client_order_id,
                        'list_client_order_id': order_result.get('listClientOrderId'), 'order_list_id_binance': order_result.get('orderListId', -1),
                        'side': 'SELL', 'type': order_type, 'quantity_req': adjusted_quantity,
                        'quantity_filled': self._safe_decimal(order_result.get('executedQty', '0')),
                        'price': self._safe_decimal(order_result.get('price', '0')),
                        'stop_price': self._safe_decimal(order_result.get('stopPrice', '0')),
                        'status': status, 'purpose': exit_purpose,
                        'timestamp': transact_time_exit
                    }
                    try:
                        await self.state_manager.add_or_update_active_order(internal_id=client_order_id, order_data=active_order_details)
                        logger.debug(f"Initiate Exit ({self.symbol}, {reason}): Дані ордера на вихід {client_order_id} збережено в StateManager.")
                        # Позиція закриється, коли прийде FILLED update для цього ордера через WebSocket
                    except StateManagerError as sme_save:
                         # КРИТИЧНО: Ордер на вихід на біржі, але не збережений локально
                         logger.critical(f"Initiate Exit CRITICAL ERROR ({self.symbol}, {reason}): Помилка збереження ордера на вихід {client_order_id} в StateManager: {sme_save}! Потрібне ручне втручання.", exc_info=True)
                         self._put_event_sync({'type': 'CRITICAL_ERROR', 'payload': {'reason': f'Failed to save placed exit order {client_order_id} locally for {self.symbol}'}})
                         # Що робити? Позиція залишається відкритою локально.

                else:
                    # Ордер відхилено біржею - це КРИТИЧНО, бо ми мали вийти з позиції
                    logger.critical(f"Initiate Exit ({self.symbol}, {reason}): Ордер на вихід ВІДХИЛЕНО біржею або невідомий статус '{status}'! Response: {order_result}")
                    reject_reason_exit = order_result.get('rejectReason', 'Unknown reason')
                    self._put_event_sync({'type': 'CRITICAL_ERROR', 'payload': {'symbol': self.symbol, 'reason': f'Exit order rejected by exchange ({reason}, status: {status})', 'details': reject_reason_exit or str(order_result)}})
                    # Позиція залишається відкритою, ордери скасовані. Потрібне втручання.

            else:
                # Помилка API при розміщенні - це КРИТИЧНО
                logger.critical(f"Initiate Exit ({self.symbol}, {reason}): КРИТИЧНА ПОМИЛКА API при розміщенні ордера на вихід! Response: {order_result}")
                self._put_event_sync({'type': 'CRITICAL_ERROR', 'payload': {'symbol': self.symbol, 'reason': f'API error placing exit order ({reason})', 'details': str(order_result)}})
                # Позиція залишається відкритою, ордери скасовані. Потрібне втручання.


        # Обробка винятків з усього процесу
        except StateManagerError as sme:
             logger.error(f"Initiate Exit ({self.symbol}, {reason}): Помилка StateManager: {sme}", exc_info=True)
             # Якщо помилка сталася до скасування ордерів або розміщення ордера на вихід, це менш критично
             self._put_event_sync({'type': 'ERROR', 'payload': {'symbol': self.symbol, 'reason': f'StateManager error during exit ({reason})', 'details': str(sme)}})
        except BinanceAPIError as bae:
             # Може бути викликано при place_order або get_current_price, якщо не оброблено вище
             logger.critical(f"Initiate Exit ({self.symbol}, {reason}): КРИТИЧНА неперехоплена помилка Binance API: {bae}", exc_info=True)
             self._put_event_sync({'type': 'CRITICAL_ERROR', 'payload': {'symbol': self.symbol, 'reason': f'Unhandled API Error during exit ({reason})', 'details': str(bae)}})
        except InvalidOperation as ioe:
             logger.error(f"Initiate Exit ({self.symbol}, {reason}): Помилка конвертації Decimal: {ioe}", exc_info=True)
             self._put_event_sync({'type': 'ERROR', 'payload': {'symbol': self.symbol, 'reason': f'Decimal conversion error during exit ({reason})', 'details': str(ioe)}})
        except Exception as e:
             # Неочікувана помилка - КРИТИЧНО
             logger.critical(f"Initiate Exit ({self.symbol}, {reason}): КРИТИЧНА НЕОЧІКУВАНА ПОМИЛКА: {e}", exc_info=True)
             self._put_event_sync({'type': 'CRITICAL_ERROR', 'payload': {'symbol': self.symbol, 'reason': f'Unexpected critical error during exit ({reason})', 'details': str(e)}})
    # --- КІНЕЦЬ ЗМІНИ ---

    # --- Допоміжний метод для безпечного скасування ордерів ---
    async def _cancel_all_active_orders_safely(self, reason_prefix: str) -> bool:
        """
        Безпечно скасовує всі активні ордери для self.symbol.
        Обробляє помилки API та StateManager.

        Args:
            reason_prefix (str): Префікс причини для логування.

        Returns:
            bool: True, якщо операція пройшла успішно або без критичних помилок API,
                  False, якщо сталася критична помилка API, яка перешкоджає подальшим діям.
        """
        logger.info(f"_cancel_all_active_orders_safely ({self.symbol}, Reason: {reason_prefix}): Скасування активних ордерів...")
        cancel_success = False # Прапорець успішного *запиту* на скасування
        critical_api_error = False # Прапорець критичної помилки API

        try:
            # Припускаємо, що cancel_all_orders повертає список скасованих або [] при успіху,
            # і None при критичній помилці API (інші помилки обробляються як винятки BinanceAPIError)
            cancel_result = await self.binance_api.cancel_all_orders(symbol=self.symbol)

            if cancel_result is None:
                # Ситуація, коли сам метод cancel_all_orders сигналізує про непереборну помилку
                logger.critical(f"_cancel_all_active_orders_safely ({self.symbol}, {reason_prefix}): КРИТИЧНА ПОМИЛКА під час виклику скасування ордерів!")
                self._put_event_sync({'type': 'CRITICAL_ERROR', 'payload': {'reason': f'Critical failure during order cancellation call ({reason_prefix}) for {self.symbol}'}})
                critical_api_error = True # Встановлюємо прапорець
            else:
                logger.info(f"_cancel_all_active_orders_safely ({self.symbol}, {reason_prefix}): Успішно надіслано запит на скасування {len(cancel_result)} активних ордерів.")
                cancel_success = True # Вважаємо, що запит на скасування пройшов

        except BinanceAPIError as bae_cancel:
            # Помилка API під час скасування (напр., таймаут, проблеми з'єднання)
            logger.critical(f"_cancel_all_active_orders_safely ({self.symbol}, {reason_prefix}): КРИТИЧНА ПОМИЛКА API під час скасування ордерів: {bae_cancel}!", exc_info=True)
            self._put_event_sync({'type': 'CRITICAL_ERROR', 'payload': {'reason': f'API error cancelling orders ({reason_prefix}) for {self.symbol}', 'details': str(bae_cancel)}})
            critical_api_error = True # Встановлюємо прапорець
        except Exception as e_cancel:
            # Неочікувана помилка
            logger.critical(f"_cancel_all_active_orders_safely ({self.symbol}, {reason_prefix}): КРИТИЧНА НЕОЧІКУВАНА ПОМИЛКА під час скасування ордерів: {e_cancel}!", exc_info=True)
            self._put_event_sync({'type': 'CRITICAL_ERROR', 'payload': {'reason': f'Unexpected error cancelling orders ({reason_prefix}) for {self.symbol}', 'details': str(e_cancel)}})
            critical_api_error = True # Розглядаємо як критичну

        # Очищаємо локальний стан ТІЛЬКИ ЯКЩО НЕ БУЛО КРИТИЧНОЇ ПОМИЛКИ API
        if not critical_api_error:
            try:
                active_orders_before_clear = await self.state_manager.get_all_active_orders(symbol=self.symbol)
                if active_orders_before_clear:
                     count_before = len(active_orders_before_clear)
                     await self.state_manager.clear_active_orders_for_symbol(symbol=self.symbol)
                     logger.debug(f"_cancel_all_active_orders_safely ({self.symbol}, {reason_prefix}): Локальний стан {count_before} активних ордерів очищено.")
                else:
                     logger.debug(f"_cancel_all_active_orders_safely ({self.symbol}, {reason_prefix}): Немає активних ордерів для очищення в локальному стані.")
            except StateManagerError as sme_clear:
                # Не змогли очистити локальний стан - не критично для виходу, але варто залогувати
                logger.error(f"_cancel_all_active_orders_safely ({self.symbol}, {reason_prefix}): Помилка очищення локальних ордерів у StateManager після скасування: {sme_clear}. Продовжуємо...", exc_info=True)
                # Не змінюємо cancel_success або critical_api_error через це

        # Повертаємо True, якщо не було КРИТИЧНОЇ помилки API
        return not critical_api_error

# END BLOCK 3.6

# BLOCK 3.7: Entry Calculation Helpers
    async def _adjust_quantity_async(self, quantity: Decimal) -> Optional[Decimal]:
        """
        Коригує кількість згідно з правилом кроку лоту (LOT_SIZE filter) для символу.
        Округлення відбувається ВНИЗ (floor).

        Args:
            quantity (Decimal): "Груба" розрахована кількість.

        Returns:
            Optional[Decimal]: Скоригована кількість або None у разі помилки
                               (не вдалося отримати правила, кількість < minQty).
        """
        logger.debug(f"_adjust_quantity_async: Початкова кількість: {quantity}")
        symbol_info = await self._get_symbol_info_async()
        if symbol_info is None:
            logger.error("_adjust_quantity_async: Не вдалося отримати symbol_info. Неможливо скоригувати кількість.")
            return None

        try:
            lot_size_filter = None
            # Знаходимо фільтр LOT_SIZE
            for f in symbol_info.get('filters', []):
                if f.get('filterType') == 'LOT_SIZE':
                    lot_size_filter = f
                    break

            if not lot_size_filter:
                logger.warning(f"_adjust_quantity_async: Не знайдено фільтр LOT_SIZE для {self.symbol}. "
                               f"Буде використано округлення до self.qty_precision={self.qty_precision}.")
                # Використовуємо стандартне округлення вниз до заданої точності
                decimal_places = Decimal('1e-' + str(self.qty_precision))
                adjusted = quantity.quantize(decimal_places, rounding='ROUND_DOWN')
                logger.debug(f"_adjust_quantity_async (Fallback): {quantity} -> {adjusted}")
                # Перевіряємо чи не стало нулем
                return adjusted if adjusted > Decimal(0) else None

            # Отримуємо параметри фільтра LOT_SIZE
            minQty_str = lot_size_filter.get('minQty')
            maxQty_str = lot_size_filter.get('maxQty')
            stepSize_str = lot_size_filter.get('stepSize')

            if not all([minQty_str, maxQty_str, stepSize_str]):
                 logger.error(f"_adjust_quantity_async: Неповні дані у фільтрі LOT_SIZE для {self.symbol}: {lot_size_filter}")
                 return None

            # Конвертуємо в Decimal
            minQty = Decimal(minQty_str)
            maxQty = Decimal(maxQty_str)
            stepSize = Decimal(stepSize_str)

            logger.debug(f"_adjust_quantity_async: Правила LOT_SIZE: min={minQty}, max={maxQty}, step={stepSize}")

            # 1. Перевірка minQty (попередньо)
            if quantity < minQty:
                logger.warning(f"_adjust_quantity_async: Початкова кількість {quantity} менша за minQty ({minQty}).")
                # Повертаємо None, бо навіть після округлення вона може не пройти
                return None

            # 2. Обмеження maxQty
            if quantity > maxQty:
                 logger.debug(f"_adjust_quantity_async: Кількість {quantity} обрізана до maxQty ({maxQty}).")
                 quantity = maxQty # Обрізаємо зверху

            # 3. Застосування stepSize (округлення вниз)
            if stepSize > Decimal(0):
                # Використовуємо ділення націло для Decimal
                adjusted = (quantity // stepSize) * stepSize
                logger.debug(f"_adjust_quantity_async: Після застосування stepSize={stepSize}: {quantity} -> {adjusted}")
            else:
                # Якщо stepSize = 0 (малоймовірно), не застосовуємо крок
                logger.warning(f"_adjust_quantity_async: stepSize для {self.symbol} дорівнює 0. Крок не застосовано.")
                adjusted = quantity

            # 4. Фінальна перевірка minQty (після округлення)
            if adjusted < minQty:
                logger.warning(f"_adjust_quantity_async: Скоригована кількість {adjusted} стала меншою за minQty ({minQty}).")
                return None # Не пройшла мінімальний лот

            logger.info(f"_adjust_quantity_async: Фінальна скоригована кількість: {adjusted}")
            return adjusted

        except (InvalidOperation, ValueError) as e:
            logger.error(f"_adjust_quantity_async: Помилка конвертації правил LOT_SIZE для {self.symbol}: {e}", exc_info=True)
            return None
        except Exception as e:
            logger.error(f"_adjust_quantity_async: Неочікувана помилка при коригуванні кількості: {e}", exc_info=True)
            return None

    # --- ЗМІНА: Реалізація _check_min_notional_async ---
    async def _check_min_notional_async(self, quantity: Decimal, price: Decimal) -> bool:
        """
        Перевіряє, чи проходить вартість ордера правило NOTIONAL (для MARKET ордерів).

        Args:
            quantity (Decimal): Скоригована кількість базового активу.
            price (Decimal): Поточна ринкова ціна ордера.

        Returns:
            bool: True, якщо перевірку пройдено (або фільтр не знайдено), інакше False.
        """
        logger.debug(f"_check_min_notional_async: Перевірка для Qty={quantity}, Price={price}")

        symbol_info = await self._get_symbol_info_async()
        if symbol_info is None:
            logger.error("_check_min_notional_async: Не вдалося отримати symbol_info. Неможливо виконати перевірку.")
            return False # Не можемо перевірити, вважаємо невдачею

        try:
            notional_filter = None
            filter_type_to_check = 'NOTIONAL' # Специфічно для MARKET ордерів

            for f in symbol_info.get('filters', []):
                if f.get('filterType') == filter_type_to_check:
                    notional_filter = f
                    break

            if not notional_filter:
                logger.warning(f"_check_min_notional_async: Не знайдено фільтр '{filter_type_to_check}' для {self.symbol}. "
                               "Перевірка вважається пройденою.")
                return True # Якщо правила немає, вважаємо, що все ОК

            # Отримуємо значення minNotional з фільтра
            minNotional_str = notional_filter.get('minNotional')
            if minNotional_str is None:
                 logger.error(f"_check_min_notional_async: Відсутній ключ 'minNotional' у фільтрі '{filter_type_to_check}' для {self.symbol}: {notional_filter}")
                 return False

            # Конвертуємо рядок в Decimal
            minNotional = Decimal(minNotional_str)
            logger.debug(f"_check_min_notional_async: Правило {filter_type_to_check}: minNotional={minNotional} {self.quote_asset}") # Припускаємо, що в quote asset

            # Перевірка валідності вхідних даних (чи вони Decimal та позитивні)
            if not isinstance(quantity, Decimal) or quantity <= Decimal(0):
                 logger.error(f"_check_min_notional_async: Некоректна кількість для перевірки: {quantity} ({type(quantity)})")
                 return False
            if not isinstance(price, Decimal) or price <= Decimal(0):
                 logger.error(f"_check_min_notional_async: Некоректна ціна для перевірки: {price} ({type(price)})")
                 return False

            # Розрахунок номінальної вартості ордера
            notional_value = quantity * price
            logger.debug(f"_check_min_notional_async: Розрахована номінальна вартість: {notional_value} {self.quote_asset}")

            # Порівняння з мінімальним значенням
            passes = notional_value >= minNotional

            # Логування результату
            if passes:
                logger.debug(f"_check_min_notional_async: Перевірку {filter_type_to_check} пройдено ({notional_value} >= {minNotional}).")
            else:
                logger.warning(f"_check_min_notional_async: Перевірку {filter_type_to_check} НЕ пройдено! "
                               f"Вартість ордера ({notional_value}) < MinNotional ({minNotional}).")

            return passes # Повертаємо результат перевірки

        except (InvalidOperation, ValueError) as e:
            logger.error(f"_check_min_notional_async: Помилка конвертації minNotional '{minNotional_str}' для {self.symbol}: {e}", exc_info=True)
            return False # Помилка конвертації - не можемо перевірити
        except Exception as e:
            logger.error(f"_check_min_notional_async: Неочікувана помилка під час перевірки: {e}", exc_info=True)
            return False # Інша помилка - не можемо перевірити
    # --- КІНЕЦЬ ЗМІНИ ---

    async def _generate_client_order_id_async(self, prefix: str) -> str:
        """
        Генерує унікальний clientOrderId для ордера.
        (Заглушка - можна залишити синхронною або зробити асинхронною за потреби)
        """
        logger.warning("Виклик заглушки _generate_client_order_id_async")
        # Поки що проста реалізація на основі часу
        now_ms = int(time.time() * 1000)
        client_id = f"{prefix}_{now_ms}"[-32:] # Обмеження довжини (приклад)
        logger.debug(f"_generate_client_order_id_async (stub): prefix={prefix}, generated_id={client_id}")
        return client_id
# END BLOCK 3.7
# BLOCK 3.8: Symbol Info Helper
    async def _get_symbol_info_async(self) -> Optional[Dict[str, Any]]:
        """
        Асинхронно отримує інформацію про символ (з кешу або API).
        Оновлює атрибути точності та активів при першому отриманні.

        Returns:
            Optional[Dict[str, Any]]: Словник з інформацією про символ або None у разі помилки.
        """
        # 1. Перевірка кешу
        if self._symbol_info is not None:
            logger.debug(f"_get_symbol_info_async: Інформація про символ {self.symbol} взята з кешу.")
            return self._symbol_info

        # 2. Отримання з API (якщо кеш порожній)
        logger.info(f"_get_symbol_info_async: Запит інформації про символ {self.symbol} з API...")
        if not self.binance_api or not self.binance_api._initialized:
             logger.error("_get_symbol_info_async: BinanceAPI відсутній або не ініціалізований.")
             return None

        try:
            symbol_info = await self.binance_api.get_symbol_info(self.symbol)

            if symbol_info and isinstance(symbol_info, dict):
                logger.info(f"_get_symbol_info_async: Отримано інформацію для {self.symbol}.")
                # Зберігаємо в кеш
                self._symbol_info = symbol_info

                # --- Оновлюємо атрибути класу ---
                try:
                    # Використовуємо значення з symbol_info, якщо вони є, інакше залишаємо поточні
                    self.price_precision = int(symbol_info.get('quotePrecision', self.price_precision))
                    self.qty_precision = int(symbol_info.get('baseAssetPrecision', self.qty_precision))
                    self.quote_asset = symbol_info.get('quoteAsset', self.quote_asset)
                    self.base_asset = symbol_info.get('baseAsset', self.base_asset)
                    logger.info(f"_get_symbol_info_async: Оновлено параметри символу з API: "
                                f"Prec P:{self.price_precision}/Q:{self.qty_precision}, "
                                f"Assets B:{self.base_asset}/Q:{self.quote_asset}")
                except (ValueError, TypeError) as e:
                    logger.error(f"_get_symbol_info_async: Помилка оновлення атрибутів точності/активів з symbol_info: {e}", exc_info=True)
                    # Не перериваємо роботу, атрибути залишаться зі старими значеннями
                # --- Кінець оновлення атрибутів ---

                return self._symbol_info # Повертаємо щойно отримані дані
            else:
                logger.error(f"_get_symbol_info_async: Не вдалося отримати або некоректний формат symbol_info для {self.symbol}. Відповідь: {symbol_info}")
                return None

        except BinanceAPIError as bae:
            logger.error(f"_get_symbol_info_async: Помилка API при отриманні symbol_info для {self.symbol}: {bae}", exc_info=True)
            return None
        except Exception as e:
            logger.error(f"_get_symbol_info_async: Неочікувана помилка при отриманні symbol_info для {self.symbol}: {e}", exc_info=True)
            return None

# END BLOCK 3.8
# BLOCK 3.9: OCO, Statistics, and Other Helper Placeholders
    # --- ЗМІНА: Реалізація _place_oco_order_async ---
    async def _place_oco_order_async(self, position_data: dict):
        """
        Розраховує ціни TP/SL та розміщує OCO (One-Cancels-the-Other) ордер SELL
        для захисту відкритої позиції.

        Args:
            position_data (dict): Дані про щойно відкриту/оновлену позицію
                                    (має містити 'symbol', 'quantity', 'entry_price').
        """
        symbol = position_data.get('symbol') # Отримуємо символ на початку для логування
        logger.info(f"OCO Placement: Початок розміщення OCO для {symbol}...")

        # --- 1. Перевірки ---
        if not self._is_running:
            logger.warning(f"OCO Placement: Пропуск для {symbol}, бот не запущено.")
            return
        if not self.binance_api or not self.binance_api._initialized:
            logger.error(f"OCO Placement: BinanceAPI відсутній або не ініціалізований для {symbol}. OCO не розміщено.")
            # Немає сенсу викликати exit, якщо API недоступний
            return
        if not (self.tp_percent > 0 and self.sl_percent > 0):
            logger.warning(f"OCO Placement: Не розміщується для {symbol}, оскільки TP ({self.tp_percent:.2%}) або SL ({self.sl_percent:.2%}) не увімкнено (> 0).")
            return

        # Витягуємо та валідуємо дані позиції
        try:
            quantity_abs = abs(self._safe_decimal(position_data.get('quantity'), Decimal(0))) # Беремо abs і перевіряємо > 0
            entry_price = self._safe_decimal(position_data.get('entry_price'))

            if symbol is None or quantity_abs <= Decimal(0) or entry_price is None or entry_price <= Decimal(0):
                 raise ValueError(f"Відсутні або некоректні дані позиції (symbol='{symbol}', quantity='{position_data.get('quantity')}', entry_price='{position_data.get('entry_price')}').")
        except (ValueError, TypeError, InvalidOperation) as e_val:
            logger.error(f"OCO Placement: Некоректні вхідні дані позиції для {symbol}: {e_val}")
            return # Виходимо, якщо дані позиції невалідні

        try:
            # --- 2. Отримання Symbol Info та Tick Size ---
            symbol_info = await self._get_symbol_info_async()
            if symbol_info is None:
                logger.error(f"OCO Placement: Не вдалося отримати symbol_info для {symbol}. Неможливо розрахувати/розмістити OCO.")
                # Розглянути аварійний вихід, якщо це критично?
                # await self._initiate_exit_async(reason="OCO SymbolInfo Failed") # Поки не викликаємо вихід
                return

            price_filter = next((f for f in symbol_info.get('filters', []) if f.get('filterType') == 'PRICE_FILTER'), None)
            if not price_filter:
                logger.error(f"OCO Placement: Не знайдено PRICE_FILTER для {symbol}. OCO не розміщено.")
                # await self._initiate_exit_async(reason="OCO PriceFilter Failed") # Поки не викликаємо вихід
                return

            tickSize_str = price_filter.get('tickSize')
            tickSize = self._safe_decimal(tickSize_str)
            if tickSize is None or tickSize <= Decimal(0):
                logger.error(f"OCO Placement: Некоректний tickSize '{tickSize_str}' для {symbol}. OCO не розміщено.")
                # await self._initiate_exit_async(reason="OCO TickSize Failed") # Поки не викликаємо вихід
                return
            logger.debug(f"OCO Placement: TickSize для {symbol}: {tickSize}")

            # --- 3. Розрахунок та Округлення цін TP/SL ---
            tp_price_raw = entry_price * (Decimal(1) + self.tp_percent)
            sl_price_raw = entry_price * (Decimal(1) - self.sl_percent)

            # Округлення TP ВНИЗ (бо це ціна SELL ордера)
            tp_price = (tp_price_raw / tickSize).quantize(Decimal('0'), rounding='ROUND_DOWN') * tickSize
            # Округлення SL Trigger ВГОРУ (щоб спрацював раніше при падінні ціни)
            sl_trigger_price = (sl_price_raw / tickSize).quantize(Decimal('0'), rounding='ROUND_UP') * tickSize
            # SL Limit Price трохи нижче тригера
            sl_limit_price = sl_trigger_price - tickSize

            # Перевірка, щоб SL Limit не став нульовим або від'ємним
            if sl_limit_price <= Decimal(0):
                sl_limit_price = tickSize # Встановлюємо мінімально можливу ціну
                logger.warning(f"OCO Placement: Розрахована SL Limit ціна була <= 0 для {symbol}, скориговано до tickSize: {sl_limit_price}")

            logger.debug(f"OCO Placement: Розраховані ціни для {symbol}: TP={tp_price}, SL_Trig={sl_trigger_price}, SL_Lim={sl_limit_price} (Entry={entry_price})")

            # --- 4. Валідація розрахованих цін ---
            if not (tp_price > entry_price):
                logger.critical(f"OCO Placement Error ({symbol}): Розрахована ціна TP ({tp_price}) не вища за ціну входу ({entry_price})!")
                self._put_event_sync({'type': 'CRITICAL_ERROR', 'payload': {'reason': f'Invalid TP price calculation for {symbol}'}})
                # await self._initiate_exit_async(reason="OCO TP Price Calc Failed") # Можливо, потрібен вихід
                return
            if not (sl_trigger_price < entry_price):
                logger.critical(f"OCO Placement Error ({symbol}): Розрахована ціна SL Trigger ({sl_trigger_price}) не нижча за ціну входу ({entry_price})!")
                self._put_event_sync({'type': 'CRITICAL_ERROR', 'payload': {'reason': f'Invalid SL Trigger price calculation for {symbol}'}})
                # await self._initiate_exit_async(reason="OCO SL Trig Price Calc Failed") # Можливо, потрібен вихід
                return
            if not (sl_limit_price < sl_trigger_price):
                # Допускаємо рівність, якщо sl_trigger_price сам по собі є tickSize
                if sl_trigger_price > tickSize:
                    logger.critical(f"OCO Placement Error ({symbol}): Розрахована ціна SL Limit ({sl_limit_price}) не нижча за SL Trigger ({sl_trigger_price})!")
                    self._put_event_sync({'type': 'CRITICAL_ERROR', 'payload': {'reason': f'Invalid SL Limit price calculation for {symbol}'}})
                    # await self._initiate_exit_async(reason="OCO SL Lim Price Calc Failed") # Можливо, потрібен вихід
                    return
                else:
                     logger.warning(f"OCO Placement ({symbol}): SL Limit ({sl_limit_price}) дорівнює SL Trigger ({sl_trigger_price}), ймовірно через низьку ціну входу або великий tickSize.")


            # --- 5. Коригування кількості ---
            # Використовуємо абсолютну кількість з позиції
            adjusted_quantity = await self._adjust_quantity_async(quantity_abs)
            if adjusted_quantity is None or adjusted_quantity <= Decimal(0):
                logger.error(f"OCO Placement ({symbol}): Не вдалося скоригувати кількість ({quantity_abs}) для OCO ордера або вона стала нульовою. OCO не розміщено.")
                # await self._initiate_exit_async(reason="OCO Qty Adjust Failed") # Можливо, потрібен вихід
                return

            # --- 6. Формування параметрів та Розміщення OCO ---
            listClientOrderId = await self._generate_client_order_id_async(prefix='oco')
            params = {
                'symbol': self.symbol,
                'side': 'SELL', # OCO для закриття LONG позиції
                'quantity': adjusted_quantity, # Передаємо Decimal
                'price': tp_price, # Ціна для LIMIT ордера (Take Profit)
                'stopPrice': sl_trigger_price, # Ціна активації для STOP ордера
                'stopLimitPrice': sl_limit_price, # Лімітна ціна для STOP ордера
                'stopLimitTimeInForce': 'GTC', # Good-Til-Canceled для стоп-ліміт частини
                'listClientOrderId': listClientOrderId # Наш ID для OCO списку
            }

            logger.info(f"OCO Placement: Розміщення OCO SELL ордера для {symbol}. Params: {params}")

            oco_result = await self.binance_api.place_oco_order(**params)

            # --- 7. Обробка результату OCO ---
            if oco_result and isinstance(oco_result, dict) and oco_result.get('orderListId', -1) != -1:
                orderListId = oco_result.get('orderListId')
                returnedListId = oco_result.get('listClientOrderId', listClientOrderId)
                logger.info(f"OCO Placement: OCO ордер успішно розміщено для {symbol}. OrderListId: {orderListId}, ListClientOrderId: {returnedListId}")

                try:
                    oco_id_to_save = str(orderListId)
                    await self.state_manager.update_open_position_field(symbol=symbol, field='oco_list_id', value=oco_id_to_save)
                    logger.debug(f"OCO Placement: Збережено oco_list_id={oco_id_to_save} для позиції {symbol}.")
                except StateManagerError as e_pos_update:
                    logger.critical(f"OCO Placement CRITICAL ERROR ({symbol}): Не вдалося оновити позицію з oco_list_id={oco_id_to_save}: {e_pos_update}! Потрібне ручне втручання або логіка відновлення.", exc_info=True)
                    self._put_event_sync({'type': 'CRITICAL_ERROR', 'payload': {'reason': f'Failed to save OCO ID {oco_id_to_save} to position {symbol}', 'details': str(e_pos_update)}})

                orderReports = oco_result.get('orderReports', [])
                if len(orderReports) == 2:
                    for report in orderReports:
                        try:
                            report_orderId = report.get('orderId')
                            report_clientOrderId = report.get('clientOrderId')
                            internal_report_id = report_clientOrderId if report_clientOrderId else str(report_orderId)

                            report_type = report.get('type')
                            report_status = report.get('status')
                            report_transactTime = self.state_manager._from_db_timestamp(report.get('transactTime')) or datetime.now(timezone.utc)

                            purpose = 'UNKNOWN_OCO_PART'
                            if report_type == 'LIMIT_MAKER':
                                purpose = 'TP'
                            elif report_type == 'STOP_LOSS_LIMIT':
                                purpose = 'SL'

                            if report_orderId is None or internal_report_id is None:
                                logger.error(f"OCO Placement ({symbol}): Не вдалося отримати orderId або визначити internal_id зі звіту OCO: {report}")
                                continue

                            order_details = {
                                'symbol': report.get('symbol'),
                                'order_id_binance': report_orderId,
                                'client_order_id': report_clientOrderId,
                                'order_list_id_binance': orderListId,
                                'list_client_order_id': returnedListId,
                                'side': report.get('side'),
                                'type': report_type,
                                'quantity_req': self._safe_decimal(report.get('origQty')),
                                'quantity_filled': self._safe_decimal(report.get('executedQty')),
                                'price': self._safe_decimal(report.get('price')),
                                'stop_price': self._safe_decimal(report.get('stopPrice')),
                                'status': report_status,
                                'purpose': purpose,
                                'timestamp': report_transactTime
                            }
                            await self.state_manager.add_or_update_active_order(internal_id=internal_report_id, order_data=order_details)
                            logger.info(f"OCO Placement: Збережено {purpose} ордер ID:{internal_report_id}(BinanceID:{report_orderId}) для {symbol} зі статусом {report_status}.")

                        except StateManagerError as sme_report:
                             logger.critical(f"OCO Placement CRITICAL ERROR ({symbol}): Помилка StateManager при збереженні звіту OCO ордера ID:{internal_report_id}: {sme_report}! Потрібне ручне втручання.", exc_info=True)
                             self._put_event_sync({'type': 'CRITICAL_ERROR', 'payload': {'reason': f'Failed to save OCO report {internal_report_id} locally for {symbol}', 'details': str(sme_report)}})
                        except Exception as e_report:
                            logger.error(f"OCO Placement ({symbol}): Неочікувана помилка обробки/збереження звіту OCO ордера: {report}. Помилка: {e_report}", exc_info=True)
                else:
                    logger.error(f"OCO Placement ({symbol}): Отримано неочікувану кількість звітів ({len(orderReports)}) в результаті OCO: {oco_result}")

            else:
                logger.critical(f"OCO Placement: КРИТИЧНА ПОМИЛКА розміщення OCO ордера для {symbol}! Позиція НЕЗАХИЩЕНА! Result: {oco_result}")
                self._put_event_sync({'type': 'CRITICAL_ERROR', 'payload': {'reason': 'OCO Placement Failed', 'symbol': symbol, 'details': str(oco_result)}})
                logger.info(f"OCO Placement Failure ({symbol}): Ініціюємо аварійний вихід...")
                await self._initiate_exit_async(reason="OCO Placement Failed")

        except (ValueError, InvalidOperation) as e_calc:
            logger.critical(f"OCO Placement ({symbol}): Помилка даних/конвертації при підготовці OCO: {e_calc}", exc_info=True)
            self._put_event_sync({'type': 'CRITICAL_ERROR', 'payload': {'reason': 'Data/Decimal error during OCO preparation', 'symbol': symbol, 'details': str(e_calc)}})
            logger.info(f"OCO Preparation Failure ({symbol}, Calc Error): Ініціюємо аварійний вихід...")
            await self._initiate_exit_async(reason="OCO Calc Failed")
        except BinanceAPIError as bae:
             logger.critical(f"OCO Placement ({symbol}): КРИТИЧНА ПОМИЛКА API під час розміщення OCO: {bae}! Позиція НЕЗАХИЩЕНА!", exc_info=True)
             self._put_event_sync({'type': 'CRITICAL_ERROR', 'payload': {'reason': 'API Error during OCO Placement', 'symbol': symbol, 'details': str(bae)}})
             logger.info(f"OCO Placement Failure ({symbol}, API Error): Ініціюємо аварійний вихід...")
             await self._initiate_exit_async(reason="OCO Placement Failed API Error")
        except StateManagerError as sme:
             logger.critical(f"OCO Placement ({symbol}): КРИТИЧНА ПОМИЛКА StateManager під час обробки OCO: {sme}! Стан може бути неконсистентним.", exc_info=True)
             self._put_event_sync({'type': 'CRITICAL_ERROR', 'payload': {'reason': 'StateManager error during OCO processing', 'symbol': symbol, 'details': str(sme)}})
        except Exception as e:
            logger.critical(f"OCO Placement ({symbol}): КРИТИЧНА НЕОЧІКУВАНА ПОМИЛКА під час розміщення OCO: {e}! Позиція може бути НЕЗАХИЩЕНА!", exc_info=True)
            self._put_event_sync({'type': 'CRITICAL_ERROR', 'payload': {'reason': 'Unexpected critical error during OCO Placement', 'symbol': symbol, 'details': str(e)}})
            logger.info(f"OCO Placement Failure ({symbol}, Exception): Ініціюємо аварійний вихід...")
            await self._initiate_exit_async(reason="OCO Placement Failed Unexpected Error")
    # --- КІНЕЦЬ ЗМІНИ ---


    async def _cancel_other_oco_part_async(self, symbol: str, list_id: str, executed_order_id: int):
        """
        Скасовує іншу частину OCO ордера, якщо одна з частин (TP або SL) виконана.
        (Заглушка - зазвичай біржа робить це автоматично, але метод може знадобитись).
        """
        logger.warning(f"Виклик заглушки _cancel_other_oco_part_async для OCO {list_id} (виконано {executed_order_id})")
        # TODO: Можливо, реалізувати перевірку статусу іншого ордера та явне скасування, якщо потрібно
        await asyncio.sleep(0)


    async def _update_session_stats_async(self, pnl: Decimal, commission: Decimal):
        """
        Оновлює статистику поточної торгової сесії після закритої угоди.
        """
        async with self.lock: # Захищаємо доступ до спільного ресурсу
            try:
                ss = self.session_stats # Отримуємо поточну статистику

                # Оновлення основних лічильників
                ss['total_trades'] += 1
                ss['total_pnl'] += pnl
                ss['total_commission'] += commission

                # Оновлення статистики виграшних/програшних угод
                if pnl > 0:
                    ss['winning_trades'] += 1
                    ss['total_profit'] += pnl
                elif pnl < 0:
                    ss['losing_trades'] += 1
                    ss['total_loss'] += abs(pnl) # Зберігаємо збиток як позитивне число

                # Перерахунок похідних показників
                total_trades = ss['total_trades']
                winning_trades = ss['winning_trades']
                losing_trades = ss['losing_trades']
                total_profit = ss['total_profit']
                total_loss = ss['total_loss']

                # Win Rate
                ss['win_rate'] = float(winning_trades / total_trades * 100) if total_trades > 0 else 0.0

                # Profit Factor
                ss['profit_factor'] = float(total_profit / total_loss) if total_loss > 0 else 0.0 # Або нескінченність, якщо збитків немає? Поки 0.0

                # Average Profit/Loss
                ss['avg_profit'] = (total_profit / winning_trades) if winning_trades > 0 else Decimal(0)
                ss['avg_loss'] = (total_loss / losing_trades) if losing_trades > 0 else Decimal(0) # Зберігаємо як позитивне

                # Оновлюємо сам словник в класі
                self.session_stats = ss
                logger.info(f"Session Stats Updated: Trades={ss['total_trades']}, PnL={ss['total_pnl']:.4f}, WinRate={ss['win_rate']:.2f}%")

                # Надсилання події про оновлення статистики
                try:
                    # Розрахунок тривалості сесії
                    duration_str = "N/A"
                    if self.start_time:
                        now = datetime.now(timezone.utc)
                        duration = now - self.start_time
                        duration_str = str(duration).split('.')[0] # Прибираємо мілісекунди

                    # Створюємо копію та конвертуємо Decimal для JSON
                    stats_payload = {}
                    for key, value in self.session_stats.items():
                         if isinstance(value, Decimal):
                             stats_payload[key] = round(float(value), 8)
                         else:
                             stats_payload[key] = value
                    stats_payload['session_duration'] = duration_str

                    self._put_event_sync({'type':'STATS_UPDATE', 'payload': stats_payload})
                    logger.debug("STATS_UPDATE event sent.")

                except Exception as e_event:
                    logger.error(f"Помилка надсилання події STATS_UPDATE: {e_event}", exc_info=True)

            except Exception as e:
                logger.error(f"Помилка в _update_session_stats_async: {e}", exc_info=True)

    # --- НОВИЙ МЕТОД: _check_and_restore_oco_async ---
    async def _check_and_restore_oco_async(self):
        """
        Перевіряє стан OCO ордера для відкритої позиції та перестворює його за потреби.
        Викликається під час start().
        """
        logger.info(f"OCO Restore ({self.symbol}): Початок перевірки стану OCO...")

        try:
            # 1. Отримання позиції
            position = await self.state_manager.get_open_position(symbol=self.symbol)
            if position is None:
                logger.info(f"OCO Restore ({self.symbol}): Відкрита позиція не знайдена. Перевірка OCO не потрібна.")
                return

            logger.debug(f"OCO Restore ({self.symbol}): Знайдено відкриту позицію: {position}")
            saved_oco_id_str = position.get('oco_list_id') # Отримуємо збережений ID (як рядок)

            # 2. Отримання активних ордерів з біржі
            logger.debug(f"OCO Restore ({self.symbol}): Запит активних ордерів з біржі...")
            open_orders_api = await self.binance_api.get_open_orders(symbol=self.symbol)
            if open_orders_api is None: # Перевіряємо на None (індикація помилки API в get_open_orders)
                 logger.error(f"OCO Restore ({self.symbol}): Помилка отримання активних ордерів з API. Перевірка OCO неможлива.")
                 return

            logger.info(f"OCO Restore ({self.symbol}): Отримано {len(open_orders_api)} активних ордерів з API.")

            # --- Аналіз ---
            active_oco_orders_found = []
            saved_oco_id_int = -1 # Для порівняння з orderListId з API
            if saved_oco_id_str:
                try:
                    saved_oco_id_int = int(saved_oco_id_str)
                    # Шукаємо ордери, що належать до нашого OCO
                    for order in open_orders_api:
                        if order.get('orderListId') == saved_oco_id_int:
                            active_oco_orders_found.append(order)
                    logger.debug(f"OCO Restore ({self.symbol}): Знайдено {len(active_oco_orders_found)} ордерів для збереженого OCO ID {saved_oco_id_int}.")
                except ValueError:
                    logger.error(f"OCO Restore ({self.symbol}): Некоректний формат збереженого oco_list_id: '{saved_oco_id_str}'. Очищаємо...")
                    saved_oco_id_str = None # Вважаємо, що ID немає
                    try:
                        await self.state_manager.update_open_position_field(symbol=self.symbol, field='oco_list_id', value=None)
                    except StateManagerError as e_clear:
                        logger.error(f"OCO Restore ({self.symbol}): Помилка очищення некоректного oco_list_id: {e_clear}")


            # --- Випадок 1: Є збережений OCO ID ---
            if saved_oco_id_str and saved_oco_id_int != -1:
                num_found = len(active_oco_orders_found)

                if num_found == 2:
                    # Знайдено обидва ордери OCO
                    # TODO: Додати перевірку параметрів ордерів (ціни, кількість) - поки пропускаємо
                    logger.info(f"OCO Restore ({self.symbol}): Знайдено 2 активних ордери для OCO ID {saved_oco_id_int}. Стан OCO вважається валідним.")
                    # TODO: Оновити/зберегти ці ордери в StateManager?
                    # Можливо, варто перевірити, чи вони вже є в БД, і якщо ні - додати.
                    # Це ускладнює логіку, поки що пропускаємо.
                    return # Все гаразд

                elif num_found == 1:
                    # Знайдено тільки один ордер - неконсистентний стан
                    inconsistent_order = active_oco_orders_found[0]
                    inconsistent_order_id = inconsistent_order.get('orderId')
                    inconsistent_client_id = inconsistent_order.get('clientOrderId')
                    logger.warning(f"OCO Restore ({self.symbol}): Знайдено лише 1 активний ордер (ID: {inconsistent_order_id}, ClientID: {inconsistent_client_id}) для OCO ID {saved_oco_id_int}. Неконсистентний стан!")

                    # Скасовуємо цей один ордер
                    logger.info(f"OCO Restore ({self.symbol}): Скасування неконсистентного ордера ID {inconsistent_order_id}...")
                    cancel_res = await self.binance_api.cancel_order(symbol=self.symbol, orderId=inconsistent_order_id)
                    if cancel_res:
                        logger.info(f"OCO Restore ({self.symbol}): Неконсистентний ордер {inconsistent_order_id} скасовано.")
                        # Видаляємо його з локального стану (якщо він там був)
                        internal_id_to_remove = inconsistent_client_id if inconsistent_client_id else str(inconsistent_order_id)
                        await self.state_manager.remove_active_order(internal_id=internal_id_to_remove)
                    else:
                        logger.error(f"OCO Restore ({self.symbol}): Не вдалося скасувати неконсистентний ордер {inconsistent_order_id}!")
                        # Що робити далі? Можливо, не варто перестворювати OCO. Поки що виходимо.
                        return

                    # TODO: Спробувати скасувати OCO список (якщо API підтримує)?
                    # Наприклад: await self.binance_api.cancel_oco_order(symbol=self.symbol, orderListId=saved_oco_id_int)

                elif num_found == 0:
                    # Не знайдено жодного ордера для збереженого ID
                    logger.warning(f"OCO Restore ({self.symbol}): Збережений OCO ID {saved_oco_id_int} не знайдено серед активних ордерів на біржі.")

                # --- Загальна логіка для num_found = 0 або 1 (після скасування) ---
                # Очищаємо ID в позиції та перестворюємо OCO
                try:
                    logger.info(f"OCO Restore ({self.symbol}): Очищення невалідного/неповного oco_list_id ({saved_oco_id_str}) у позиції...")
                    await self.state_manager.update_open_position_field(symbol=self.symbol, field='oco_list_id', value=None)
                    # Перестворюємо OCO
                    logger.info(f"OCO Restore ({self.symbol}): Перестворення OCO ордера...")
                    await self._place_oco_order_async(position) # Передаємо дані поточної позиції
                    logger.info(f"OCO Restore ({self.symbol}): Завершено спробу перестворення OCO.")
                except StateManagerError as e_clear:
                    logger.error(f"OCO Restore ({self.symbol}): Помилка очищення oco_list_id: {e_clear}. OCO не буде перестворено.")
                except Exception as e_recreate:
                     logger.error(f"OCO Restore ({self.symbol}): Помилка під час перестворення OCO: {e_recreate}", exc_info=True)

                return # Завершуємо роботу після спроби відновлення

            # --- Випадок 2: Немає збереженого OCO ID ---
            else: # saved_oco_id_str is None or saved_oco_id_int == -1
                logger.info(f"OCO Restore ({self.symbol}): Збережений oco_list_id відсутній. Перевірка існуючих ордерів...")

                # Перевіряємо, чи є потенційно конфліктуючі ордери (SELL з цілями TP/SL/EXIT)
                conflicting_orders_found = False
                conflicting_purposes = {'TP', 'SL', 'EXIT_SIGNAL', 'EXIT_MANUAL', 'EXIT_PROTECTION', 'EXIT_UNKNOWN'} # Можливі цілі вихідних ордерів
                for order in open_orders_api:
                    order_side = order.get('side')
                    # Перевіряємо ціль ордера (якщо вона є в локальному стані - тут ми її не маємо, тому перевірка ускладнена)
                    # Поки що просто перевіряємо наявність будь-яких SELL ордерів
                    if order_side == 'SELL':
                        logger.warning(f"OCO Restore ({self.symbol}): Знайдено активний SELL ордер (ID: {order.get('orderId')}) при відсутності збереженого OCO ID.")
                        conflicting_orders_found = True
                        break # Достатньо одного

                if conflicting_orders_found:
                    logger.warning(f"OCO Restore ({self.symbol}): Виявлено потенційно конфліктуючі активні ордери. Скасування всіх ордерів перед розміщенням OCO...")
                    cancel_ok = await self._cancel_all_active_orders_safely("Restore OCO - Clearing Existing")
                    if not cancel_ok:
                        logger.error(f"OCO Restore ({self.symbol}): Не вдалося скасувати існуючі ордери. OCO не буде створено.")
                        return # Не можемо продовжити, якщо скасування не вдалося

                # Розміщуємо новий OCO (або після скасування, або якщо конфліктів не було)
                logger.info(f"OCO Restore ({self.symbol}): Розміщення нового OCO ордера...")
                await self._place_oco_order_async(position)
                logger.info(f"OCO Restore ({self.symbol}): Завершено розміщення нового OCO.")

        except BinanceAPIError as bae:
            logger.error(f"OCO Restore ({self.symbol}): Помилка API під час перевірки/відновлення OCO: {bae}", exc_info=True)
        except StateManagerError as sme:
            logger.error(f"OCO Restore ({self.symbol}): Помилка StateManager під час перевірки/відновлення OCO: {sme}", exc_info=True)
        except Exception as e:
            logger.error(f"OCO Restore ({self.symbol}): Неочікувана помилка під час перевірки/відновлення OCO: {e}", exc_info=True)

        logger.info(f"OCO Restore ({self.symbol}): Перевірку стану OCO завершено.")
    # --- КІНЕЦЬ НОВОГО МЕТОДУ ---

# END BLOCK 3.9
# BLOCK 3.10: PnL Calculation Helper
    def _calculate_trade_pnl(self,
                             position_data: dict,
                             exit_avg_price: Decimal,
                             exit_filled_qty: Decimal,
                             exit_commission: Decimal,
                             exit_commission_asset: Optional[str]) -> Tuple[Optional[Decimal], Optional[Decimal]]:
        """
        Розраховує чистий PnL та загальну комісію для закритої угоди в Quote Asset.

        Args:
            position_data (dict): Дані позиції перед закриттям (з StateManager).
            exit_avg_price (Decimal): Середня ціна виходу.
            exit_filled_qty (Decimal): Виконана кількість при виході.
            exit_commission (Decimal): Комісія (останньої частини) ордера на вихід.
            exit_commission_asset (Optional[str]): Актив комісії виходу.

        Returns:
            Tuple[Optional[Decimal], Optional[Decimal]]: (pnl_net, total_commission_quote)
                                                        або (None, None) у разі помилки.
        """
        logger.debug(f"PnL Calc: Початок розрахунку для позиції: {position_data.get('symbol')}, ExitPx={exit_avg_price}, ExitQty={exit_filled_qty}")

        try:
            # --- Витягуємо та валідуємо дані входу ---
            entry_price = self._safe_decimal(position_data.get('entry_price'))
            entry_qty_abs = abs(self._safe_decimal(position_data.get('quantity'), Decimal(0)))
            # entry_cost = self._safe_decimal(position_data.get('entry_cost')) # Поки не використовуємо
            entry_commission = self._safe_decimal(position_data.get('entry_commission'), Decimal(0)) # Комісія входу (з позиції)
            entry_commission_asset = position_data.get('entry_commission_asset')

            if entry_price is None or entry_price <= 0 or entry_qty_abs <= 0:
                raise ValueError(f"Некоректні дані входу: entry_price={entry_price}, entry_qty_abs={entry_qty_abs}")

            # --- Валідуємо дані виходу ---
            if exit_avg_price is None or exit_avg_price <= 0 or exit_filled_qty is None or exit_filled_qty <= 0:
                 raise ValueError(f"Некоректні дані виходу: exit_avg_price={exit_avg_price}, exit_filled_qty={exit_filled_qty}")

            # Перевірка консистентності кількості (кількість виходу має відповідати кількості входу для повного закриття)
            # Допускаємо невелику похибку через округлення
            qty_diff_threshold = Decimal('1e-' + str(self.qty_precision)) # Використовуємо точність кількості
            if abs(exit_filled_qty - entry_qty_abs) > qty_diff_threshold:
                logger.warning(f"PnL Calc: Кількість виходу ({exit_filled_qty}) суттєво відрізняється від кількості входу ({entry_qty_abs}). PnL може бути неточним.")
                # Можна вирішити використовувати меншу з кількостей для розрахунку, або продовжити з exit_filled_qty

            # --- Розрахунок загальної комісії в Quote Asset ---
            total_commission_quote = Decimal(0)

            # Комісія входу
            if entry_commission > 0:
                if entry_commission_asset == self.quote_asset:
                    total_commission_quote += entry_commission
                    logger.debug(f"PnL Calc: Додано комісію входу (Quote): {entry_commission}")
                elif entry_commission_asset == self.base_asset:
                    commission_entry_quote = entry_commission * entry_price # Оцінка вартості комісії входу
                    total_commission_quote += commission_entry_quote
                    logger.debug(f"PnL Calc: Додано комісію входу (Base->Quote): {entry_commission} * {entry_price} = {commission_entry_quote}")
                else:
                    logger.warning(f"PnL Calc: Невідомий актив комісії входу: '{entry_commission_asset}'. Комісію входу ({entry_commission}) не враховано.")

            # Комісія виходу
            # УВАГА: `exit_commission` з executionReport - це комісія ЛИШЕ ОСТАННЬОЇ УГОДИ ордера.
            # Для точного розрахунку потрібно агрегувати комісії всіх угод, що закрили позицію.
            # Поки що використовуємо те, що є, згідно з інструкцією.
            if exit_commission > 0 and exit_commission_asset:
                if exit_commission_asset == self.quote_asset:
                    total_commission_quote += exit_commission
                    logger.debug(f"PnL Calc: Додано комісію виходу (Quote): {exit_commission}")
                elif exit_commission_asset == self.base_asset:
                    commission_exit_quote = exit_commission * exit_avg_price # Оцінка вартості комісії виходу
                    total_commission_quote += commission_exit_quote
                    logger.debug(f"PnL Calc: Додано комісію виходу (Base->Quote): {exit_commission} * {exit_avg_price} = {commission_exit_quote}")
                else:
                    logger.warning(f"PnL Calc: Невідомий актив комісії виходу: '{exit_commission_asset}'. Комісію виходу ({exit_commission}) не враховано.")
            elif exit_commission > 0:
                 logger.warning(f"PnL Calc: Не вказано актив комісії виходу ('{exit_commission_asset}'). Комісію виходу ({exit_commission}) не враховано.")


            # --- Розрахунок PnL ---
            # Використовуємо кількість виходу як фактичну кількість угоди
            # Використовуємо ціну входу та кількість входу для розрахунку вартості входу
            entry_value = entry_price * entry_qty_abs
            exit_value = exit_avg_price * exit_filled_qty # Використовуємо фактичну кількість виходу

            pnl_gross = exit_value - entry_value
            pnl_net = pnl_gross - total_commission_quote

            logger.debug(f"PnL Calc: EntryValue={entry_value:.4f}, ExitValue={exit_value:.4f}, GrossPnL={pnl_gross:.4f}, TotalComm={total_commission_quote:.4f}, NetPnL={pnl_net:.4f}")

            return pnl_net, total_commission_quote

        except (ValueError, TypeError, InvalidOperation) as e_val:
            logger.error(f"PnL Calc: Помилка валідації даних для розрахунку PnL: {e_val}", exc_info=True)
            return None, None
        except Exception as e:
            logger.error(f"PnL Calc: Неочікувана помилка при розрахунку PnL: {e}", exc_info=True)
            return None, None

# END BLOCK 3.10
# BLOCK 3.11: Command Processor Thread Management
    def _start_command_processor(self):
        """Запускає фоновий потік для обробки команд з command_queue."""
        if self._command_processor_thread is not None and self._command_processor_thread.is_alive():
            logger.warning("Command Processor: Спроба запуску, коли потік вже працює.")
            return

        logger.info("Command Processor: Запуск потоку обробки команд...")
        self._stop_command_processor = False
        try:
            # Зберігаємо поточний event loop, в якому працює ядро
            self._main_event_loop = asyncio.get_running_loop()
            logger.debug(f"Command Processor: Збережено main event loop: {self._main_event_loop}")
        except RuntimeError as e_loop:
            # Це не повинно статися, якщо start викликається з async контексту
            logger.critical(f"Command Processor: Не вдалося отримати поточний event loop! {e_loop}", exc_info=True)
            # Не запускаємо потік, якщо немає циклу для виконання команд
            return

        self._command_processor_thread = threading.Thread(
            target=self._command_processor_loop,
            daemon=True, # Робимо потік демоном
            name="CommandProcessor"
        )
        self._command_processor_thread.start()
        logger.info("Command Processor: Потік запущено.")

    def _stop_command_processor(self):
        """Сигналізує про зупинку потоку обробки команд та чекає на його завершення."""
        if not self._command_processor_thread or not self._command_processor_thread.is_alive():
            logger.warning("Command Processor: Спроба зупинки, коли потік не запущено або вже завершився.")
            return

        logger.info("Command Processor: Ініціювання зупинки потоку обробки команд...")
        self._stop_command_processor = True
        # Надсилаємо None в чергу, щоб розбудити потік, якщо він чекає на get()
        try:
            self.command_queue.put_nowait(None)
        except queue.Full:
             logger.warning("Command Processor: Не вдалося додати None в чергу команд (переповнена) для зупинки.")
        except Exception as e_put:
             logger.error(f"Command Processor: Помилка при додаванні None в чергу для зупинки: {e_put}")

        # Чекаємо на завершення потоку
        logger.debug("Command Processor: Очікування завершення потоку...")
        self._command_processor_thread.join(timeout=2.0) # Збільшимо таймаут

        if self._command_processor_thread.is_alive():
            logger.warning("Command Processor: Потік обробки команд не завершився вчасно (timeout).")
        else:
            logger.info("Command Processor: Потік обробки команд успішно зупинено.")

        self._command_processor_thread = None
        self._main_event_loop = None # Скидаємо збережений цикл

    def _command_processor_loop(self):
        """
        Основний цикл обробника команд, що виконується у фоновому потоці.
        Читає команди з self.command_queue та передає їх на асинхронне виконання.
        """
        thread_name = threading.current_thread().name
        logger.info(f"Command Processor ({thread_name}): Цикл обробки команд запущено.")

        if not self._main_event_loop:
            logger.critical(f"Command Processor ({thread_name}): Відсутній main_event_loop! Цикл не може працювати.")
            return

        while not self._stop_command_processor:
            try:
                # Отримуємо команду з черги, чекаємо 1 секунду
                command = self.command_queue.get(block=True, timeout=1.0)

                if command is None: # Сигнал на зупинку
                    logger.info(f"Command Processor ({thread_name}): Отримано сигнал None, завершення циклу.")
                    break # Виходимо з циклу

                if isinstance(command, dict) and 'type' in command:
                    command_type = command.get('type')
                    logger.info(f"Command Processor ({thread_name}): Отримано команду '{command_type}'. Передача в main loop...")

                    # Передаємо асинхронну задачу в основний цикл asyncio
                    future = asyncio.run_coroutine_threadsafe(
                        self._process_command_async(command),
                        self._main_event_loop
                    )

                    try:
                        # Очікуємо результат виконання (опціонально, з таймаутом)
                        # Це блокує command processor потік, поки команда не виконається в main loop
                        result = future.result(timeout=10.0) # Збільшений таймаут для потенційно довгих команд
                        logger.debug(f"Command Processor ({thread_name}): Команда '{command_type}' виконана в main loop, результат: {result}")
                    except asyncio.TimeoutError:
                        logger.warning(f"Command Processor ({thread_name}): Таймаут ({future.get_loop().time() - future._start_time:.1f}s) очікування результату виконання команди '{command_type}'.")
                        # Команда може все ще виконуватися в main loop
                    except Exception as e_exec:
                        # Помилка сталася під час виконання _process_command_async в main loop
                        logger.error(f"Command Processor ({thread_name}): Помилка виконання команди '{command_type}' в main_loop: {e_exec}", exc_info=True)
                        # Потрібно вирішити, чи повідомляти про помилку назад через event_queue?
                        self._put_event_sync({'type': 'COMMAND_ERROR', 'payload': {'command': command_type, 'error': str(e_exec)}})

                else:
                    logger.warning(f"Command Processor ({thread_name}): Отримано невірний формат команди: {command}")

                # Позначаємо команду як оброблену в черзі
                self.command_queue.task_done()

            except queue.Empty:
                # Таймаут очікування в черзі - це нормально, просто продовжуємо цикл
                continue
            except Exception as e_loop:
                logger.error(f"Command Processor ({thread_name}): Неочікувана помилка в циклі обробки команд: {e_loop}", exc_info=True)
                # Можливо, варто спробувати відновити роботу або зупинитися?
                # Поки що просто логуємо і продовжуємо (якщо stop_flag не встановлено)
                time.sleep(1) # Невелика пауза перед наступною ітерацією

        logger.info(f"Command Processor ({thread_name}): Цикл обробки команд завершено.")

# END BLOCK 3.11
# BLOCK 3.12: Async Command Processor Logic
    async def _process_command_async(self, command: dict):
        """
        Асинхронно обробляє команду, отриману з command_queue.
        Виконується в основному циклі asyncio.
        """
        command_type = command.get('type', 'UNKNOWN').upper()
        payload = command.get('payload', {})
        logger.info(f"Processing Command: Type='{command_type}', Payload='{payload}'")

        try:
            if command_type == 'START':
                logger.info("Command Processor: Виклик self.start()...")
                await self.start()
                return "Start command processed."

            elif command_type == 'STOP':
                logger.info("Command Processor: Виклик self.stop()...")
                await self.stop()
                return "Stop command processed."

            elif command_type == 'GET_STATUS':
                logger.info("Command Processor: Виклик self.get_status_async()...")
                status = await self.get_status_async()
                self._put_event_sync({'type': 'STATUS_RESPONSE', 'payload': status})
                return "Status requested."

            elif command_type == 'GET_BALANCE':
                logger.debug("Command Processor: Отримання балансів...")
                balances_decimal = await self.state_manager.get_balances()
                balances_response = self._convert_dict_types_for_event(balances_decimal)
                self._put_event_sync({'type': 'BALANCE_RESPONSE', 'payload': balances_response})
                return "Balances requested."

            elif command_type == 'GET_POSITION':
                logger.debug(f"Command Processor: Отримання позиції для {self.symbol}...")
                position_decimal = await self.state_manager.get_open_position(self.symbol)
                position_response = self._convert_dict_types_for_event(position_decimal)
                self._put_event_sync({'type': 'POSITION_RESPONSE', 'payload': position_response})
                return "Position requested."

            elif command_type == 'GET_HISTORY':
                limit = payload.get('count', 100)
                logger.debug(f"Command Processor: Отримання історії {limit} угод для {self.symbol}...")
                history_decimal = await self.state_manager.get_trade_history(symbol=self.symbol, limit=limit)
                history_response = self._convert_dict_types_for_event(history_decimal)
                self._put_event_sync({'type': 'HISTORY_RESPONSE', 'payload': history_response})
                return f"History requested (limit={limit})."

            elif command_type == 'GET_STATS':
                 logger.debug("Command Processor: Отримання статистики сесії...")
                 stats_copy = self.session_stats.copy()
                 duration_str = "N/A"
                 if self.start_time:
                     now = datetime.now(timezone.utc)
                     duration = now - self.start_time
                     duration_str = str(duration).split('.')[0]
                 stats_copy['session_duration'] = duration_str
                 stats_response = self._convert_dict_types_for_event(stats_copy)
                 self._put_event_sync({'type': 'STATS_RESPONSE', 'payload': stats_response})
                 return "Stats requested."

            elif command_type == 'CLOSE_POSITION':
                logger.info("Command Processor: Ініціація ручного закриття позиції...")
                current_pos = await self.state_manager.get_open_position(self.symbol)
                if current_pos:
                    await self._initiate_exit_async(reason="Manual")
                    return "Manual position close initiated."
                else:
                    logger.warning("Command Processor: Команда CLOSE_POSITION, але позиція не відкрита.")
                    self._put_event_sync({'type': 'INFO', 'payload': {'message': 'Position is not open, cannot close.'}})
                    return "Position not open."

            # --- ЗМІНА: Оновлення обробки RELOAD_SETTINGS ---
            elif command_type == 'RELOAD_SETTINGS':
                logger.info("Command Processor: Перезавантаження налаштувань та стратегії...")
                ws_restart_needed = False
                try:
                    # Зберігаємо поточні параметри, що впливають на WS
                    old_symbol = self.symbol
                    old_interval = self.interval
                    logger.info(f"RELOAD_SETTINGS: Поточні параметри: Symbol={old_symbol}, Interval={old_interval}")

                    # Виконуємо синхронні операції в екзекуторі
                    loop = asyncio.get_running_loop()
                    await loop.run_in_executor(None, self._load_bot_settings)
                    # _load_bot_settings скидає self._symbol_info
                    await loop.run_in_executor(None, self._load_strategy)
                    logger.info("Налаштування та стратегію перезавантажено (синхронна частина).")

                    # Перевіряємо, чи змінились параметри, що вимагають перезапуску WS
                    if self.symbol != old_symbol or self.interval != old_interval:
                        logger.info(f"RELOAD_SETTINGS: Змінилися параметри Symbol/Interval ({old_symbol}/{old_interval} -> {self.symbol}/{self.interval}). Потрібен перезапуск WebSocket.")
                        ws_restart_needed = True

                        # 1. Зупиняємо старий WS Handler (якщо він є)
                        if self._ws_handler:
                            logger.info("RELOAD_SETTINGS: Зупинка старого WebSocket Handler...")
                            try:
                                await self._ws_handler.stop()
                                logger.info("RELOAD_SETTINGS: Старий WebSocket Handler зупинено.")
                            except Exception as e_ws_stop:
                                logger.error(f"RELOAD_SETTINGS: Помилка під час зупинки старого WS Handler: {e_ws_stop}", exc_info=True)
                            finally:
                                self._ws_handler = None # Скидаємо посилання в будь-якому випадку

                        # 2. Оновлюємо symbol_info для нового символу (кеш вже скинуто)
                        logger.info(f"RELOAD_SETTINGS: Отримання symbol_info для нового символу {self.symbol}...")
                        await self._get_symbol_info_async()

                        # 3. Перестворюємо WS Handler (синхронний метод)
                        logger.info("RELOAD_SETTINGS: Перестворення WebSocket Handler...")
                        self._create_ws_handler() # Цей метод встановлює self._ws_handler

                        if not self._ws_handler:
                             logger.error("RELOAD_SETTINGS: Не вдалося перестворити WebSocket Handler!")
                             # Якщо не вдалося створити обробник, бот не зможе працювати далі з ринковими даними
                             self._put_event_sync({'type': 'ERROR', 'payload': {'message': 'Failed to recreate WebSocket handler after settings reload.'}})
                             return "Error during WS Handler recreation." # Виходимо з обробки команди

                        # 4. Якщо бот запущено, запускаємо новий WS Handler
                        if self._is_running:
                            logger.info("RELOAD_SETTINGS: Ядро запущено, запускаємо новий WebSocket Handler...")
                            try:
                                # 4.1 Отримуємо новий Listen Key
                                logger.info("RELOAD_SETTINGS: Отримання нового Listen Key...")
                                listen_key = await self.binance_api.start_user_data_stream()
                                if not listen_key:
                                     logger.error("RELOAD_SETTINGS: Не вдалося отримати новий Listen Key! WS не буде запущено.")
                                     raise AsyncBotCoreError("Failed to get new listen key for WS restart.")

                                # 4.2 Встановлюємо ключ та запускаємо WS
                                self._ws_handler.set_listen_key(listen_key)
                                logger.info("RELOAD_SETTINGS: Запуск нового WebSocket Handler...")
                                await self._ws_handler.start()
                                await asyncio.sleep(1) # Пауза для підключення
                                ws_connected_check = getattr(self._ws_handler, 'is_connected', getattr(self._ws_handler, 'connected', False))
                                ws_connected = ws_connected_check() if callable(ws_connected_check) else ws_connected_check
                                if ws_connected:
                                     logger.info("RELOAD_SETTINGS: Новий WebSocket Handler успішно запущено та підключено.")
                                else:
                                     logger.warning("RELOAD_SETTINGS: Новий WebSocket Handler запущено, але ще не підключився.")

                            except (BinanceAPIError, AsyncBotCoreError) as e_ws_start:
                                 logger.error(f"RELOAD_SETTINGS: Помилка під час отримання Listen Key або запуску нового WS Handler: {e_ws_start}", exc_info=True)
                                 self._put_event_sync({'type': 'ERROR', 'payload': {'message': f'Failed to restart WebSocket: {e_ws_start}'}})
                                 # Що робити далі? Бот запущений, але без WS. Можливо, зупинити?
                                 # Поки що просто логуємо помилку.
                        else:
                             logger.info("RELOAD_SETTINGS: Ядро не запущено, новий WS Handler створено, але не запущено.")
                    else:
                        # Параметри не змінилися
                        logger.info("RELOAD_SETTINGS: Параметри Symbol/Interval не змінилися. Перезапуск WebSocket не потрібен.")

                    # Повідомлення про успішне перезавантаження (загальне)
                    self._put_event_sync({'type': 'INFO', 'payload': {'message': f'Settings and strategy ({self.selected_strategy_name}) reloaded successfully for {self.symbol}. WS Restart Needed: {ws_restart_needed}'}})
                    # Надіслати оновлений статус
                    status = await self.get_status_async()
                    self._put_event_sync({'type': 'STATUS_RESPONSE', 'payload': status})
                    return "Settings reloaded."

                except Exception as e_reload:
                    logger.error(f"Помилка під час перезавантаження налаштувань: {e_reload}", exc_info=True)
                    self._put_event_sync({'type': 'ERROR', 'payload': {'message': f'Error reloading settings: {e_reload}'}})
                    return "Error during settings reload."
            # --- КІНЕЦЬ ЗМІНИ ---

            else:
                logger.warning(f"Command Processor: Отримано невідомий тип команди: '{command_type}'")
                self._put_event_sync({'type': 'ERROR', 'payload': {'message': f"Unknown command type: {command_type}"}})
                return f"Unknown command type: {command_type}"

        except StateManagerError as sme:
             logger.error(f"Command Processor ({command_type}): Помилка StateManager: {sme}", exc_info=True)
             self._put_event_sync({'type': 'COMMAND_ERROR', 'payload': {'command': command_type, 'error': f"StateManager Error: {sme}"}})
             return f"Error processing {command_type}: StateManager Error"
        except BinanceAPIError as bae:
             logger.error(f"Command Processor ({command_type}): Помилка Binance API: {bae}", exc_info=True)
             self._put_event_sync({'type': 'COMMAND_ERROR', 'payload': {'command': command_type, 'error': f"Binance API Error: {bae}"}})
             return f"Error processing {command_type}: Binance API Error"
        except Exception as e:
            logger.error(f"Command Processor ({command_type}): Неочікувана помилка обробки команди: {e}", exc_info=True)
            self._put_event_sync({'type': 'COMMAND_ERROR', 'payload': {'command': command_type, 'error': f"Unexpected Error: {e}"}})
            return f"Error processing {command_type}: Unexpected Error"


    def _convert_dict_types_for_event(self, data: Optional[Any]) -> Optional[Any]:
        """
        Рекурсивно конвертує типи даних (Decimal -> float, datetime -> str)
        у словниках та списках для JSON-сумісності подій.
        """
        if data is None:
            return None

        if isinstance(data, Decimal):
            # Округлення до 8 знаків для float
            return round(float(data), 8)
        elif isinstance(data, datetime):
            return data.isoformat()
        elif isinstance(data, dict):
            converted_dict = {}
            for key, value in data.items():
                converted_dict[key] = self._convert_dict_types_for_event(value) # Рекурсивний виклик
            return converted_dict
        elif isinstance(data, list):
            # Рекурсивно обробляємо елементи списку
            return [self._convert_dict_types_for_event(item) for item in data]
        else:
            # Повертаємо інші типи як є (int, str, bool, ...)
            return data

# END BLOCK 3.12
# BLOCK 4: Lifecycle Methods (start, stop, _run_loop)
    async def start(self):
        """Асинхронно запускає основний цикл роботи бота та WebSocket."""
        logger.info("AsyncBotCore: Спроба запуску...")
        async with self.lock: # Захищаємо процес запуску
            if self._is_running:
                logger.warning("AsyncBotCore: Спроба запуску, коли ядро вже працює.")
                self._put_event_sync({'type':'INFO', 'payload': {'message': 'Ядро вже запущено.'}})
                return
            if not self._initialized:
                logger.error("AsyncBotCore: Спроба запуску до завершення ініціалізації!")
                self._put_event_sync({'type':'ERROR', 'payload': {'message': 'Помилка: Ядро не ініціалізовано.'}})
                try:
                     logger.info("AsyncBotCore: Спроба автоматичної ініціалізації перед запуском...")
                     await self.initialize()
                     if not self._initialized: # Перевірка після спроби ініціалізації
                          logger.error("AsyncBotCore: Автоматична ініціалізація не вдалася. Запуск скасовано.")
                          return
                except Exception as e_init:
                     logger.error(f"AsyncBotCore: Помилка автоматичної ініціалізації: {e_init}. Запуск скасовано.")
                     return

            if not self._ws_handler:
                logger.error("AsyncBotCore: Немає обробника WebSocket для запуску.")
                self._put_event_sync({'type':'ERROR', 'payload': {'message': 'Помилка: WS Handler не створено.'}})
                return
            if not self._strategy_instance:
                logger.error("AsyncBotCore: Немає екземпляра стратегії для запуску.")
                self._put_event_sync({'type':'ERROR', 'payload': {'message': 'Помилка: Стратегія не завантажена.'}})
                return
            if not self._command_processor_thread or not self._command_processor_thread.is_alive():
                 logger.warning("AsyncBotCore: Потік обробки команд не запущено. Спроба запуску...")
                 self._start_command_processor()
                 await asyncio.sleep(0.1)
                 if not self._command_processor_thread or not self._command_processor_thread.is_alive():
                      logger.error("AsyncBotCore: Не вдалося запустити потік обробки команд. Запуск скасовано.")
                      return


            logger.info(f"=== ЗАПУСК AsyncBotCore [{self.symbol} / {self.active_mode}] ===")
            try:
                # 1. Скидання статистики сесії та часу останньої свічки
                self.session_stats = self._reset_session_stats()
                self.last_kline_update_time = 0

                # 2. Перевірка/відновлення OCO для існуючої позиції (якщо є)
                await self._check_and_restore_oco_async()

                # 3. Отримання/Оновлення Listen Key
                logger.info("AsyncBotCore: Отримання/Оновлення Listen Key...")
                listen_key = await self.binance_api.start_user_data_stream()
                if not listen_key:
                    raise AsyncBotCoreError("Не вдалося отримати listenKey від Binance API.")
                self._ws_handler.set_listen_key(listen_key)

                # 4. Запуск WebSocket Handler
                logger.info("AsyncBotCore: Запуск WebSocket Handler...")
                await self._ws_handler.start()
                await asyncio.sleep(1)
                ws_connected_check = getattr(self._ws_handler, 'is_connected', getattr(self._ws_handler, 'connected', False))
                ws_connected = ws_connected_check() if callable(ws_connected_check) else ws_connected_check
                if not ws_connected:
                    logger.warning("AsyncBotCore: WS Handler запустився, але ще не підключився.")

                # 5. Встановлення стану "запущено" та часу старту
                self._is_running = True
                self.start_time = datetime.now(timezone.utc)

                # 6. Запуск основного циклу ядра (якщо він потрібен)
                if not self._main_task or self._main_task.done():
                    logger.info("AsyncBotCore: Запуск основного циклу _run_loop...")
                    self._main_task = asyncio.create_task(self._run_loop(), name="AsyncBotCoreRunLoop")
                    self._main_task.add_done_callback(self._handle_main_task_completion)
                else:
                    logger.warning("AsyncBotCore: Основна задача _main_task вже існує і не завершена.")

                start_time_str = self.start_time.strftime('%Y-%m-%d %H:%M:%S %Z')
                logger.info(f"=== AsyncBotCore УСПІШНО ЗАПУЩЕНО о {start_time_str} ===")
                # Надсилаємо подію про статус
                # Використовуємо старий get_status, бо _put_event_sync синхронний
                status_payload = self.get_status()
                self._put_event_sync({'type':'STATUS_UPDATE', 'payload': status_payload})

            except (BinanceAPIError, AsyncBotCoreError, Exception) as e:
                logger.critical(f"AsyncBotCore: Критична помилка під час старту: {e}", exc_info=True)
                await self.stop()
                self._put_event_sync({'type':'ERROR', 'payload': {'source':'AsyncBotCore.start', 'message':f"Помилка старту: {e}"}})


    async def stop(self):
        """Асинхронно зупиняє WebSocket, основну задачу та закриває ресурси."""
        logger.info("AsyncBotCore: Ініціювання зупинки...")
        if not self._is_running:
            logger.warning("AsyncBotCore: Спроба зупинки, коли ядро вже зупинено або не було запущено.")
            if self._command_processor_thread and self._command_processor_thread.is_alive():
                 logger.warning("AsyncBotCore: Ядро не запущено, але обробник команд працює. Зупиняємо його.")
                 self._stop_command_processor()
            return

        self._is_running = False
        stop_time = datetime.now(timezone.utc)
        logger.info("AsyncBotCore: Прапорець _is_running встановлено в False.")

        async with self.lock:
            # 1. Скасовуємо основну задачу ядра
            if self._main_task and not self._main_task.done():
                 logger.info("AsyncBotCore: Скасування основної задачі _run_loop...")
                 self._main_task.cancel()
                 try:
                     await asyncio.wait_for(self._main_task, timeout=2.0)
                 except asyncio.CancelledError: logger.debug("Основна задача _run_loop скасована.")
                 except asyncio.TimeoutError: logger.warning("Таймаут очікування завершення _run_loop.")
                 except Exception as e: logger.error(f"Помилка очікування _run_loop: {e}")
                 finally: self._main_task = None

            # 2. Зупиняємо WebSocket Handler
            current_ws_handler = self._ws_handler
            listen_key_to_close = None
            if current_ws_handler:
                 logger.info("AsyncBotCore: Зупинка WebSocket Handler...")
                 try:
                     listen_key_to_close = current_ws_handler.listen_key
                     await current_ws_handler.stop()
                 except Exception as e_ws_stop:
                      logger.error(f"Помилка під час зупинки WebSocket Handler: {e_ws_stop}", exc_info=True)
                 finally:
                      self._ws_handler = None

            # 3. Закриваємо Listen Key
            if listen_key_to_close and self.binance_api and self.binance_api._initialized:
                 logger.info("AsyncBotCore: Закриття Listen Key...")
                 try:
                     await self.binance_api.close_user_data_stream(listen_key_to_close)
                 except Exception as lk_close_err:
                     logger.error(f"Помилка закриття Listen Key {listen_key_to_close[:5]}...: {lk_close_err}")

            # 4. Зупиняємо обробник команд
            logger.info("AsyncBotCore: Зупинка обробника команд...")
            self._stop_command_processor()

            # 5. Закриваємо з'єднання з БД
            if self.state_manager:
                 logger.info("AsyncBotCore: Закриття StateManager (БД)...")
                 try:
                     await self.state_manager.close()
                 except Exception as e_sm_close:
                     logger.error(f"Помилка закриття StateManager: {e_sm_close}", exc_info=True)


            # 6. Закриваємо сесію API
            if self.binance_api:
                 logger.info("AsyncBotCore: Закриття BinanceAPI (сесія)...")
                 try:
                     await self.binance_api.close()
                 except Exception as e_api_close:
                     logger.error(f"Помилка закриття BinanceAPI: {e_api_close}", exc_info=True)

            # Скидаємо прапорець ініціалізації
            self._initialized = False
            self.start_time = None

            stop_time_str = stop_time.strftime('%Y-%m-%d %H:%M:%S %Z')
            logger.info(f"=== AsyncBotCore ЗУПИНЕНО о {stop_time_str} ===")
            try:
                # Використовуємо старий get_status, бо _put_event_sync синхронний
                final_status = self.get_status()
                self._put_event_sync({'type':'STATUS_UPDATE', 'payload': final_status})
            except Exception as e:
                logger.error(f"Помилка отримання/надсилання фінального статусу: {e}")


    async def _run_loop(self):
        """Основний асинхронний цикл роботи ядра (після запуску)."""
        task_name = asyncio.current_task().get_name() if asyncio.current_task() else "AsyncBotCoreRunLoop"
        logger.info(f"AsyncBotCore ({task_name}): Основний цикл запущено.")
        try:
            while self._is_running:
                current_task = asyncio.current_task()
                if current_task and current_task.cancelled():
                    logger.info(f"AsyncBotCore ({task_name}): Цикл скасовано.")
                    break

                # Основна логіка ядра (періодичні задачі)
                # Наприклад, можна додати перевірку стану WS з'єднання
                # if self._ws_handler and not self._ws_handler.is_connected():
                #    logger.warning(f"AsyncBotCore ({task_name}): WebSocket не підключено! Спроба перепідключення?")
                   # Можна ініціювати reconnect логіку тут або в самому ws_handler

                await asyncio.sleep(60)

        except asyncio.CancelledError:
            logger.info(f"AsyncBotCore ({task_name}): Основний цикл коректно скасовано.")
        except Exception as e:
            logger.critical(f"AsyncBotCore ({task_name}): Критична помилка в основному циклі: {e}", exc_info=True)
            if self._is_running:
                 logger.info(f"AsyncBotCore ({task_name}): Ініціюємо аварійну зупинку через помилку в циклі...")
                 asyncio.create_task(self.stop())
            self._is_running = False
            self._put_event_sync({'type':'CRITICAL_ERROR', 'payload': {'source':'AsyncBotCore._run_loop', 'message':f'Критична помилка циклу: {e}'}})
        finally:
            logger.info(f"AsyncBotCore ({task_name}): Основний цикл завершено.")

    # --- Callback для завершення основної задачі ---
    def _handle_main_task_completion(self, task: asyncio.Task):
        """Обробляє завершення основної задачі _run_loop."""
        task_name = task.get_name()
        try:
            exception = task.exception()
            if exception and not isinstance(exception, asyncio.CancelledError):
                logger.critical(f"AsyncBotCore: Основна задача ({task_name}) завершилася з помилкою:", exc_info=exception)
                self._put_event_sync({'type':'CRITICAL_ERROR', 'payload': {'source':task_name, 'message':f'Помилка основного циклу: {exception}'}})
            elif task.cancelled():
                 logger.info(f"AsyncBotCore: Основна задача ({task_name}) була скасована.")
            else:
                logger.info(f"AsyncBotCore: Основна задача ({task_name}) завершена штатно.")
        except asyncio.CancelledError:
             logger.info(f"AsyncBotCore: Callback завершення задачі ({task_name}) скасовано.")
        except Exception as e:
            logger.error(f"AsyncBotCore: Помилка в _handle_main_task_completion для {task_name}: {e}", exc_info=True)

    # --- Метод для надсилання подій (синхронний, для використання з callback/потоків) ---
    def _put_event_sync(self, event: dict):
        """Синхронно додає подію до event_queue."""
        if not hasattr(self, 'event_queue') or self.event_queue is None:
             logger.error("AsyncBotCore: event_queue не ініціалізовано! Неможливо надіслати подію.")
             return
        try:
            event['timestamp_event'] = datetime.now(timezone.utc).isoformat()
            self.event_queue.put_nowait(event)
        except queue.Full:
            logger.warning(f"AsyncBotCore: Черга подій (event_queue) переповнена! Подія {event.get('type')} втрачена.")
        except Exception as e:
            logger.error(f"AsyncBotCore: Помилка надсилання події {event.get('type')}: {e}", exc_info=True)

    # --- Тимчасовий синхронний метод get_status ---
    # TODO: Застарілий метод. Використовується лише в start()/stop() через синхронний _put_event_sync.
    # Замінити на асинхронний виклик, коли _put_event_sync стане асинхронним або буде інший механізм.
    def get_status(self) -> dict:
        """ Тимчасовий синхронний метод отримання статусу. """
        ws_conn = False
        if self._ws_handler:
            ws_connected_check = getattr(self._ws_handler, 'is_connected', getattr(self._ws_handler, 'connected', False))
            ws_conn = ws_connected_check() if callable(ws_connected_check) else ws_connected_check

        pos_active = False # Заглушка
        active_orders_count = 0 # Заглушка

        return {
            "is_running": self._is_running,
            "initialized": self._initialized,
            "active_mode": self.active_mode,
            "symbol": self.symbol,
            "strategy": self.selected_strategy_name,
            "websocket_connected": ws_conn,
            "position_active": pos_active, # Заглушка
            "active_orders_count": active_orders_count, # Заглушка
            "last_kline_update_time": self.last_kline_update_time,
            "start_time": self.start_time.isoformat() if self.start_time else None
        }
    # --- Кінець тимчасового методу ---

# END BLOCK 4


# --- Новий блок для публічних методів API ядра ---
# BLOCK 4.5: Public Async Methods
    async def get_status_async(self) -> dict:
        """
        Асинхронно збирає та повертає розширений статус бота.
        """
        logger.debug("get_status_async: Збір статусу...")
        status = {
            # Базовий стан
            "is_running": self._is_running,
            "initialized": self._initialized,
            "active_mode": self.active_mode,
            "symbol": self.symbol,
            "interval": self.interval,
            "strategy": self.selected_strategy_name,
            "start_time": self.start_time.isoformat() if self.start_time else None,
            # Стан WebSocket
            "websocket_connected": False,
            # Стан позиції
            "position_active": False,
            "position_details": None,
            # Активні ордери
            "active_orders_count": 0,
            "active_orders_details": None, # Поки що не додаємо деталі за замовчуванням
            # Останні дані
            "last_kline_update_time": self.last_kline_update_time,
        }

        try:
            # Перевірка WebSocket
            if self._ws_handler:
                ws_connected_check = getattr(self._ws_handler, 'is_connected', getattr(self._ws_handler, 'connected', False))
                status["websocket_connected"] = ws_connected_check() if callable(ws_connected_check) else ws_connected_check

            # Перевірка стану ініціалізації StateManager
            if self.state_manager and self.state_manager._initialized:
                # Отримання позиції
                position_decimal = await self.state_manager.get_open_position(self.symbol)
                if position_decimal:
                    status['position_active'] = True
                    # Конвертуємо типи для відповіді
                    status['position_details'] = self._convert_dict_types_for_event(position_decimal)

                # Отримання активних ордерів
                active_orders_decimal = await self.state_manager.get_all_active_orders(self.symbol)
                if active_orders_decimal is not None:
                    status['active_orders_count'] = len(active_orders_decimal)
                    # Опціонально: додати деталі ордерів
                    # status['active_orders_details'] = [self._convert_dict_types_for_event(o) for o in active_orders_decimal]
                else:
                     status['active_orders_count'] = 0 # Якщо отримали None
            else:
                 logger.warning("get_status_async: StateManager не ініціалізовано, не вдалося отримати позицію/ордери.")

        except StateManagerError as sme:
             logger.error(f"get_status_async: Помилка StateManager при зборі статусу: {sme}", exc_info=True)
             # Повертаємо частковий статус
        except Exception as e:
            logger.error(f"get_status_async: Неочікувана помилка при зборі статусу: {e}", exc_info=True)
            # Повертаємо частковий статус

        logger.debug(f"get_status_async: Статус зібрано: {status}")
        return status

# END BLOCK 4.5

# --- Потрібно додати реалізацію для обробника команд та _process_signal_async ---