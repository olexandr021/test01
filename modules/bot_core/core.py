# modules/bot_core/core.py
# Основний клас ядра бота (після рефакторингу StateManager).
# Керує загальною логікою, взаємодією компонентів, обробкою команд/подій.

# BLOCK 1: Imports and Class Definition Header
import threading
import queue
import time
import pandas as pd
from decimal import Decimal, ROUND_DOWN, ROUND_UP, getcontext, DivisionByZero, InvalidOperation
import json
from datetime import datetime, timezone, timedelta
import os
import shutil
import inspect
import copy
import logging # Для WS debug
from modules.config_manager import ConfigManager
from modules.binance_integration import BinanceAPI, BinanceWebSocketHandler, BinanceAPIError
from modules.logger import logger
from modules.strategies_tab import load_strategies
# Імпортуємо StateManager з того ж пакету bot_core
from .state_manager import StateManager, StateManagerError

# Імпортуємо базову стратегію
try:
    from strategies.base_strategy import BaseStrategy
except ImportError:
    logger.error("BotCore (Core): Не вдалося імпортувати BaseStrategy.")
    class BaseStrategy: # Клас-заглушка
        pass

# Встановлюємо точність Decimal (можна зробити один раз на рівні програми)
getcontext().prec = 28

class BotCoreError(Exception): pass

class BotCore:
    """
    Ядро торгового бота (після рефакторингу StateManager).
    Керує життєвим циклом, обробкою подій та команд, взаємодією компонентів.
    """
# END BLOCK 1
# BLOCK 2: Core Initialization (__init__)
    def __init__(self, config_manager: ConfigManager, command_queue: queue.Queue = None, event_queue: queue.Queue = None):
        logger.info("BotCore (Core): Ініціалізація...")
        self.config_manager = config_manager
        self.config = {} # Загальна конфігурація бота (завантажиться пізніше)

        # Ініціалізація черг
        if command_queue is None:
            self.command_queue = queue.Queue()
        else:
            self.command_queue = command_queue
        if event_queue is None:
            self.event_queue = queue.Queue()
        else:
            self.event_queue = event_queue

        # Створюємо StateManager - він завантажить стан з файлу
        self.state_manager = StateManager()

        # Ініціалізуємо компоненти як None спочатку
        self.binance_api: BinanceAPI | None = None
        self.strategy: BaseStrategy | None = None
        self.ws_handler: BinanceWebSocketHandler | None = None
        self.listen_key: str | None = None
        self.command_processor_thread: threading.Thread | None = None

        # Поточний стан роботи
        self.is_running = False
        self.start_time = None

        # Налаштування, що будуть завантажені з конфігу
        self.active_mode = 'TestNet'
        self.symbol = 'BTCUSDT'
        self.selected_strategy_name = None
        self.use_testnet = True
        self.symbol_info = None
        self.quote_asset = 'USDT'
        self.base_asset = 'BTC'
        self.price_precision = 8
        self.qty_precision = 8
        self.position_size_percent = Decimal('0.1') # Дефолт
        self.commission_taker = Decimal('0.00075') # Дефолт
        self.tp_percent = Decimal('0.01') # Дефолт
        self.sl_percent = Decimal('0.005') # Дефолт

        # Статистика сесії (залишається в Core)
        self.session_stats = self._reset_session_stats()

        # Буфер даних Klines (залишається в Core)
        # Доступ до нього має бути захищений локом StateManager
        self.klines_buffer = pd.DataFrame()
        self.required_klines_length = 100 # Буде оновлено стратегією
        self.last_kline_update_time = 0

        # Виконуємо основну послідовність ініціалізації
        try:
            # 1. Завантажуємо налаштування бота
            self._load_bot_settings()
            # 2. Ініціалізуємо API відповідно до режиму
            self._initialize_api()
            # Перевіряємо API
            if not self.binance_api:
                api_init_error = BotCoreError("Не ініціалізовано API")
                raise api_init_error
            # 3. Отримуємо інформацію про символ
            self._fetch_symbol_info()
            # Перевіряємо інфо про символ
            if not self.symbol_info:
                symbol_info_error = BotCoreError(f"Не отримано інфо про символ {self.symbol}")
                raise symbol_info_error
            # 4. Оновлюємо точність
            self._update_precisions_from_symbol_info()
            # 5. Завантажуємо клас стратегії
            self._load_strategy()
            # Примітка: Сам екземпляр стратегії створюється/перестворюється при старті/перезавантаженні
            # 6. Запускаємо обробник команд у фоновому потоці
            self._start_command_processor()

            # 7. Перевіряємо несумісність завантаженої позиції (якщо є) з поточним символом
            current_pos = self.state_manager.get_open_position()
            if current_pos:
                position_symbol = current_pos.get('symbol')
                symbols_match = (position_symbol == self.symbol)
                if not symbols_match:
                    logger.warning(f"Завантажена позиція {position_symbol} несумісна з поточним символом {self.symbol}, скинуто.")
                    # Скидаємо позицію через StateManager
                    self.state_manager.set_open_position(None)

        except Exception as init_e:
            # Логуємо критичну помилку
            logger.critical(f"Критична помилка ініціалізації BotCore: {init_e}", exc_info=True)
            # Зупиняємо Command Processor, якщо він встиг запуститись
            self._stop_command_processor()
            # Перекидаємо виняток далі, щоб init_bot_core в streamlit_app міг його зловити
            raise init_e

        logger.info("BotCore (Core): Ініціалізація завершена.")
# END BLOCK 2
# BLOCK 3: Session Statistics Methods
    def _reset_session_stats(self):
        """Скидає статистику поточної сесії."""
        stats = {}
        stats['total_trades'] = 0
        stats['winning_trades'] = 0
        stats['losing_trades'] = 0
        stats['total_pnl'] = Decimal(0)
        stats['total_commission'] = Decimal(0)
        stats['win_rate'] = 0.0
        stats['profit_factor'] = 0.0
        stats['total_profit'] = Decimal(0)
        stats['total_loss'] = Decimal(0)
        stats['avg_profit'] = Decimal(0)
        stats['avg_loss'] = Decimal(0)
        logger.debug("Статистику сесії скинуто.")
        return stats

    def _update_session_stats(self, pnl:Decimal, commission:Decimal):
        """
        Оновлює статистику сесії після закритої угоди.
        Примітка: Цей метод НЕ є потокобезпечним сам по собі,
        оскільки оновлює self.session_stats. Він має викликатися з контексту,
        де доступ до статистики вже синхронізовано (наприклад, з _handle_order_update,
        який сам виконується послідовно завдяки обробці подій WS).
        Якщо статистика буде оновлюватись з різних потоків, потрібен окремий lock.
        """
        # Отримуємо поточну статистику
        ss = self.session_stats

        # Оновлюємо загальні лічильники
        current_total_trades = ss.get('total_trades', 0)
        ss['total_trades'] = current_total_trades + 1

        current_total_pnl = ss.get('total_pnl', Decimal(0))
        ss['total_pnl'] = current_total_pnl + pnl

        current_total_commission = ss.get('total_commission', Decimal(0))
        ss['total_commission'] = current_total_commission + commission

        # Визначаємо результат угоди
        is_win = pnl > 0
        is_loss = pnl < 0

        # Оновлюємо лічильники прибуткових/збиткових угод та сум
        current_wins = ss.get('winning_trades', 0)
        current_losses = ss.get('losing_trades', 0)
        current_total_profit = ss.get('total_profit', Decimal(0))
        current_total_loss = ss.get('total_loss', Decimal(0))

        if is_win:
            ss['winning_trades'] = current_wins + 1
            ss['total_profit'] = current_total_profit + pnl
        elif is_loss:
            ss['losing_trades'] = current_losses + 1
            # Додаємо АБСОЛЮТНЕ значення збитку
            ss['total_loss'] = current_total_loss + abs(pnl)

        # Перераховуємо похідні метрики
        total_trades = ss['total_trades'] # Оновлене значення
        wins = ss['winning_trades']       # Оновлене значення
        losses = ss['losing_trades']     # Оновлене значення
        total_profit = ss['total_profit'] # Оновлене значення
        total_loss = ss['total_loss']     # Оновлене значення

        # Розрахунок Win Rate
        win_rate_val = 0.0
        if total_trades > 0:
            # Використовуємо Decimal для точності проміжних розрахунків
            win_rate_decimal = (Decimal(wins) / Decimal(total_trades)) * Decimal(100)
            # Конвертуємо в float для зберігання/відображення
            win_rate_val = float(win_rate_decimal)
        ss['win_rate'] = win_rate_val

        # Розрахунок Profit Factor
        profit_factor_val = 0.0
        try:
            if total_loss > 0:
                # Використовуємо Decimal для точності ділення
                profit_factor_decimal = total_profit / total_loss
                # Конвертуємо в float
                profit_factor_val = float(profit_factor_decimal)
            elif total_profit > 0:
                # Якщо збитків не було, але був прибуток -> нескінченність
                profit_factor_val = float('inf')
            # Інакше (прибуток=0, збиток=0) залишається 0.0
        except DivisionByZero:
            # Теоретично, перевірка total_loss > 0 має це покривати, але про всяк випадок
             if total_profit > 0:
                  profit_factor_val = float('inf')
             # else profit_factor_val remains 0.0
        ss['profit_factor'] = profit_factor_val

        # Розрахунок Avg Profit
        avg_profit_val = Decimal(0)
        if wins > 0:
             avg_profit_val = total_profit / Decimal(wins)
        ss['avg_profit'] = avg_profit_val # Зберігаємо як Decimal

        # Розрахунок Avg Loss
        avg_loss_val = Decimal(0)
        if losses > 0:
             avg_loss_val = total_loss / Decimal(losses)
        ss['avg_loss'] = avg_loss_val # Зберігаємо як Decimal

        # Логування оновленої статистики
        pnl_formatted = f"{ss['total_pnl']:.4f}" # Форматуємо для логу
        logger.info(f"Stats Updated: Trades={total_trades}, Wins={wins}, Losses={losses}, PnL={pnl_formatted}")
# END BLOCK 3
# BLOCK 4: Config and Component Initialization Methods
    def _load_bot_settings(self):
        """Завантажує та валідує налаштування бота."""
        logger.info("Завантаження налаштувань бота...")
        # Отримуємо конфіг з менеджера
        bot_config_data = self.config_manager.get_bot_config()
        # Зберігаємо повний конфіг на випадок потреби
        self.config = bot_config_data
        try:
            # --- Розмір позиції ---
            pos_size_conf = self.config.get('position_size_percent', 10.0)
            pos_size_dec = Decimal(str(pos_size_conf))
            pos_size_fraction = pos_size_dec / Decimal(100)
            self.position_size_percent = pos_size_fraction

            # --- Комісія ---
            comm_taker_conf = self.config.get('commission_taker', 0.075)
            comm_taker_dec = Decimal(str(comm_taker_conf))
            comm_taker_fraction = comm_taker_dec / Decimal(100)
            self.commission_taker = comm_taker_fraction

            # --- Take Profit ---
            tp_conf = self.config.get('take_profit_percent', 1.0)
            tp_dec = Decimal(str(tp_conf))
            tp_fraction = tp_dec / Decimal(100)
            self.tp_percent = tp_fraction

            # --- Stop Loss ---
            sl_conf = self.config.get('stop_loss_percent', 0.5)
            sl_dec = Decimal(str(sl_conf))
            sl_fraction = sl_dec / Decimal(100)
            self.sl_percent = sl_fraction

            # --- Символ ---
            symbol_conf = self.config.get('trading_symbol','BTCUSDT')
            symbol_upper = symbol_conf.upper()
            self.symbol = symbol_upper

            # --- Стратегія ---
            strategy_name_conf = self.config.get('selected_strategy')
            self.selected_strategy_name = strategy_name_conf

            # --- Режим роботи ---
            active_mode_conf = self.config.get('active_mode','TestNet')
            self.active_mode = active_mode_conf
            is_testnet_mode = (self.active_mode == 'TestNet')
            self.use_testnet = is_testnet_mode

            # --- Інтервал Klines ---
            kline_interval_conf = self.config.get('kline_interval', '1m')
            # Зберігаємо в self.config, використовуємо при потребі через self.config.get()

            # Логування завантажених параметрів
            pos_size_disp = self.position_size_percent * 100
            tp_disp = self.tp_percent * 100
            sl_disp = self.sl_percent * 100

            log_msg_1 = f" Налаштування OK: Mode={self.active_mode}, Sym={self.symbol}, Strat={self.selected_strategy_name}, KlineInterval={kline_interval_conf}"
            logger.info(log_msg_1)
            log_msg_2 = f"  Trading Params: PosSize={pos_size_disp:.1f}%, TP={tp_disp:.2f}%, SL={sl_disp:.2f}%"
            logger.info(log_msg_2)

        except Exception as e:
            logger.error(f"Помилка завантаження/конвертації налаштувань: {e}", exc_info=True)
            config_load_error = ValueError("Помилка завантаження налаштувань бота.")
            raise config_load_error

    def _initialize_api(self):
        """Ініціалізує BinanceAPI."""
        # Логуємо режим, для якого ініціалізуємо API
        current_mode = self.active_mode
        logger.info(f"Ініц. API: {current_mode}")
        try:
            # Створюємо екземпляр API
            # use_testnet визначається на основі active_mode
            is_testnet = self.use_testnet
            api_instance = BinanceAPI(use_testnet=is_testnet)

            # Перевіряємо з'єднання
            server_time = api_instance.get_server_time()
            # Якщо час не отримано, генеруємо помилку
            if not server_time:
                 connect_error = ConnectionError("Не вдалося отримати час сервера Binance.")
                 raise connect_error

            # Якщо все гаразд, зберігаємо екземпляр API
            self.binance_api = api_instance
            logger.info("API OK.")

        except Exception as e:
            # Логуємо помилку та залишаємо self.binance_api = None
            logger.error(f"Помилка ініціалізації Binance API: {e}", exc_info=True)
            self.binance_api = None

    def _fetch_symbol_info(self):
        """Отримує та зберігає інформацію про символ."""
        # Логуємо символ
        current_symbol = self.symbol
        logger.info(f"Запит інформації для символу {current_symbol}...")
        # Перевіряємо API
        api = self.binance_api
        if not api:
            logger.error("API не доступне для запиту інфо символу.")
            self.symbol_info = None
            return # Виходимо

        try:
            # Виконуємо запит
            exchange_info = api.get_exchange_info(symbol=current_symbol)
            # Аналізуємо відповідь
            symbols_data = None
            symbols_list_exists = False
            is_valid_response = False

            if exchange_info is not None:
                if 'symbols' in exchange_info:
                    symbols_data = exchange_info['symbols']
                    # Перевіряємо, що це список
                    if isinstance(symbols_data, list):
                         symbols_list_exists = True

            # Перевіряємо, чи отримали список з одним елементом
            if symbols_list_exists:
                 list_len = len(symbols_data)
                 if list_len == 1:
                      is_valid_response = True

            # Зберігаємо дані, якщо відповідь коректна
            if is_valid_response:
                symbol_data = symbols_data[0]
                self.symbol_info = symbol_data
                # Визначаємо базовий та котирувальний активи
                quote_asset_from_info = self.symbol_info.get('quoteAsset', 'USDT')
                base_asset_from_info = self.symbol_info.get('baseAsset', '')
                self.quote_asset = quote_asset_from_info
                self.base_asset = base_asset_from_info
                logger.info(f"Інформацію для символу {current_symbol} отримано.")
            else:
                # Якщо відповідь некоректна
                logger.error(f"Не отримано коректну інформацію для символу {current_symbol}. Відповідь API: {exchange_info}")
                self.symbol_info = None

        except Exception as e:
            # Якщо виникла будь-яка помилка
            logger.error(f"Помилка отримання інфо символу {current_symbol}: {e}", exc_info=True)
            self.symbol_info = None

    def _update_precisions_from_symbol_info(self):
        """Встановлює точність для ціни та кількості з symbol_info."""
        symbol_info_data = self.symbol_info
        if symbol_info_data:
            try:
                # Отримуємо значення точності (з дефолтом 8)
                quote_prec_val_str = symbol_info_data.get('quotePrecision', '8')
                base_prec_val_str = symbol_info_data.get('baseAssetPrecision', '8')

                # Конвертуємо в int та зберігаємо
                price_prec_int = int(quote_prec_val_str)
                qty_prec_int = int(base_prec_val_str)
                self.price_precision = price_prec_int
                self.qty_precision = qty_prec_int

                # Оновлюємо глобальну точність Decimal
                # Беремо максимум з 28 (стандарт Decimal) та суми точностей + запас
                new_decimal_prec = max(28, price_prec_int + qty_prec_int + 5)
                getcontext().prec = new_decimal_prec

                logger.info(f"Встановлено точність: Ціна={self.price_precision}, К-сть={self.qty_precision}")
            except Exception as e:
                logger.error(f"Помилка встановлення точності: {e}")
        else:
             logger.warning("Немає інформації про символ для встановлення точності. Використовуються значення за замовчуванням (8/8).")
             # Залишаємо дефолтні значення price_precision=8, qty_precision=8, встановлені в __init__

    def _load_strategy(self):
        """Завантажує та ініціалізує обрану стратегію."""
        strategy_name = self.selected_strategy_name
        logger.info(f"Завантаження стратегії: {strategy_name}...")

        # Перевіряємо, чи ім'я стратегії вказано
        if not strategy_name:
            logger.warning("Ім'я стратегії не обрано в конфігурації.")
            self.strategy = None
            return

        # Завантажуємо всі доступні класи стратегій
        available_strategies_dict = load_strategies()
        # Знаходимо потрібний клас за іменем
        strategy_class_to_load = available_strategies_dict.get(strategy_name)

        # Перевіряємо, чи клас знайдено
        if strategy_class_to_load:
            try:
                # Створюємо екземпляр стратегії, передаючи символ та менеджер конфігу
                strategy_instance = strategy_class_to_load(
                    symbol=self.symbol,
                    config_manager=self.config_manager
                )
                self.strategy = strategy_instance # Зберігаємо екземпляр
                logger.info(f"Екземпляр стратегії '{strategy_name}' створено.")

                # Визначаємо необхідну довжину історії Klines
                req_len_from_strategy = getattr(self.strategy, 'required_klines_length', None)
                if req_len_from_strategy is not None:
                     # Якщо стратегія явно вказує довжину
                     self.required_klines_length = req_len_from_strategy
                else:
                     # Інакше, пробуємо визначити на основі slow_period (якщо є)
                     slow_p_default = 99
                     slow_p = getattr(self.strategy, 'slow_period', slow_p_default)
                     # Додаємо 1 для можливості розрахунку попереднього значення MA
                     self.required_klines_length = slow_p + 1
                logger.info(f"Встановлено required_klines_length = {self.required_klines_length} для стратегії {strategy_name}.")

            except Exception as e:
                logger.error(f"Помилка ініціалізації екземпляра стратегії '{strategy_name}': {e}", exc_info=True)
                self.strategy = None # Скидаємо стратегію у разі помилки
        else:
            # Якщо клас не знайдено
            logger.error(f"Клас стратегії '{strategy_name}' не знайдено в папці 'strategies'.")
            self.strategy = None
# END BLOCK 4
# BLOCK 5: Initial Data Fetching Methods (User's Version + Configurable Kline Interval)
    def _fetch_initial_data(self):
        """Завантажує початкові дані (баланс, свічки)."""
        logger.info("Завантаження початкових даних (баланс, свічки)...")
        # Послідовно викликаємо методи для завантаження
        self._fetch_initial_balance()
        self._fetch_initial_klines() # Цей метод тепер використовуватиме конфіг
        logger.debug("Завершено спробу завантаження початкових даних.")

    def _fetch_initial_balance(self):
        """Отримує та зберігає початковий стан балансів через StateManager."""
        logger.debug("Запит початкових балансів...")
        # Перевіряємо наявність API
        api = self.binance_api
        if not api:
            logger.error("API не доступне для запиту балансу.")
            return # Виходимо, якщо немає API
        try:
            # Отримуємо інформацію про акаунт
            acc_info = api.get_account_info()
            # Перевіряємо відповідь
            balances_key_exists = False
            balances_data = None
            if acc_info:
                if 'balances' in acc_info:
                    balances_data = acc_info['balances']
                    # Переконуємось, що це список
                    if isinstance(balances_data, list):
                         balances_key_exists = True

            if balances_key_exists:
                # Якщо дані отримано коректно
                new_balance_dict = {}
                # Обробляємо кожен актив у відповіді
                for item in balances_data:
                    asset_name = item.get('asset')
                    free_str = item.get('free', '0') # Безпечний доступ
                    locked_str = item.get('locked', '0') # Безпечний доступ
                    # Обробляємо тільки якщо є назва активу
                    if asset_name:
                         try:
                              # Конвертуємо рядки в Decimal
                              free_bal = Decimal(free_str)
                              locked_bal = Decimal(locked_str)
                              # Перевіряємо, чи баланс ненульовий
                              is_positive_balance = free_bal > 0 or locked_bal > 0
                              if is_positive_balance:
                                   # Додаємо до нового словника балансів
                                   balance_entry = {'free': free_bal, 'locked': locked_bal}
                                   new_balance_dict[asset_name] = balance_entry
                         except InvalidOperation:
                              # Логуємо помилку конвертації для конкретного активу
                              logger.warning(f"Некоректне значення балансу для {asset_name}: free='{free_str}', locked='{locked_str}'")

                # Встановлюємо новий стан балансів через StateManager (потокобезпечно)
                self.state_manager.set_balances(new_balance_dict)

                # Логуємо основний баланс (зчитуємо оновлений стан)
                current_balances = self.state_manager.get_balances()
                quote_balance_data = current_balances.get(self.quote_asset, {})
                free_quote_bal = quote_balance_data.get('free', Decimal(0))
                quote_balance_str = f"{free_quote_bal:.8f}" # Форматуємо для логу
                logger.info(f"Баланси успішно завантажено та оновлено. {self.quote_asset}: {quote_balance_str}")
            else:
                # Якщо відповідь API некоректна
                logger.error("Не отримано коректну інформацію про баланси з API.")
        except Exception as e:
            # Якщо виникла будь-яка інша помилка
            logger.error(f"Помилка отримання початкових балансів: {e}", exc_info=True)

    def _fetch_initial_klines(self):
        """Завантажує початкову історію свічок та зберігає у буфер."""
        logger.info("Завантаження початкових Klines...")
        # Перевіряємо наявність API та стратегії
        api_ready = self.binance_api is not None
        strategy_ready = self.strategy is not None
        if not api_ready: logger.error("API не готове для завантаження Klines."); return
        if not strategy_ready: logger.error("Стратегія не готова для визначення довжини Klines."); return

        # <<< ЗМІНА: Читаємо інтервал з self.config >>>
        kline_interval = self.config.get('kline_interval', '1m')
        logger.info(f"_fetch_initial_klines: Використання інтервалу {kline_interval}")
        # <<< КІНЕЦЬ ЗМІНИ >>>

        # Визначаємо ліміт
        limit = self.required_klines_length + 20
        try:
            # <<< ЗМІНА: Використовуємо змінну kline_interval >>>
            # Запитуємо історичні дані
            klines_data = self.binance_api.get_historical_klines(
                symbol=self.symbol,
                interval=kline_interval, # Використовуємо отриманий інтервал
                limit=limit
            )
            # <<< КІНЕЦЬ ЗМІНИ >>>

            # Перевіряємо результат
            klines_received = klines_data is not None
            enough_klines = False
            received_count = 0
            if klines_received:
                 received_count = len(klines_data)
                 enough_klines = received_count >= self.required_klines_length

            # Якщо отримали достатньо даних
            if enough_klines:
                # Створюємо DataFrame
                cols_api = ['t','o','h','l','c','v','ct','qav','n','tbv','tqv','i']
                # Беремо тільки ті колонки, що реально прийшли
                actual_cols = cols_api[:len(klines_data[0])]
                df = pd.DataFrame(klines_data, columns=actual_cols)
                # Перейменовуємо колонки
                column_mapping = {
                    't':'timestamp', 'o':'open', 'h':'high', 'l':'low', 'c':'close',
                    'v':'volume', 'n':'num_trades', 'qav':'quote_asset_volume',
                    'tbv':'taker_base_vol', 'tqv':'taker_quote_vol'
                }
                df.rename(columns=column_mapping, inplace=True)

                # Обробляємо timestamp
                df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms', utc=True)
                df.set_index('timestamp', inplace=True)

                # Конвертуємо числові колонки в Decimal
                num_cols_to_convert = [
                    'open','high','low','close','volume','quote_asset_volume',
                    'num_trades','taker_base_vol','taker_quote_vol'
                ]
                for col_name in num_cols_to_convert:
                    if col_name in df.columns:
                        col_data = df[col_name]
                        # Функція для безпечної конвертації
                        def safe_to_decimal_kline(x):
                            if pd.notna(x):
                                try: return Decimal(str(x))
                                except InvalidOperation: return Decimal('NaN')
                            else: return Decimal('NaN')
                        # Застосовуємо конвертацію
                        converted_col = pd.to_numeric(col_data, errors='coerce').apply(safe_to_decimal_kline)
                        df[col_name] = converted_col

                # Видаляємо рядки з NaN в основних цінових колонках
                df.dropna(subset=['open','high','low','close'], inplace=True)

                # Зберігаємо результат у буфер під блокуванням StateManager
                with self.state_manager.lock:
                    # Беремо останні N рядків, що залишились
                    final_df_slice = df.iloc[-limit:]
                    self.klines_buffer = final_df_slice
                    # Перевіряємо, чи буфер не порожній
                    buffer_is_empty = self.klines_buffer.empty
                    if not buffer_is_empty:
                        # Оновлюємо час останнього оновлення
                        last_timestamp_in_buffer = self.klines_buffer.index[-1]
                        self.last_kline_update_time = last_timestamp_in_buffer.value // 10**6
                        buffer_len = len(self.klines_buffer)
                        logger.info(f"Збережено {buffer_len} початкових свічок у буфер.")
                    else:
                        logger.error("Буфер Klines порожній після видалення NaN.")
            else:
                # Якщо отримали недостатньо свічок
                logger.error(f"Не завантажено достатньо ({received_count}/{limit}) свічок для інтервалу {kline_interval}.")
                # Очищаємо буфер
                with self.state_manager.lock:
                     self.klines_buffer = pd.DataFrame()
        except Exception as e:
            # Обробляємо будь-які помилки
            logger.error(f"Помилка завантаження/обробки початкових Klines: {e}", exc_info=True)
            # Очищаємо буфер у разі помилки
            with self.state_manager.lock:
                 self.klines_buffer = pd.DataFrame()
# END BLOCK 5
# BLOCK 6: OCO Check and Restore Method (Using get_order_list - NameError Fix)
    def _check_and_restore_oco(self):
        """
        Перевіряє OCO для існуючої позиції (з StateManager)
        та розміщує новий, якщо потрібно. Додає знайдені активні частини
        OCO до стану активних ордерів через StateManager.
        """
        logger.info("Перевірка стану OCO для відкритої позиції...")
        # Крок 1: Отримуємо поточну відкриту позицію (потокобезпечно)
        pos = self.state_manager.get_open_position()

        # Крок 2: Перевіряємо, чи існує позиція
        if pos is None: # Явніша перевірка на None
            logger.debug("_check_and_restore_oco: Відкритих позицій немає для перевірки OCO.")
            return # Виходимо, якщо немає позиції

        # Крок 3: Отримуємо дані позиції
        pos_symbol = pos.get('symbol')
        pos_qty = pos.get('quantity')
        pos_entry_price = pos.get('entry_price')
        # Отримуємо збережений ID OCO (це може бути listClientOrderId або orderListId)
        oco_id = pos.get('oco_list_id')

        # Перевіряємо критичні дані позиції
        has_symbol = pos_symbol is not None
        has_qty = False
        if pos_qty is not None:
             if isinstance(pos_qty, Decimal): # Перевірка типу
                  if pos_qty > 0:
                       has_qty = True
        has_price = False
        if pos_entry_price is not None:
             if isinstance(pos_entry_price, Decimal): # Перевірка типу
                  if pos_entry_price > 0:
                       has_price = True

        # Перевіряємо всі прапорці
        all_data_present = has_symbol and has_qty and has_price
        if not all_data_present:
             logger.error(f"_check_and_restore_oco: Неповні або некоректні дані позиції: {pos}")
             return # Не можемо працювати без основних даних

        # Крок 4: Перевіряємо валідність OCO ID та викликаємо API
        found_active_oco = False # Прапорець знайденого активного OCO
        api_error_occurred = False # Прапорець помилки API під час перевірки

        # Перевіряємо, чи є ID для запиту
        oco_id_is_valid = False
        if oco_id is not None:
             if oco_id != -1: # -1 стандартне значення Binance
                  oco_id_str_check = str(oco_id).strip()
                  if oco_id_str_check != "": # Перевірка на порожній рядок
                       oco_id_is_valid = True

        # Крок 5: Якщо є валідний OCO ID, перевіряємо його статус на біржі
        if oco_id_is_valid:
            oco_id_log_str = str(oco_id) # Використовуємо цю змінну для логування
            logger.info(f"_check_and_restore_oco: Позиція {pos_symbol} має OCO ID: {oco_id_log_str}. Запит статусу списку...")
            try:
                # Готуємо параметри запиту get_order_list
                query_params = {}
                # Пріоритетно шукаємо за listClientOrderId, якщо він схожий на наш
                if oco_id_log_str.startswith("bot_"):
                    query_params['listClientOrderId'] = oco_id_log_str
                elif oco_id_log_str.isdigit(): # Якщо це число, припускаємо, що це orderListId
                    query_params['orderListId'] = int(oco_id_log_str)
                else: # Якщо це інший рядок, спробуємо як origClientOrderId (або listClientOrderId)
                     # Binance API дозволяє використовувати origClientOrderId замість listClientOrderId
                     query_params['origClientOrderId'] = oco_id_log_str # Спробуємо так

                # Перевіряємо, чи вдалося сформувати параметри
                if not query_params:
                     id_query_error = ValueError(f"Не вдалося визначити тип ID для запиту OCO: {oco_id_log_str}")
                     raise id_query_error

                # Викликаємо новий метод API
                oco_info = self.binance_api.get_order_list(**query_params)

                # --- Аналізуємо відповідь ---
                if oco_info is None:
                    # Помилка API при запиті статусу OCO
                    logger.error(f"_check_and_restore_oco: Помилка API при запиті статусу OCO ID: {oco_id_log_str}.")
                    api_error_occurred = True
                    found_active_oco = True # Вважаємо активним, щоб не ставити новий

                elif isinstance(oco_info, dict) and not oco_info:
                    # API повернуло порожній словник - OCO не знайдено
                    logger.warning(f"_check_and_restore_oco: OCO ордер з ID {oco_id_log_str} не знайдено на біржі.")
                    found_active_oco = False # Активного OCO немає
                    # Очищаємо OCO ID у позиції
                    self.state_manager.update_open_position_field('oco_list_id', None)

                elif isinstance(oco_info, dict) and 'listOrderStatus' in oco_info:
                    # Отримали інформацію про OCO
                    oco_status = oco_info.get('listOrderStatus')
                    logger.info(f"_check_and_restore_oco: Статус OCO списку {oco_id_log_str}: {oco_status}")

                    if oco_status == 'EXECUTING':
                        logger.info(f"_check_and_restore_oco: OCO {oco_id_log_str} активний. Оновлення стану активних ордерів...")
                        found_active_oco = True
                        orders_in_list = oco_info.get('orders', [])
                        active_orders_found_count = 0
                        # Оновлюємо або додаємо активні частини OCO в StateManager
                        for order_data in orders_in_list:
                            order_status = order_data.get('status')
                            is_order_active = order_status in ['NEW', 'PARTIALLY_FILLED']
                            if is_order_active:
                                client_order_id = order_data.get('clientOrderId')
                                binance_order_id = order_data.get('orderId')
                                order_type = order_data.get('type')
                                internal_id = client_order_id # Пріоритет нашому ID
                                is_our_cid = False
                                if internal_id: is_our_cid = internal_id.startswith('bot_')
                                if not is_our_cid: internal_id = binance_order_id

                                if internal_id:
                                    purpose = 'TP' if order_type == 'LIMIT_MAKER' else ('SL' if order_type == 'STOP_LOSS_LIMIT' else 'UNKNOWN_OCO')
                                    order_symbol = order_data.get('symbol')
                                    order_side = order_data.get('side')
                                    orig_qty_str = order_data.get('origQty', '0')
                                    price_str = order_data.get('price','0')
                                    stop_price_str = order_data.get('stopPrice','0')
                                    list_cid_from_resp = oco_info.get('listClientOrderId')
                                    list_id_from_resp = oco_info.get('orderListId')

                                    active_order_details = {
                                        'symbol': order_symbol, 'order_id_binance': binance_order_id,
                                        'client_order_id': client_order_id, 'list_client_order_id': list_cid_from_resp,
                                        'order_list_id_binance': list_id_from_resp, 'side': order_side,
                                        'type': order_type, 'quantity_req': Decimal(orig_qty_str),
                                        'price': Decimal(price_str), 'stop_price': Decimal(stop_price_str),
                                        'status': order_status, 'purpose': purpose
                                    }
                                    self.state_manager.add_or_update_active_order(internal_id, active_order_details)
                                    logger.debug(f"_check_and_restore_oco: Оновлено активний ордер {internal_id} ({purpose}) зі статусом {order_status}")
                                    active_orders_found_count = active_orders_found_count + 1
                                else:
                                     logger.warning(f"_check_and_restore_oco: Не вдалося визначити internal_id для ордера {binance_order_id}")
                        logger.info(f"_check_and_restore_oco: Знайдено {active_orders_found_count} активних ордерів у OCO списку {oco_id_log_str}.")
                        # Переконуємось, що знайдено хоча б один, якщо статус EXECUTING
                        if active_orders_found_count == 0:
                             logger.warning(f"Статус OCO {oco_id_log_str} = EXECUTING, але не знайдено активних ордерів у списку orders!")
                             # Можливо, варто вважати, що OCO неактивний?
                             # found_active_oco = False # Поки що залишаємо True, як заявлено статусом списку

                    elif oco_status in ['ALL_DONE', 'REJECT']:
                        logger.critical(f"_check_and_restore_oco: OCO {oco_id_log_str} має статус {oco_status}, але позиція {pos_symbol} все ще відкрита!")
                        self.state_manager.update_open_position_field('oco_list_id', None)
                        found_active_oco = False # OCO не активний
                        critical_msg = f"КРИТИЧНО: OCO {oco_id_log_str} статус {oco_status}, але позиція {pos_symbol} відкрита!"
                        critical_payload = {'source':'BotCore._check_and_restore_oco', 'message': critical_msg}
                        self._put_event({'type':'CRITICAL_ERROR', 'payload': critical_payload})
                        # Розглянути аварійний вихід тут?

                    else: # Інші статуси (EXPIRED тощо)
                        logger.warning(f"_check_and_restore_oco: OCO {oco_id_log_str} має неочікуваний статус: {oco_status}. Вважаємо неактивним.")
                        self.state_manager.update_open_position_field('oco_list_id', None)
                        found_active_oco = False

                else: # Якщо відповідь API мала несподіваний формат
                     logger.error(f"_check_and_restore_oco: Несподіваний формат відповіді від get_order_list: {oco_info}")
                     api_error_occurred = True
                     found_active_oco = True # Не розміщуємо новий OCO

            except Exception as e:
                # Якщо будь-яка інша помилка під час перевірки
                logger.error(f"_check_and_restore_oco: Загальна помилка перевірки OCO: {e}", exc_info=True)
                logger.critical(f"_check_and_restore_oco: НЕ ВДАЛОСЯ ПЕРЕВІРИТИ/ВІДНОВИТИ OCO!")
                api_error_occurred = True
                found_active_oco = True # Не розміщуємо новий OCO
                error_message_oco_check = f"Загальна помилка перевірки/відновлення OCO: {e}"
                error_payload_check = {'source':'BotCore._check_and_restore_oco', 'message': error_message_oco_check}
                self._put_event({'type':'CRITICAL_ERROR', 'payload': error_payload_check})

        # --- Крок 6: Розміщуємо новий OCO, якщо потрібно ---
        should_place_new_oco = False
        # Розміщуємо, тільки якщо:
        # 1) Не було помилки API під час перевірки
        # 2) АБО OCO ID був невалідний СПОЧАТКУ
        #    АБО перевірка показала, що активного OCO НЕМАЄ (found_active_oco = False)
        if not api_error_occurred:
             if not oco_id_is_valid:
                  should_place_new_oco = True
             elif not found_active_oco:
                  should_place_new_oco = True

        if should_place_new_oco:
            reason_log = "немає валідного OCO ID" if not oco_id_is_valid else f"активних частин OCO {str(oco_id)} не знайдено" # Використовуємо str(oco_id)
            logger.warning(f"_check_and_restore_oco: Позиція існує, але {reason_log}. Розміщення нового OCO...")
            # Запускаємо розміщення нового OCO в окремому потоці
            oco_thread_name_missing = f"OCOPlaceOnRestore-{pos_symbol}"
            oco_thread_missing = threading.Thread(
                target=self._place_oco_order,
                args=(pos_symbol, pos_qty, pos_entry_price),
                daemon=True,
                name=oco_thread_name_missing
            )
            oco_thread_missing.start()
        # Логуємо завершення перевірки, якщо не було розміщення нового ордера
        elif not api_error_occurred and found_active_oco:
             oco_id_log_final = str(oco_id) # Використовуємо str(oco_id)
             logger.info(f"_check_and_restore_oco: Перевірка OCO завершена. Активний OCO ({oco_id_log_final}) існує.")
        elif api_error_occurred:
             logger.error("_check_and_restore_oco: Перевірку OCO завершено з помилкою API. Новий OCO не розміщено.")
# END BLOCK 6
# BLOCK 6.5: Lifecycle Methods (start, stop) - !! Corrected Ternary Operators + Config Kline Interval !!
    def start(self):
        """Запускає основну логіку бота та WebSocket."""
        # Перевіряємо, чи бот вже запущено
        already_running = self.is_running
        if already_running:
            logger.warning("Спроба запуску, але ядро вже запущено.")
            return

        # Перевіряємо готовність основних компонентів
        api_ok = self.binance_api is not None
        strategy_ok = self.strategy is not None
        symbol_info_ok = self.symbol_info is not None
        components_ready = api_ok and strategy_ok and symbol_info_ok

        if not components_ready:
            error_msg_comp = f"Не готові компоненти для запуску (API:{api_ok}, Strat:{strategy_ok}, SymInfo:{symbol_info_ok})"
            logger.error(error_msg_comp)
            error_payload_comp = {'source':'BotCore.start', 'message':error_msg_comp}
            self._put_event({'type':'ERROR', 'payload': error_payload_comp})
            return

        logger.info(f"=== СПРОБА ЗАПУСКУ BotCore {self.symbol}... ===")
        try:
            # Крок 1: Перезавантаження налаштувань та перевірка/ініціалізація API
            logger.debug("start(): Перевірка/оновлення налаштувань та API...")
            self._load_bot_settings() # Завантажує актуальні налаштування, включаючи kline_interval
            api_needs_reinit = False
            api_instance = self.binance_api # Отримуємо поточний екземпляр
            if api_instance:
                 # Перевіряємо, чи збігається use_testnet в API з активним режимом
                 is_api_testnet = api_instance.use_testnet
                 is_config_testnet = (self.active_mode == 'TestNet')
                 # Потребує переініціалізації, якщо режими не збігаються
                 api_needs_reinit = (is_api_testnet != is_config_testnet)
            else:
                 # Якщо API немає, його точно треба ініціалізувати
                 api_needs_reinit = True
                 logger.warning("start(): BinanceAPI не було ініціалізовано раніше. Спроба ініціалізації...")

            if api_needs_reinit:
                 # Визначаємо поточний режим API для логування
                 current_api_mode_str = 'None' # Значення за замовчуванням
                 if self.binance_api:
                      if self.binance_api.use_testnet:
                           current_api_mode_str = 'TestNet'
                      else:
                           current_api_mode_str = 'MainNet'
                 # Логуємо потребу переініціалізації
                 logger.warning(f"start(): Режим конфігурації ({self.active_mode}) не співпадає з поточним режимом API ({current_api_mode_str}) або API відсутнє. Переініціалізація API...")
                 self._initialize_api() # Спробуємо ініціалізувати знову

            # Остаточна перевірка API
            if not self.binance_api:
                 # Якщо і після спроби ініціалізації API немає
                 api_final_error = BotCoreError("Помилка API після перевірки/спроби ініціалізації.")
                 logger.critical("start(): BinanceAPI не ініціалізовано.")
                 raise api_final_error # Генеруємо помилку

            # Визначаємо рядок режиму API для логування
            if self.binance_api.use_testnet:
                 api_mode_str = 'TestNet'
            else:
                 api_mode_str = 'MainNet'
            logger.debug(f"start(): API ({api_mode_str}) готове.")

            # Крок 2: Завантаження початкових даних
            logger.debug("start(): Завантаження початкових даних...")
            self._fetch_initial_data() # Цей метод вже використовує kline_interval з конфігу
            self.session_stats = self._reset_session_stats() # Скидаємо статистику при старті
            logger.debug("start(): Початкові дані завантажено (або спроба зроблена).")

            # Крок 3: Перевірка відкритої позиції та OCO
            pos_to_check = self.state_manager.get_open_position() # Отримуємо з StateManager
            if pos_to_check:
                logger.warning(f"start(): Запуск з ВІДКРИТОЮ позицією! Перевірка OCO...")
                self._check_and_restore_oco() # Цей метод теж використовує StateManager
            else:
                logger.debug("start(): Відкритих позицій для перевірки OCO не знайдено.")

            # Крок 4: Запуск User Data Stream
            logger.debug("start(): Запуск User Data Stream...")
            new_listen_key = self.binance_api.start_user_data_stream()
            # Перевіряємо, чи отримано ключ
            if not new_listen_key:
                 listen_key_error = BotCoreError("Не вдалося отримати listenKey.")
                 logger.critical("start(): Не вдалося отримати listenKey від Binance API.")
                 raise listen_key_error # Генеруємо помилку
            # Зберігаємо ключ
            self.listen_key = new_listen_key
            listen_key_preview = self.listen_key[:5] # Для логу
            logger.info(f"start(): Отримано listenKey: {listen_key_preview}...")

            # Крок 5: Ініціалізація WebSocket Handler
            logger.debug("start(): Ініціалізація WebSocket Handler...")
            # Отримуємо інтервал з конфігу
            kline_interval_from_config = self.config.get('kline_interval', '1m')
            logger.info(f"start(): Використання інтервалу свічок для WS: {kline_interval_from_config}")
            # Формуємо список потоків (з конфігурованим інтервалом)
            stream_symbol = self.symbol.lower()
            kline_stream_name = f"{stream_symbol}@kline_{kline_interval_from_config}"
            ws_streams = [kline_stream_name]
            # Словник callback-функцій
            ws_callbacks = {
                'kline': self._handle_kline_update,
                'order_update': self._handle_order_update,
                'balance_update': self._handle_balance_update
            }
            # Створюємо екземпляр WS Handler
            ws_handler_instance = BinanceWebSocketHandler(
                streams=ws_streams,
                callbacks=ws_callbacks,
                use_testnet=self.use_testnet, # Використовуємо поточний режим ядра
                api=self.binance_api,
                ws_log_level=logging.DEBUG # Залишаємо DEBUG для діагностики
            )
            # Зберігаємо екземпляр
            self.ws_handler = ws_handler_instance
            logger.debug("start(): Встановлення listenKey для WS Handler...")
            # Встановлюємо ключ (це також запустить потік оновлення ключа)
            self.ws_handler.set_listen_key(self.listen_key)

            # Крок 5.5: ЗАПУСК ПОТОКУ WebSocket Handler
            logger.info("start(): Запуск основного потоку WebSocket Handler...")
            self.ws_handler.start() # Запускаємо основний потік WS

            # Крок 6: Очікування підключення WebSocket
            ws_connect_timeout = 10 # Секунд
            logger.info(f"start(): Очікування підключення WebSocket ({ws_connect_timeout} сек)...")
            wait_start_time = time.time()
            ws_connected = False
            # Цикл очікування
            while True:
                 current_time = time.time()
                 elapsed_time = current_time - wait_start_time
                 # Перевіряємо таймаут
                 if elapsed_time >= ws_connect_timeout:
                      break # Виходимо, якщо час вийшов
                 # Перевіряємо з'єднання
                 current_ws_handler = self.ws_handler
                 if current_ws_handler:
                      is_conn = current_ws_handler.is_connected()
                      if is_conn:
                           ws_connected = True
                           break # Виходимо, якщо підключено
                 # Невелика пауза
                 time.sleep(0.5)

            # Перевіряємо результат очікування
            if not ws_connected:
                 ws_connect_error = BotCoreError(f"WebSocket Handler не зміг підключитися протягом {ws_connect_timeout} секунд!")
                 logger.critical(ws_connect_error)
                 # Зупиняємо обробник, якщо він був створений
                 if self.ws_handler:
                     self.ws_handler.stop()
                     self.ws_handler = None
                 raise ws_connect_error # Генеруємо помилку
            # Якщо підключилися
            logger.info("start(): WebSocket Handler успішно підключено (перевірено).")

            # Крок 7: Перевірка стану Command Processor
            command_processor_ok = False
            current_cmd_thread = self.command_processor_thread
            if current_cmd_thread:
                is_cmd_thread_alive = current_cmd_thread.is_alive()
                if is_cmd_thread_alive:
                    command_processor_ok = True

            if not command_processor_ok:
                 logger.critical("start(): Потік обробника команд НЕ активний перед фінальним запуском!")
                 # Спробуємо запустити його ще раз
                 self._start_command_processor()
                 # Перевіряємо знову
                 command_processor_ok_after_restart = False
                 restarted_cmd_thread = self.command_processor_thread
                 if restarted_cmd_thread:
                     is_restarted_alive = restarted_cmd_thread.is_alive()
                     if is_restarted_alive:
                          command_processor_ok_after_restart = True

                 if not command_processor_ok_after_restart:
                      cmd_proc_error = BotCoreError("Не вдалося запустити потік обробника команд!")
                      raise cmd_proc_error
                 else:
                      logger.warning("start(): Потік обробника команд був перезапущений.")

            # Крок 8: Встановлення фінального статусу та часу
            self.is_running = True
            self.start_time = datetime.now(timezone.utc)
            start_time_str = self.start_time.strftime('%Y-%m-%d %H:%M:%S %Z')
            logger.info(f"=== BotCore УСПІШНО ЗАПУЩЕНО о {start_time_str} ===")
            # Надсилаємо подію про успішний запуск
            status_payload = self.get_status()
            status_update_event = {'type':'STATUS_UPDATE', 'payload': status_payload}
            self._put_event(status_update_event)

        except Exception as start_e:
            # Обробка будь-яких помилок під час запуску
            logger.critical(f"Критична помилка під час старту BotCore: {start_e}", exc_info=True)
            logger.info("start(): Викликаємо self.stop() через помилку...")
            # Викликаємо повну зупинку для очищення ресурсів
            self.stop()
            # Надсилаємо подію про помилку в UI/Telegram
            error_message_start = f"Помилка старту: {start_e}"
            error_payload = {'source':'BotCore.start', 'message': error_message_start}
            self._put_event({'type':'ERROR', 'payload': error_payload})

    def stop(self):
        """Зупиняє WebSocket, обробник команд та зберігає стан."""
        stop_time = datetime.now(timezone.utc)
        is_already_stopped = not self.is_running
        # Логуємо початок зупинки
        if is_already_stopped:
            logger.warning("Спроба зупинки BotCore, хоча is_running = False...")
        else:
            logger.info("Зупинка BotCore...")

        # Встановлюємо прапорець is_running в False перш за все
        self.is_running = False

        # Зупиняємо WebSocket Handler
        ws_handler_instance = self.ws_handler
        if ws_handler_instance:
            logger.debug("Зупинка WebSocket Handler...")
            ws_handler_instance.stop()
            self.ws_handler = None # Скидаємо посилання
        else:
             logger.debug("WebSocket Handler не був активний при зупинці.")

        # Закриваємо Listen Key на біржі
        current_listen_key = self.listen_key
        api_instance = self.binance_api
        if current_listen_key and api_instance:
            key_preview = current_listen_key[:5]
            logger.debug(f"Закриття Listen Key: {key_preview}...")
            try:
                # Викликаємо метод API для закриття потоку
                api_instance.close_user_data_stream(current_listen_key)
                logger.info(f"Listen Key {key_preview}... успішно закрито.")
            except Exception as e_close_lk:
                # Логуємо помилку, але продовжуємо зупинку
                logger.error(f"Помилка при закритті listen key: {e_close_lk}")
            # Скидаємо ключ у будь-якому випадку
            self.listen_key = None
        else:
             logger.debug("Listen Key не був активний або API відсутнє при зупинці.")

        # Зупиняємо Command Processor
        self._stop_command_processor()

        # Зберігаємо персистентний стан (позиція, історія)
        # Викликаємо метод StateManager для збереження
        self.state_manager._save_state_to_file()

        # Логуємо фінальний стан та статистику
        final_stats = self.get_session_stats() # Отримуємо статистику сесії
        stop_time_str = stop_time.strftime('%Y-%m-%d %H:%M:%S %Z')
        logger.info(f"BotCore зупинено о {stop_time_str}.")
        # Використовуємо json.dumps з default=str для безпечного логування статистики
        stats_json_str = json.dumps(final_stats, indent=2, default=str)
        logger.info(f"Статистика сесії: {stats_json_str}")
        # Надсилаємо фінальний статус (має показати is_running=False)
        final_status_payload = self.get_status()
        status_update_event = {'type':'STATUS_UPDATE', 'payload': final_status_payload}
        self._put_event(status_update_event)
# END BLOCK 6.5
# BLOCK 7: Command Processor Thread Management
    def _start_command_processor(self):
        """Запускає потік обробника команд, якщо він ще не запущений."""
        # Перевіряємо, чи потік вже існує та активний
        processor_thread_exists = self.command_processor_thread is not None
        processor_thread_alive = False
        if processor_thread_exists:
             current_thread = self.command_processor_thread
             processor_thread_alive = current_thread.is_alive()

        # Якщо потік вже працює, нічого не робимо
        if processor_thread_alive:
             logger.warning("Спроба запустити command_processor_thread, коли він вже працює.")
             return

        logger.debug("Запуск потоку обробника команд...")
        # Створюємо новий потік
        new_thread = threading.Thread(
            target=self._command_processor_loop,
            daemon=True, # Робимо потік демоном
            name="CommandProcessor"
        )
        # Зберігаємо посилання на новий потік
        self.command_processor_thread = new_thread
        # Запускаємо потік
        self.command_processor_thread.start()

        # Невелика пауза, щоб дати потоку час запуститись
        time.sleep(0.1)

        # Перевіряємо, чи новий потік справді запустився
        is_new_thread_alive = False
        if self.command_processor_thread: # Перевірка на None
             is_new_thread_alive = self.command_processor_thread.is_alive()

        if not is_new_thread_alive:
            logger.critical("Не вдалося запустити потік обробника команд!")
            # Скидаємо посилання, якщо запуск не вдався
            self.command_processor_thread = None
            # Генеруємо помилку
            cmd_proc_start_error = BotCoreError("Не вдалося запустити потік обробника команд.")
            raise cmd_proc_start_error
        else:
             logger.info("Потік обробника команд успішно запущено.")

    def _stop_command_processor(self):
        """Коректно зупиняє потік обробника команд."""
        # Отримуємо посилання на поточний потік
        thread_to_stop = self.command_processor_thread
        # Перевіряємо, чи потік існує
        thread_exists = thread_to_stop is not None
        thread_alive = False
        # Якщо існує, перевіряємо, чи він активний
        if thread_exists:
             thread_alive = thread_to_stop.is_alive()

        # Зупиняємо тільки якщо потік активний
        if thread_alive:
            logger.debug("Зупинка обробника команд...")
            # is_running вже має бути False на момент виклику stop() -> _stop_command_processor()
            try:
                 # Надсилаємо спеціальну команду, щоб розблокувати command_queue.get()
                 command_queue_exists = self.command_queue is not None
                 if command_queue_exists:
                     # Використовуємо put з невеликим таймаутом
                     try:
                          stop_command = {'type':'STOP_INTERNAL'}
                          self.command_queue.put(stop_command, timeout=0.5)
                     except queue.Full:
                           logger.error("_stop_command_processor: Не вдалося додати STOP_INTERNAL до черги команд (переповнена).")

                 # Чекаємо завершення потоку (з таймаутом)
                 thread_to_stop.join(timeout=2.5)
                 # Перевіряємо, чи потік завершився
                 thread_is_still_alive = thread_to_stop.is_alive()
                 if thread_is_still_alive:
                      logger.warning("Обробник команд не завершився вчасно після STOP_INTERNAL.")
                 else:
                      logger.info("Обробник команд успішно зупинено.")
            except Exception as e:
                 logger.error(f"Помилка при зупинці обробника команд: {e}")
        else:
             # Якщо потік не був активний
             logger.debug("Обробник команд не був активний.")
        # Скидаємо посилання на потік у будь-якому випадку
        self.command_processor_thread = None

    def _command_processor_loop(self):
        """Цикл, що обробляє команди з command_queue."""
        # Цей лог тепер має з'являтися при ініціалізації BotCore
        logger.info("Потік Command processor запущено...")
        # Постійний цикл, вихід через команду STOP_INTERNAL
        while True:
            try:
                # Очікуємо команду з черги
                command = self.command_queue.get(timeout=1)

                # Обробляємо команду, якщо вона є
                if command:
                    cmd_type = command.get('type')
                    # Перевірка на спеціальну команду зупинки
                    if cmd_type == 'STOP_INTERNAL':
                        logger.info("Command processor отримав STOP_INTERNAL. Завершення роботи...")
                        # Позначаємо завдання як виконане перед виходом
                        self.command_queue.task_done()
                        break # Виходимо з циклу while True

                    # Перевіряємо self.is_running для команд, що потребують активного ядра
                    process_command_flag = True # За замовчуванням обробляємо
                    # Список команд, які НЕ потребують запущеного ядра
                    commands_allowed_when_stopped = [
                        'START', 'STOP', # Команди керування
                        'GET_STATUS', 'GET_BALANCE', 'GET_POSITION', # Команди отримання стану
                        'GET_HISTORY', 'GET_STATS', 'RELOAD_SETTINGS' # Інші інформаційні/налаштувальні
                    ]
                    requires_running_core = cmd_type not in commands_allowed_when_stopped

                    # Якщо команда потребує запущеного ядра, а воно не запущено
                    if requires_running_core:
                        bot_is_currently_running = self.is_running
                        if not bot_is_currently_running:
                             logger.warning(f"Команда {cmd_type} отримана, але ядро не запущено. Ігнорується.")
                             process_command_flag = False # Не обробляємо

                    # Обробляємо команду, якщо дозволено
                    if process_command_flag:
                        # Викликаємо основний обробник команди
                        self.process_command(command)

                    # Позначаємо завдання в черзі як виконане
                    self.command_queue.task_done()

            except queue.Empty:
                # Якщо черга порожня, просто продовжуємо цикл очікування
                # Це нормальна ситуація, не логуємо нічого
                continue
            except Exception as e:
                # Логуємо будь-які інші помилки в циклі обробника
                logger.error(f"Помилка cmd processor: {e}", exc_info=True)
                # Невелика пауза перед наступною спробою
                time.sleep(1)

        # Цей лог виконається після виходу з циклу
        logger.info("Потік Command processor зупинено.")
# END BLOCK 7
# BLOCK 8: Event Queue Method
    def _put_event(self, event:dict):
        """Додає подію або сповіщення в event_queue (для UI/Telegram)."""
        # Отримуємо посилання на чергу
        event_q = self.event_queue
        # Перевіряємо, чи черга існує
        if event_q:
            try:
                # Намагаємось додати подію без блокування
                event_q.put_nowait(event)
                # Можна додати логування, якщо потрібно відстежувати події
                # event_type = event.get('type', 'NO_TYPE')
                # logger.debug(f"Подію '{event_type}' додано до event_queue.")
            except queue.Full:
                # Якщо черга переповнена
                logger.warning("Черга подій (event_queue) переповнена! Подію втрачено.")
            except Exception as e:
                # Інші можливі помилки при додаванні до черги
                logger.error(f"Помилка додавання події до event_queue: {e}")
        else:
             # Якщо черга не була передана/ініціалізована
             logger.error("Спроба додати подію, але event_queue не існує.")
# END BLOCK 8
# BLOCK 8.5: WebSocket Callback Handlers - !! Code from Response #99 !! (May violate formatting, for LOGIC TESTING ONLY)
    def _handle_kline_update(self, symbol: str, kline_data: dict, event_time: int):
        """Обробляє оновлення свічки з WebSocket."""
        # --- ДОДАНО ЛОГУВАННЯ ВХОДУ ---
        logger.debug(f"_handle_kline_update: Вхід. Символ: {symbol}")

        # Перевіряємо, чи бот працює
        bot_is_running = self.is_running
        if not bot_is_running:
            logger.debug("_handle_kline_update: Вихід (бот не запущено).")
            return

        # Перевіряємо, чи символ співпадає
        symbol_matches = symbol == self.symbol
        if not symbol_matches:
             logger.debug(f"_handle_kline_update: Вихід (символ не співпадає: {symbol} != {self.symbol}).")
             return

        try:
            # Отримуємо час початку свічки та перевіряємо на застарілі дані
            kline_start_time_ms = kline_data.get('t') # Використовуємо get для безпеки
            if kline_start_time_ms is None:
                 logger.warning("_handle_kline_update: Відсутній ключ 't' (час початку) в kline_data.")
                 return

            # Перевіряємо на застарілі дані
            last_update_ms = self.last_kline_update_time
            is_old_kline = kline_start_time_ms <= last_update_ms
            if is_old_kline:
                # logger.debug(f"_handle_kline_update: Вихід (застаріла свічка: {kline_start_time_ms} <= {last_update_ms}).")
                return

            # Перевіряємо, чи свічка закрита
            is_kline_closed = kline_data.get('x') # Використовуємо get
            if is_kline_closed is None:
                 logger.warning("_handle_kline_update: Відсутній ключ 'x' (статус закриття) в kline_data.")
                 return
            if not is_kline_closed:
                # logger.info("_handle_kline_update: Вихід (свічка ще не закрита, x=False).") # Повернули на DEBUG
                logger.debug("_handle_kline_update: Вихід (свічка ще не закрита, x=False).")
                return # Обробляємо тільки закриті свічки (x=True)

            # Якщо пройшли всі перевірки, логуємо закриття
            kline_start_dt = pd.to_datetime(kline_start_time_ms, unit='ms', utc=True)
            kline_close_price_str = kline_data.get('c', 'N/A') # Безпечний доступ до ціни закриття
            kline_dt_str = kline_start_dt.strftime('%H:%M:%S')
            # --- ЦЕЙ ЛОГ ТЕПЕР МАЄ З'ЯВИТИСЯ ---
            logger.debug(f"Kline Closed: {symbol} | T:{kline_dt_str} | C:{kline_close_price_str}")

            # Формуємо дані для DataFrame
            new_kline_dict = {
                'open': Decimal(kline_data.get('o', 'NaN')), # Додано get з дефолтом NaN
                'high': Decimal(kline_data.get('h', 'NaN')),
                'low': Decimal(kline_data.get('l', 'NaN')),
                'close': Decimal(kline_data.get('c', 'NaN')),
                'volume': Decimal(kline_data.get('v', 'NaN')),
                'num_trades': int(kline_data.get('n', 0)),
                'quote_asset_volume': Decimal(kline_data.get('q', 'NaN')),
                'taker_base_vol': Decimal(kline_data.get('V', 'NaN')),
                'taker_quote_vol': Decimal(kline_data.get('Q', 'NaN')),
            }
            new_kline_df = pd.DataFrame([new_kline_dict], index=[kline_start_dt])

            buffer_copy_for_strategy = None # Ініціалізуємо

            # Оновлюємо буфер Klines під блокуванням StateManager
            with self.state_manager.lock:
                current_buffer = self.klines_buffer
                # Видаляємо рядок з таким самим індексом, якщо він є
                index_exists = kline_start_dt in current_buffer.index
                if index_exists:
                     current_buffer = current_buffer.drop(kline_start_dt)
                # Додаємо новий рядок і сортуємо
                updated_buffer = pd.concat([current_buffer, new_kline_df]).sort_index()
                # Обмежуємо розмір буфера
                max_buffer_size = self.required_klines_length + 20
                current_buffer_size = len(updated_buffer)
                if current_buffer_size > max_buffer_size:
                    slice_start_index = current_buffer_size - max_buffer_size # Виправлено для Python 3
                    updated_buffer = updated_buffer.iloc[slice_start_index:] # Виправлено для Python 3
                # Зберігаємо оновлений буфер та час
                self.klines_buffer = updated_buffer
                self.last_kline_update_time = kline_start_time_ms
                # Створюємо копію для передачі в стратегію
                buffer_copy_for_strategy = self.klines_buffer.copy()
                current_buffer_len_log = len(buffer_copy_for_strategy)
                logger.debug(f"Kline buffer updated. Size: {current_buffer_len_log}")

            # Виклик стратегії поза блокуванням
            strategy_exists = self.strategy is not None
            has_enough_data = False
            buffer_exists_check = buffer_copy_for_strategy is not None
            if buffer_exists_check:
                 buffer_len_check = len(buffer_copy_for_strategy)
                 min_len_check = self.required_klines_length
                 has_enough_data = buffer_len_check >= min_len_check

            if strategy_exists and has_enough_data:
                logger.debug("Виклик calculate_signals стратегії...")
                try:
                    signal = self.strategy.calculate_signals(buffer_copy_for_strategy) # Передаємо копію
                    self._process_signal(signal)
                except Exception as strategy_error:
                     logger.error(f"Помилка під час виконання стратегії calculate_signals: {strategy_error}", exc_info=True)
            elif strategy_exists: # Даних недостатньо
                 buffer_len_log = 0
                 if buffer_exists_check:
                      buffer_len_log = len(buffer_copy_for_strategy)
                 req_len_log = self.required_klines_length
                 logger.debug(f"Недостатньо даних для стратегії ({buffer_len_log}/{req_len_log}).")

        except Exception as e:
            logger.error(f"Помилка обробки оновлення Kline: {e}", exc_info=True)


    def _handle_order_update(self, order_data: dict, event_time: int):
        """Обробляє оновлення статусу ордера (з User Data Stream). Використовує StateManager."""
        bot_is_running = self.is_running
        if not bot_is_running:
            return # Не обробляємо, якщо бот зупинено

        try:
            # --- Парсинг основних даних ордера ---
            binance_order_id = order_data.get('i') # Order ID (Binance) - Використовуємо get для безпеки
            client_order_id = order_data.get('c') # Client Order ID
            symbol = order_data.get('s') # Symbol
            side = order_data.get('S') # Side (BUY/SELL)
            order_type = order_data.get('o') # Order Type
            order_status = order_data.get('X') # Execution Type / Order Status
            filled_qty_cumulative_str = order_data.get('z', '0') # Cumulative filled quantity
            avg_price_str = order_data.get('ap', '0') # Average price
            last_filled_price_str = order_data.get('L', '0') # Last filled price
            commission_amount_str = order_data.get('n', '0') # Commission amount
            commission_asset = order_data.get('N') # Commission asset
            order_list_id_str = order_data.get('O', '-1') # OrderListId (для OCO)
            event_dt = pd.to_datetime(event_time, unit='ms', utc=True).to_pydatetime() # Час події

            # Перевірка наявності критичних даних
            if None in [binance_order_id, client_order_id, symbol, side, order_type, order_status]:
                 logger.error(f"Неповні дані в Order Update: {order_data}")
                 return

            # --- Конвертація в Decimal ---
            filled_qty_cumulative = Decimal(filled_qty_cumulative_str)
            last_filled_price = Decimal(last_filled_price_str)
            # Визначаємо ціну виконання (середню або останню)
            avg_price = Decimal(avg_price_str)
            if avg_price == 0: # Якщо середня ціна 0, використовуємо останню
                 avg_price = last_filled_price
            # Визначаємо комісію
            commission_amount = Decimal(0)
            if commission_amount_str:
                 commission_amount = Decimal(commission_amount_str)
            # Конвертація OrderListId в int, якщо можливо
            order_list_id = -1 # Дефолтне значення
            if order_list_id_str is not None:
                 try:
                      order_list_id_int = int(order_list_id_str)
                      order_list_id = order_list_id_int
                 except (ValueError, TypeError):
                      order_list_id = order_list_id_str # Залишаємо рядком

            # --- Логування ---
            filled_qty_str_log = f"{filled_qty_cumulative}"
            price_str_log = f"{avg_price}"
            comm_asset_str_log = commission_asset or ''
            comm_amount_str_log = f"{commission_amount}"
            log_msg_order_update = f"Order Update: {symbol}|ID:{binance_order_id}({client_order_id})|Side:{side}|Type:{order_type}|Status:{order_status}|Filled:{filled_qty_str_log}|Price:{price_str_log}|Comm:{comm_amount_str_log}{comm_asset_str_log}"
            logger.info(log_msg_order_update)

            # --- Ініціалізація змінних ---
            notification_message = None
            pnl_calculated = None
            total_commission_for_trade = None
            oco_cancellation_thread = None
            oco_placement_thread = None
            emergency_exit_thread = None

            # --- Оновлення історії угод ---
            should_log_trade = False
            if order_status in ['FILLED', 'PARTIALLY_FILLED']:
                 if filled_qty_cumulative > 0:
                      should_log_trade = True

            if should_log_trade:
                last_filled_qty_str = order_data.get('l', '0')
                last_filled_qty = Decimal(last_filled_qty_str)
                is_maker = order_data.get('m', False)
                # Отримуємо призначення ордера з активних
                internal_order_id_log = client_order_id # Починаємо з Client ID
                is_bot_client_id_log = False
                if internal_order_id_log: is_bot_client_id_log = internal_order_id_log.startswith('bot_')
                if not is_bot_client_id_log: internal_order_id_log = binance_order_id # Використовуємо Binance ID
                active_order_info_log = self.state_manager.get_active_order(internal_order_id_log)
                order_purpose_log = 'UNKNOWN'
                if active_order_info_log:
                     order_purpose_log = active_order_info_log.get('purpose', 'UNKNOWN')
                elif order_status == 'FILLED': # Якщо не в активних, але FILLED
                     order_purpose_log = 'MANUAL/UNTRACKED'

                trade_log_entry = {
                    'time': event_dt, 'symbol': symbol, 'side': side, 'type': order_type,
                    'status': order_status, 'order_id': binance_order_id,
                    'client_order_id': client_order_id, 'order_list_id': order_list_id,
                    'price': avg_price, 'last_fill_price': last_filled_price,
                    'last_fill_qty': last_filled_qty, 'total_filled_qty': filled_qty_cumulative,
                    'commission': commission_amount, 'commission_asset': commission_asset,
                    'is_maker': is_maker, 'purpose': order_purpose_log, 'pnl': None
                }
                self.state_manager.add_trade_history_entry(trade_log_entry) # StateManager збереже стан
                logger.info(f"Історію угод оновлено/додано для Order ID {binance_order_id} (через StateManager).")

            # --- Обробка статусів ордера ---
            internal_order_id = client_order_id
            is_bot_client_id_main = False
            if internal_order_id: is_bot_client_id_main = internal_order_id.startswith('bot_')
            if not is_bot_client_id_main: internal_order_id = binance_order_id

            # --- Статус: FILLED ---
            if order_status == 'FILLED':
                executed_order_details = self.state_manager.remove_active_order(internal_order_id)

                if not executed_order_details:
                    logger.warning(f"Отримано FILLED для {internal_order_id}, але його не було в active_orders. Перевірка позиції...")
                    current_pos_check = self.state_manager.get_open_position()
                    pos_check_exists = current_pos_check is not None
                    symbol_check_matches = False
                    if pos_check_exists: symbol_check_matches = current_pos_check.get('symbol') == symbol
                    if pos_check_exists and symbol_check_matches:
                         if side == 'SELL':
                              logger.warning(f"Виконано невідстежуваний SELL {internal_order_id}. Припускаємо закриття позиції.")
                              self.state_manager.set_open_position(None)
                              notification_message = f"⚠️ Позицію {symbol}, ймовірно, було закрито невідстежуваним ордером {internal_order_id}."
                         elif side == 'BUY':
                              logger.critical(f"Виконано невідстежуваний BUY {internal_order_id}, але позиція ВЖЕ ІСНУЄ!")
                              notification_message = f"⚠️ КРИТИЧНО: Невідстежуваний BUY {internal_order_id} виконано, але позиція вже існує!"
                    elif not pos_check_exists:
                         logger.info(f"Виконано невідстежуваний ордер {internal_order_id}, позиції немає.")

                else: # Якщо ордер був у нашому списку
                    order_purpose = executed_order_details.get('purpose')
                    logger.info(f"Ордер {internal_order_id} (Призначення: {order_purpose}) ПОВНІСТЮ ВИКОНАНО.")

                    # Обробка виконаного ордера на ВХІД
                    if order_purpose == 'ENTRY':
                        current_pos_entry = self.state_manager.get_open_position()
                        if current_pos_entry:
                            logger.critical(f"ENTRY ордер {internal_order_id} виконано, але позиція вже існує!")
                            notification_message = f"⚠️ КРИТИЧНО: ENTRY {symbol} виконано, але позиція ВЖЕ ІСНУВАЛА!"
                        else:
                            entry_cost = avg_price * filled_qty_cumulative
                            entry_commission = commission_amount
                            entry_commission_asset = commission_asset
                            new_position_data = {
                                'symbol': symbol, 'quantity': filled_qty_cumulative, 'entry_price': avg_price,
                                'entry_time': event_dt, 'entry_order_id': binance_order_id,
                                'entry_client_order_id': client_order_id, 'status': 'OPEN',
                                'entry_cost': entry_cost, 'entry_commission': entry_commission,
                                'entry_commission_asset': entry_commission_asset, 'oco_list_id': None
                            }
                            self.state_manager.set_open_position(new_position_data)
                            logger.info(f"Позицію ВІДКРИТО (через StateManager).")
                            qty_log_str_entry = f"{filled_qty_cumulative:.{self.qty_precision}f}"
                            price_log_str_entry = f"{avg_price:.{self.price_precision}f}"
                            notification_message = f"✅ ВХІД {symbol}: {qty_log_str_entry} @ {price_log_str_entry}"
                            oco_thread_name_entry = f"OCOPlacement-{client_order_id[:8]}"
                            oco_placement_thread = threading.Thread(target=self._place_oco_order, args=(symbol, filled_qty_cumulative, avg_price), daemon=True, name=oco_thread_name_entry)

                    # Обробка виконаного ордера на ВИХІД
                    elif order_purpose in ['TP', 'SL', 'EXIT_MANUAL', 'EXIT_SIGNAL', 'EXIT_PROTECTION']:
                        current_pos_exit = self.state_manager.get_open_position()
                        pos_exists_exit = current_pos_exit is not None
                        symbol_matches_exit = False
                        if pos_exists_exit:
                             symbol_matches_exit = current_pos_exit.get('symbol') == symbol

                        if pos_exists_exit and symbol_matches_exit:
                            entry_price = current_pos_exit['entry_price']
                            position_quantity = current_pos_exit['quantity']
                            qty_diff = abs(filled_qty_cumulative - position_quantity)
                            min_step_exit = Decimal('1') * (Decimal('10') ** -self.qty_precision)
                            qty_tolerance_exit = max(position_quantity * Decimal('0.0001'), min_step_exit)
                            qty_matches = qty_diff <= qty_tolerance_exit
                            if not qty_matches:
                                 fill_qty_str_exit = f"{filled_qty_cumulative:.{self.qty_precision}f}"
                                 pos_qty_str_exit = f"{position_quantity:.{self.qty_precision}f}"
                                 logger.warning(f"Кількість виходу ({fill_qty_str_exit}) відрізняється від позиції ({pos_qty_str_exit})! Використовуємо виконану.")
                            exit_quantity = filled_qty_cumulative # Використовуємо фактичну к-сть

                            entry_cost = current_pos_exit.get('entry_cost', entry_price * position_quantity)
                            exit_value = avg_price * exit_quantity
                            entry_comm = current_pos_exit.get('entry_commission', Decimal(0))
                            entry_comm_asset = current_pos_exit.get('entry_commission_asset', self.quote_asset)
                            exit_comm = commission_amount
                            exit_comm_asset = commission_asset
                            total_comm_in_quote = Decimal(0)
                            # Обчислення комісії входу в quote валюті
                            if entry_comm > 0:
                                if entry_comm_asset == self.quote_asset:
                                     total_comm_in_quote = total_comm_in_quote + entry_comm
                                elif entry_comm_asset == self.base_asset:
                                     comm_entry_in_quote = entry_comm * entry_price
                                     total_comm_in_quote = total_comm_in_quote + comm_entry_in_quote
                                else:
                                     logger.warning(f"Комісія входу в невідомому активі {entry_comm_asset}")
                            # Обчислення комісії виходу в quote валюті
                            if exit_comm > 0:
                                if exit_comm_asset == self.quote_asset:
                                     total_comm_in_quote = total_comm_in_quote + exit_comm
                                elif exit_comm_asset == self.base_asset:
                                     comm_exit_in_quote = exit_comm * avg_price
                                     total_comm_in_quote = total_comm_in_quote + comm_exit_in_quote
                                else:
                                     logger.warning(f"Комісія виходу в невідомому активі {exit_comm_asset}")

                            # Розрахунок PnL
                            pnl = exit_value - entry_cost - total_comm_in_quote
                            pnl_calculated = pnl # Для статистики
                            total_commission_for_trade = total_comm_in_quote # Для статистики

                            # Логування
                            pnl_log_str_exit = f"{pnl:.{self.price_precision}f}"
                            comm_log_str_exit = f"{total_comm_in_quote:.{self.price_precision}f}"
                            logger.info(f"Позицію {symbol} закрито ({order_purpose}). PnL={pnl_log_str_exit} {self.quote_asset} (Заг.Ком.:{comm_log_str_exit} {self.quote_asset})")
                            # Оновлення PnL в історії
                            self.state_manager.update_last_trade_history_pnl(binance_order_id, pnl)
                            # Закриття позиції
                            self.state_manager.set_open_position(None)

                            # Сповіщення
                            qty_log_str_exit = f"{exit_quantity:.{self.qty_precision}f}"
                            price_log_str_exit = f"{avg_price:.{self.price_precision}f}"
                            notification_message = f"🛑 ВИХІД {symbol}({order_purpose}): {qty_log_str_exit} @ {price_log_str_exit}. PnL: {pnl_log_str_exit} {self.quote_asset}"

                            # Скасування OCO, якщо потрібно
                            is_oco_part_filled = False
                            if order_purpose in ['TP', 'SL']:
                                if order_list_id != -1:
                                     if order_list_id is not None:
                                          is_oco_part_filled = True
                            if is_oco_part_filled:
                                oco_cancel_thread_name = f"OCOCancellation-{binance_order_id}"
                                oco_list_id_arg = str(order_list_id)
                                oco_cancellation_thread = threading.Thread(
                                    target=self._cancel_other_oco_part,
                                    args=(symbol, oco_list_id_arg, binance_order_id),
                                    daemon=True, name=oco_cancel_thread_name
                                )
                        else:
                            logger.warning(f"Ордер виходу {internal_order_id} виконано, але позиції {symbol} немає або символ не співпадає!")
                            notification_message = f"⚠️ Ордер виходу {symbol} {internal_order_id} виконано, але позиції не було!"

            # --- Обробка статусів НЕВИКОНАННЯ ---
            elif order_status in ['CANCELED', 'REJECTED', 'EXPIRED']:
                canceled_order_details = self.state_manager.remove_active_order(internal_order_id)
                if canceled_order_details:
                    order_purpose_cancel = canceled_order_details.get('purpose')
                    logger.warning(f"Ордер {internal_order_id}({order_purpose_cancel}) НЕ ВИКОНАНО. Статус: {order_status}.")
                    notification_message = f"⚠️ Ордер {symbol} {side} {order_type}({order_purpose_cancel}) НЕ ВИКОНАНО: {order_status}"
                    current_pos_check_cancel = self.state_manager.get_open_position()
                    is_protection_order_cancel = order_purpose_cancel in ['TP', 'SL']
                    position_exists_for_cancel = False
                    if current_pos_check_cancel:
                         position_exists_for_cancel = current_pos_check_cancel.get('symbol') == symbol

                    if is_protection_order_cancel and position_exists_for_cancel:
                        logger.critical(f"Захисний ордер {internal_order_id} ({order_purpose_cancel}) не спрацював ({order_status})! Позиція {symbol} незахищена!")
                        notification_message = notification_message + "\n🚨 ІНІЦІЮЄМО АВАРІЙНИЙ ВИХІД!"
                        exit_thread_name_cancel = f"EmergencyExit-{internal_order_id}"
                        emergency_exit_thread = threading.Thread(target=self._initiate_exit, args=("Protection Order Failed",), daemon=True, name=exit_thread_name_cancel)
                    elif order_purpose_cancel == 'ENTRY':
                        logger.warning(f"Ордер на вхід {internal_order_id} не виконано ({order_status}).")
                else:
                    logger.info(f"Отримано статус '{order_status}' для ордера {internal_order_id}, якого не було в active_orders.")

            # --- Обробка статусу PARTIALLY_FILLED ---
            elif order_status == 'PARTIALLY_FILLED':
                filled_qty_log_str = f"{filled_qty_cumulative}"
                logger.info(f"Ордер {internal_order_id} ЧАСТКОВО ВИКОНАНО ({filled_qty_log_str}). Очікуємо FILLED.")
                current_active_order_data = self.state_manager.get_active_order(internal_order_id)
                if current_active_order_data:
                    updated_order_data = current_active_order_data.copy()
                    update_dict = {
                        'status': order_status,
                        'filled_qty': filled_qty_cumulative, # Додаємо поле
                        'avg_price': avg_price # Оновлюємо середню ціну
                    }
                    updated_order_data.update(update_dict)
                    self.state_manager.add_or_update_active_order(internal_order_id, updated_order_data)
                else:
                     logger.warning(f"Отримано PARTIALLY_FILLED для {internal_order_id}, але його не було в active_orders.")

            # --- Обробка статусу NEW ---
            elif order_status == 'NEW':
                 current_active_order_data = self.state_manager.get_active_order(internal_order_id)
                 if not current_active_order_data:
                     logger.warning(f"Отримано статус NEW для невідомого ордера {internal_order_id}. Додаємо в active_orders.")
                     new_order_details = {
                         'symbol': symbol, 'order_id_binance': binance_order_id, 'client_order_id': client_order_id,
                         'side': side, 'type': order_type, 'status': 'NEW', 'purpose': 'UNKNOWN_NEW'
                         # Додати інші поля з order_data за потреби
                     }
                     self.state_manager.add_or_update_active_order(internal_order_id, new_order_details)
                 else:
                      has_binance_id = current_active_order_data.get('order_id_binance') is not None
                      if not has_binance_id:
                           logger.debug(f"Оновлення Binance Order ID для активного ордера {internal_order_id}.")
                           updated_order_data_id = current_active_order_data.copy()
                           updated_order_data_id['order_id_binance'] = binance_order_id
                           self.state_manager.add_or_update_active_order(internal_order_id, updated_order_data_id)

            # Оновлення статистики сесії, якщо були розрахунки
            if pnl_calculated is not None:
                 if total_commission_for_trade is not None:
                      self._update_session_stats(pnl_calculated, total_commission_for_trade)

            # --- Запуск фонових потоків ---
            if notification_message:
                 notify_payload = {'message': notification_message}
                 self._put_event({'type':'NOTIFICATION', 'payload': notify_payload})
            if oco_placement_thread:
                 logger.debug(f"Запуск потоку OCO Placement: {oco_placement_thread.name}")
                 oco_placement_thread.start()
            if oco_cancellation_thread:
                 logger.debug(f"Запуск потоку OCO Cancellation: {oco_cancellation_thread.name}")
                 oco_cancellation_thread.start()
            if emergency_exit_thread:
                 logger.debug(f"Запуск потоку Emergency Exit: {emergency_exit_thread.name}")
                 emergency_exit_thread.start()

        except KeyError as e:
            logger.error(f"Відсутній ключ в даних оновлення ордера: {e}. Дані: {order_data}", exc_info=True)
            error_payload_key = {'source':'BotCore._handle_order_update', 'message':f"Помилка даних ордера (KeyError): {e}"}
            self._put_event({'type':'ERROR', 'payload': error_payload_key})
        except Exception as e:
            logger.error(f"Загальна помилка обробки оновлення ордера: {e}", exc_info=True)
            error_payload_general = {'source':'BotCore._handle_order_update', 'message':f"Помилка обробки ордера: {e}"}
            self._put_event({'type':'ERROR', 'payload': error_payload_general})


    def _handle_balance_update(self, balance_data: dict, event_time: int):
        """Обробляє оновлення балансу (з User Data Stream). Використовує StateManager."""
        bot_is_running = self.is_running
        if not bot_is_running:
            return # Не обробляємо, якщо бот зупинено

        event_type = balance_data.get('e')
        logger.debug(f"Balance Update: Type={event_type}")
        needs_full_fetch = False # Прапорець необхідності повного оновлення

        try:
            # Обробка події outboundAccountPosition (повний знімок балансу)
            if event_type == 'outboundAccountPosition':
                new_balances = {}
                balances_in_event = balance_data.get('B', []) # Список балансів
                for item in balances_in_event:
                    asset = item.get('a') # Назва активу
                    free_str = item.get('f', '0') # Вільний баланс (рядок)
                    locked_str = item.get('l', '0') # Заблокований баланс (рядок)
                    if asset: # Перевіряємо, чи є назва активу
                        try:
                             free_bal = Decimal(free_str)
                             locked_bal = Decimal(locked_str)
                             # Додаємо тільки якщо є позитивний баланс
                             is_positive = free_bal > 0 or locked_bal > 0
                             if is_positive:
                                  balance_entry = {'free': free_bal, 'locked': locked_bal}
                                  new_balances[asset] = balance_entry
                        except InvalidOperation:
                             logger.warning(f"Некоректне значення балансу для {asset} в outboundAccountPosition: free='{free_str}', locked='{locked_str}'")
                # Встановлюємо новий стан балансів через StateManager
                self.state_manager.set_balances(new_balances)
                logger.info("Баланси повністю оновлено (outboundAccountPosition через StateManager).")

            # Обробка події balanceUpdate (зміна одного активу)
            elif event_type == 'balanceUpdate':
                 asset = balance_data.get('a') # Назва активу
                 delta_str = balance_data.get('d') # Зміна балансу (рядок)
                 # T_time = balance_data.get('T') # Час оновлення (можна використовувати)
                 if asset and delta_str: # Перевіряємо наявність даних
                      try:
                           delta = Decimal(delta_str)
                           # Оновлюємо баланс через StateManager, він поверне True, якщо потрібен повний запит
                           needs_full_fetch = self.state_manager.update_balance_delta(asset, delta)
                      except InvalidOperation:
                           logger.error(f"Некоректне значення дельти '{delta_str}' для активу {asset}.")
                           needs_full_fetch = True # Потрібно перевірити стан
                 else:
                      logger.warning(f"Неповні дані в balanceUpdate: {balance_data}")
                      needs_full_fetch = True # Потрібно перевірити стан

        except Exception as e:
            # Обробка будь-яких помилок
            logger.error(f"Помилка обробки оновлення балансу: {e}", exc_info=True)
            needs_full_fetch = True # Запитуємо повний баланс при будь-якій помилці

        # Якщо за результатами обробки потрібен повний запит балансу
        if needs_full_fetch:
             logger.warning("Потрібен повний запит балансу через помилку або оновлення невідстежуваного активу.")
             # Запускаємо фонове завантаження балансу
             fetch_thread_name = "FetchFullBalanceOnError"
             fetch_thread = threading.Thread(target=self._fetch_initial_balance, daemon=True, name=fetch_thread_name)
             fetch_thread.start()
# END BLOCK 8.5
# BLOCK 9: Strategy Signal Processing Method
    def _process_signal(self, signal: str | None):
        """Обробляє сигнал ('BUY', 'CLOSE', 'SELL', None), отриманий від стратегії."""
        # Перевірка 1: Чи працює бот?
        bot_is_running = self.is_running
        if not bot_is_running:
            # Не обробляємо сигнали, якщо бот зупинено
            return

        # Перевірка 2: Чи є сигнал валідним?
        signal_is_valid = signal is not None
        if not signal_is_valid:
            # Немає сигналу для обробки
            return

        # Логуємо отриманий сигнал
        logger.info(f"Отримано сигнал '{signal}' від стратегії для {self.symbol}")

        # Отримуємо поточний стан (позиція, активні ордери) через StateManager
        current_position = self.state_manager.get_open_position()
        active_orders_dict = self.state_manager.get_all_active_orders()

        # Визначаємо, чи є активні ордери на вхід або вихід
        pending_entry = False
        pending_exit = False
        if active_orders_dict: # Перевіряємо, чи словник не порожній
            # Ітеруємо по значеннях словника (деталі ордерів)
            for order_details in active_orders_dict.values():
                order_status = order_details.get('status')
                # Список статусів, які вважаються НЕзавершеними
                active_statuses = ['NEW', 'PARTIALLY_FILLED', 'PENDING_CANCEL']
                is_active_order = order_status in active_statuses

                # Якщо ордер активний, перевіряємо його призначення
                if is_active_order:
                    order_purpose = order_details.get('purpose')
                    is_entry_purpose = (order_purpose == 'ENTRY')
                    is_exit_purpose = order_purpose in ['TP','SL','EXIT_MANUAL','EXIT_SIGNAL','EXIT_PROTECTION']

                    # Встановлюємо відповідні прапорці
                    if is_entry_purpose:
                        pending_entry = True
                    elif is_exit_purpose:
                        pending_exit = True

                # Оптимізація: якщо знайшли обидва типи, далі можна не шукати
                if pending_entry and pending_exit:
                    break

        # --- Логіка обробки конкретних сигналів ---
        # --- Сигнал BUY ---
        if signal == 'BUY':
            position_exists = current_position is not None
            if position_exists:
                logger.info("Сигнал BUY, але позиція вже відкрита. Ігноруємо.")
            elif pending_entry:
                logger.info("Сигнал BUY, але вже є активний ордер на вхід. Ігноруємо.")
            else:
                # Немає позиції і немає активного ордера на вхід -> ініціюємо вхід
                logger.info("Сигнал BUY: Немає позиції та активних ордерів на вхід. Ініціюємо вхід...")
                self._initiate_entry()

        # --- Сигнал CLOSE ---
        elif signal == 'CLOSE':
            position_exists = current_position is not None
            if not position_exists:
                logger.info("Сигнал CLOSE, але позиція не відкрита. Ігноруємо.")
            elif pending_exit:
                logger.info("Сигнал CLOSE, але вже є активний ордер на вихід. Ігноруємо.")
            else:
                # Є позиція і немає активного ордера на вихід -> ініціюємо вихід
                logger.info("Сигнал CLOSE: Є позиція та немає активних ордерів на вихід. Ініціюємо вихід...")
                # Вказуємо причину виходу
                exit_reason = "Strategy Signal"
                self._initiate_exit(reason=exit_reason)

        # --- Сигнал SELL (для шортів, поки ігнорується) ---
        elif signal == 'SELL':
            logger.warning("Сигнал SELL (для шорт-позиції) поки що ігнорується.")

        # --- Невідомий сигнал ---
        else:
            logger.warning(f"Отримано невідомий сигнал від стратегії: '{signal}'")
# END BLOCK 9
# BLOCK 10: Trading Initiation Methods
    def _initiate_entry(self):
        """Ініціює процес відкриття позиції."""
        logger.info(f"Ініціація входу {self.symbol}...")
        # Розраховуємо розмір позиції
        # Цей метод (_calculate_position_size) використовує StateManager для отримання балансу
        quantity = self._calculate_position_size()

        # Перевіряємо, чи вдалося розрахувати кількість
        quantity_is_valid = False
        if quantity is not None:
             if quantity > 0:
                  quantity_is_valid = True

        # Якщо кількість валідна, запускаємо розміщення ордера
        if quantity_is_valid:
            # Запускаємо розміщення в окремому потоці, щоб не блокувати
            entry_thread_name = f"EntryPlacement-{self.symbol}"
            entry_thread = threading.Thread(
                target=self._place_entry_order,
                args=(quantity,),
                daemon=True,
                name=entry_thread_name
            )
            entry_thread.start()
        else:
            # Якщо кількість не розраховано або нульова
            logger.warning(f"Не вдалося розрахувати коректний розмір позиції для входу.")
            # Надсилаємо подію-попередження
            warning_payload = {
                'source': 'BotCore._initiate_entry',
                'message': 'Не вдалося розрахувати розмір позиції.'
            }
            self._put_event({'type':'WARNING', 'payload': warning_payload})

    def _initiate_exit(self, reason: str):
        """Ініціює процес закриття позиції."""
        exit_reason_log = str(reason) # Для логування
        logger.info(f"Ініціація виходу {self.symbol} (Причина: {exit_reason_log})...")
        # Ініціалізуємо змінні
        sym_exit = None
        qty_close = None
        oco_list_id_to_cancel = None
        proceed_with_exit = False # Прапорець, чи потрібно продовжувати

        # Отримуємо поточну позицію через StateManager
        pos = self.state_manager.get_open_position()

        # Перевіряємо, чи є що закривати
        if pos:
             # Якщо позиція існує, отримуємо її дані
             sym_exit = pos.get('symbol')
             qty_close = pos.get('quantity')
             oco_list_id_to_cancel = pos.get('oco_list_id')

             # Перевіряємо, чи символ позиції збігається з символом бота
             # (додаткова перевірка, хоча малоймовірно)
             if sym_exit != self.symbol:
                  logger.error(f"Спроба закрити позицію для {sym_exit}, але активний символ бота {self.symbol}!")
                  # Не продовжуємо вихід для неправильного символу
                  proceed_with_exit = False
             # Перевіряємо, чи є кількість для закриття
             elif qty_close is None or qty_close <= 0:
                   logger.error(f"Некоректна кількість ({qty_close}) у відкритій позиції {sym_exit}.")
                   proceed_with_exit = False
             else:
                   # Якщо все гаразд, встановлюємо прапорець для продовження
                   proceed_with_exit = True
                   # Очищаємо OCO ID в позиції через StateManager, ЯКЩО він був
                   if oco_list_id_to_cancel:
                        self.state_manager.update_open_position_field('oco_list_id', None)
        else:
             # Якщо позиції немає
             logger.warning("Спроба ініціювати вихід, але відкритої позиції немає.")
             info_payload_exit = {
                 'source': 'BotCore._initiate_exit',
                 'message': 'Команда виходу отримана, але позиція вже закрита.'
             }
             self._put_event({'type':'INFO', 'payload': info_payload_exit})
             # Залишаємо proceed_with_exit = False

        # Якщо немає чого закривати або символ не той, виходимо
        if not proceed_with_exit:
             return

        # --- Скасування активних ордерів ПЕРЕД розміщенням ордера на вихід ---
        logger.info(f"Скасування ВСІХ активних ордерів для {sym_exit} перед виходом...")
        try:
            # Викликаємо метод API для скасування всіх ордерів
            cancel_res = self.binance_api.cancel_all_orders(symbol=sym_exit)
            # Логуємо результат (може бути список скасованих ордерів або помилка)
            logger.info(f"Результат скасування ордерів для {sym_exit}: {cancel_res}")
            # Очищаємо список активних ордерів для цього символу через StateManager
            self.state_manager.clear_active_orders_for_symbol(sym_exit)

        except BinanceAPIError as e:
            # Перевіряємо код помилки (-2011: Order does not exist / No open orders)
            is_no_orders_error = (e.code == -2011)
            if is_no_orders_error:
                # Це нормально, якщо відкритих ордерів не було
                logger.info(f"Не знайдено активних ордерів для скасування для {sym_exit} (Code -2011).")
            else:
                # Якщо інша помилка API
                logger.error(f"API помилка при скасуванні ордерів для {sym_exit}: {e}")
                logger.critical(f"НЕ ВДАЛОСЯ СКАСУВАТИ ОРДЕРИ для {sym_exit}! Спроба виходу може бути НЕБЕЗПЕЧНОЮ!")
                critical_payload_cancel = {
                    'source': 'BotCore._initiate_exit',
                    'message': f"НЕБЕЗПЕКА! Не вдалося скасувати ордери для {sym_exit}. Аварійний вихід не виконано!"
                }
                self._put_event({'type':'CRITICAL_ERROR', 'payload': critical_payload_cancel})
                return # Не продовжуємо з виходом у разі критичної помилки скасування

        except Exception as e:
            # Якщо будь-яка інша помилка під час скасування
            logger.error(f"Загальна помилка при скасуванні ордерів для {sym_exit}: {e}", exc_info=True)
            error_payload_cancel = {
                'source': 'BotCore._initiate_exit',
                'message': f"Помилка скасування ордерів для {sym_exit}."
            }
            self._put_event({'type':'ERROR', 'payload': error_payload_cancel})
            return # Не продовжуємо з виходом

        # --- Розміщення ордера на вихід (MARKET SELL) ---
        # Визначаємо призначення ('purpose') ордера на основі причини виходу
        purpose = 'EXIT_MANUAL' # Значення за замовчуванням для ручного виклику
        if reason == 'Strategy Signal':
            purpose = 'EXIT_SIGNAL'
        elif reason == 'Protection Order Failed':
            purpose = 'EXIT_PROTECTION'
        # Можна додати інші причини за потреби
        # elif reason == 'Some Other Reason':
        #    purpose = 'EXIT_OTHER'

        # Запускаємо розміщення ордера на вихід в окремому потоці
        exit_thread_name = f"ExitPlacement-{sym_exit}"
        logger.info(f"Запуск потоку для розміщення ордера на вихід для {sym_exit} (К-сть: {qty_close}, Призначення: {purpose})")
        exit_thread = threading.Thread(
            target=self._place_exit_order,
            args=(qty_close, purpose), # Передаємо кількість та призначення
            daemon=True,
            name=exit_thread_name
        )
        exit_thread.start()
# END BLOCK 10
# BLOCK 11: Order Helper Methods
    def _adjust_quantity(self, quantity: Decimal) -> Decimal | None:
        """Коригує кількість відповідно до фільтрів LOT_SIZE."""
        # Отримуємо інформацію про символ
        symbol_info_data = self.symbol_info
        if not symbol_info_data:
            logger.error("Немає інфо символу для коригування кількості!")
            return None

        # Знаходимо фільтр LOT_SIZE
        lot_size_filter = None
        filters = symbol_info_data.get('filters', [])
        for f in filters:
             filter_type = f.get('filterType')
             if filter_type == 'LOT_SIZE':
                  lot_size_filter = f
                  break

        # Якщо фільтра немає, просто нормалізуємо і повертаємо
        if not lot_size_filter:
            logger.warning(f"Фільтр LOT_SIZE не знайдено для {self.symbol}. Пропускаємо коригування кількості.")
            normalized_qty_no_filter = quantity.normalize()
            return normalized_qty_no_filter

        try:
            # Отримуємо параметри фільтра як рядки
            min_q_str = lot_size_filter.get('minQty')
            step_str = lot_size_filter.get('stepSize')
            max_q_str = lot_size_filter.get('maxQty')

            # Перевіряємо, чи отримали всі параметри
            if None in [min_q_str, step_str, max_q_str]:
                 logger.error(f"Неповні дані у фільтрі LOT_SIZE: {lot_size_filter}")
                 return None

            # Конвертуємо в Decimal
            min_q = Decimal(min_q_str)
            step = Decimal(step_str)
            max_q = Decimal(max_q_str)

            # Починаємо з переданої кількості
            current_quantity = quantity

            # 1. Перевірка на maxQty
            if current_quantity > max_q:
                 max_q_log_str = f"{max_q:.8f}" # Форматуємо для логу
                 current_qty_log_str = f"{current_quantity:.8f}"
                 logger.warning(f"Qty {current_qty_log_str} > maxQty {max_q_log_str}. Зменшено до maxQty.")
                 current_quantity = max_q # Обмежуємо зверху

            # 2. Перевірка на minQty (початкова)
            if current_quantity < min_q:
                min_q_str_log = f"{min_q:.8f}"
                current_qty_str_log = f"{current_quantity:.8f}"
                logger.warning(f"Розрахована Qty {current_qty_str_log} < minQty {min_q_str_log}. Неможливо розмістити ордер.")
                return None # Не проходимо мінімальний лот

            # 3. Застосування stepSize (кроку лота)
            adjusted_qty = current_quantity # Починаємо з поточного (можливо, вже обмеженого maxQty)
            if step > 0:
                # Використовуємо квантування Decimal для округлення донизу до кратності кроку
                # (quantity / step) -> скільки кроків вміщається
                # .quantize(Decimal('1'), rounding=ROUND_DOWN) -> округлюємо до цілого вниз
                # * step -> множимо на крок, щоб отримати фінальне значення
                quantized_value = (current_quantity / step).quantize(Decimal('1'), rounding=ROUND_DOWN)
                adjusted_qty = quantized_value * step
                step_str_log = f"{step:.8f}"
                adj_qty_str_log = f"{adjusted_qty:.8f}"
                logger.debug(f"Qty після застосування stepSize {step_str_log}: {adj_qty_str_log}")

            # 4. Фінальна перевірка на minQty (після застосування stepSize)
            if adjusted_qty < min_q:
                min_q_str_log = f"{min_q:.8f}"
                adj_qty_str_log = f"{adjusted_qty:.8f}"
                logger.warning(f"Qty {adj_qty_str_log} після stepSize стала < minQty {min_q_str_log}. Неможливо розмістити ордер.")
                return None # Не проходимо мінімальний лот після округлення

            # Повертаємо фінальну скориговану та нормалізовану кількість
            final_qty = adjusted_qty.normalize()
            return final_qty

        except (KeyError, ValueError, InvalidOperation) as e:
             # Якщо виникла помилка при обробці параметрів фільтра
             logger.error(f"Помилка обробки фільтру LOT_SIZE: {e}. Фільтр: {lot_size_filter}")
             return None # Повертаємо None у разі помилки

    def _check_min_notional(self, quantity: Decimal, price: Decimal) -> bool:
        """Перевіряє, чи проходить ордер фільтр MIN_NOTIONAL."""
        # Перевіряємо наявність інфо символу
        symbol_info_data = self.symbol_info
        if not symbol_info_data:
            logger.warning("Немає інфо символу для перевірки MIN_NOTIONAL!")
            return True # Пропускаємо перевірку, якщо немає інфо

        # Знаходимо фільтр MIN_NOTIONAL
        notional_filter = None
        filters = symbol_info_data.get('filters', [])
        for f in filters:
             filter_type = f.get('filterType')
             if filter_type == 'MIN_NOTIONAL':
                  notional_filter = f
                  break

        # Якщо фільтра немає, перевірку пройдено
        if not notional_filter:
            logger.debug(f"Фільтр MIN_NOTIONAL не знайдено для {self.symbol}. Пропускаємо перевірку.")
            return True

        try:
            # Отримуємо мінімальне значення як рядок
            min_n_str = notional_filter.get('minNotional')
            if min_n_str is None:
                 logger.warning(f"Не знайдено 'minNotional' у фільтрі: {notional_filter}")
                 return True # Пропускаємо, якщо немає значення

            # Конвертуємо в Decimal
            min_n = Decimal(min_n_str)

            # Перевіряємо валідність вхідних даних
            price_ok = False
            if price is not None:
                 if price > 0:
                      price_ok = True
            quantity_ok = False
            if quantity is not None:
                 if quantity > 0:
                      quantity_ok = True

            if not price_ok or not quantity_ok:
                logger.warning(f"Некоректні дані для перевірки MIN_NOTIONAL: Qty={quantity}, Price={price}")
                return False # Не пройшло

            # Розраховуємо вартість ордера
            notional_value = quantity * price
            # Порівнюємо з мінімальною
            passes_check = notional_value >= min_n

            # Форматуємо для логів
            price_precision_str_format = f".{self.price_precision}f"
            # Використовуємо try-except для форматування, бо Decimal може бути NaN
            try:
                 notional_value_log = f"{notional_value:{price_precision_str_format}}"
                 min_n_log = f"{min_n:{price_precision_str_format}}"
            except ValueError: # Якщо значення не форматується (напр. NaN)
                 notional_value_log = str(notional_value)
                 min_n_log = str(min_n)

            # Логуємо результат перевірки
            if passes_check:
                 logger.debug(f"Перевірка MIN_NOTIONAL пройдена: {notional_value_log} >= {min_n_log}")
                 return True # Пройшло
            else:
                 logger.warning(f"Ордер не проходить MIN_NOTIONAL: Вартість={notional_value_log} < Мін={min_n_log}")
                 return False # Не пройшло

        except (KeyError, ValueError, InvalidOperation) as e:
            # Якщо помилка при обробці фільтра
            logger.error(f"Помилка обробки фільтру MIN_NOTIONAL: {e}. Фільтр: {notional_filter}")
            return False # Вважаємо, що не пройшло, якщо помилка

    def _calculate_position_size(self) -> Decimal | None:
        """Розраховує розмір позиції на основі доступного балансу та налаштувань."""
        logger.debug("Розрахунок розміру позиції...")
        try:
            # Отримуємо поточні баланси через StateManager
            balances = self.state_manager.get_balances()
            # Отримуємо дані для котирувального активу
            quote_bal_data = balances.get(self.quote_asset)
            quote_bal = Decimal(0) # Баланс за замовчуванням
            # Якщо дані є, отримуємо вільний баланс
            if quote_bal_data:
                 free_balance_value = quote_bal_data.get('free', Decimal(0))
                 # Переконуємось, що це Decimal
                 if isinstance(free_balance_value, Decimal):
                      quote_bal = free_balance_value
                 else: # Спроба конвертації, якщо тип інший
                      try:
                           quote_bal = Decimal(str(free_balance_value))
                      except (InvalidOperation, TypeError):
                            logger.warning(f"Некоректний тип вільного балансу для {self.quote_asset}: {type(free_balance_value)}")
                            quote_bal = Decimal(0)


            # Перевіряємо, чи є достатньо коштів
            if quote_bal <= 0:
                quote_bal_str = f"{quote_bal:.8f}" # Форматуємо для логу
                logger.warning(f"Недостатньо коштів для відкриття позиції (Баланс {self.quote_asset}: {quote_bal_str}).")
                # Надсилаємо подію-попередження
                warning_payload_funds = {'source':'BotCore._calculate_position_size', 'message':f'Недостатньо {self.quote_asset} для відкриття позиції.'}
                self._put_event({'type':'WARNING', 'payload': warning_payload_funds})
                return None # Повертаємо None, якщо коштів немає

            # Розраховуємо суму для інвестиції у валюті котирування
            position_size_fraction = self.position_size_percent # Вже дріб (напр., 0.1 для 10%)
            quote_amount_to_invest = quote_bal * position_size_fraction
            # Обмежуємо суму доступним балансом
            if quote_amount_to_invest > quote_bal:
                quote_amount_to_invest = quote_bal
                logger.debug(f"Розмір інвестиції обмежено доступним балансом {self.quote_asset}.")

            # Логуємо проміжні значення
            quote_bal_str = f"{quote_bal:.8f}"
            invest_amt_str = f"{quote_amount_to_invest:.8f}"
            logger.debug(f"Доступно: {quote_bal_str} {self.quote_asset}. Розрахункова сума інвестиції: ~{invest_amt_str} {self.quote_asset}")

            # Отримуємо останню ціну (з буфера або API)
            last_price = None
            # Використовуємо lock StateManager для доступу до буфера klines
            with self.state_manager.lock:
                # Перевіряємо, чи буфер не порожній і має колонку 'close'
                buffer_exists = not self.klines_buffer.empty
                close_col_exists = 'close' in self.klines_buffer.columns
                if buffer_exists and close_col_exists:
                    # Отримуємо останнє значення ціни закриття
                    last_price_candidate = self.klines_buffer['close'].iloc[-1]
                    # Перевіряємо, чи це валідний Decimal і не NaN
                    value_is_decimal = isinstance(last_price_candidate, Decimal)
                    value_is_not_nan = pd.notna(last_price_candidate)
                    if value_is_decimal and value_is_not_nan:
                         last_price = last_price_candidate # Використовуємо ціну з буфера
                    else:
                         logger.warning("Остання ціна в буфері Klines є NaN або некоректного типу.")

            # Якщо ціну не знайдено в буфері, запитуємо з API
            if last_price is None or last_price <= 0:
                logger.warning("Немає валідної ціни в буфері Klines, запит поточної ціни з API...")
                current_price_api = None
                # Перевіряємо наявність API перед викликом
                if self.binance_api:
                     current_price_api = self.binance_api.get_current_price(self.symbol)

                # Перевіряємо результат API
                price_api_ok = current_price_api is not None and current_price_api > 0
                if not price_api_ok:
                     logger.error("Не вдалося отримати поточну ціну з API для розрахунку розміру позиції.")
                     error_payload_price = {'source':'BotCore._calculate_position_size', 'message':'Не вдалося отримати ціну для розрахунку розміру.'}
                     self._put_event({'type':'ERROR', 'payload': error_payload_price})
                     return None # Повертаємо None, якщо ціну не отримано
                # Використовуємо ціну з API
                last_price = current_price_api
                logger.info(f"Використовується поточна ціна з API: {last_price}")

            # Продовжуємо розрахунок з отриманою ціною
            current_price = Decimal(str(last_price)) # Переконуємось, що це Decimal
            # Розраховуємо "грубу" кількість базового активу
            quantity_gross = quote_amount_to_invest / current_price
            qty_gross_str = f"{quantity_gross:.8f}"
            logger.debug(f"Початкова розрахункова к-сть: {qty_gross_str}")

            # Коригуємо кількість за фільтрами LOT_SIZE
            adjusted_quantity = self._adjust_quantity(quantity_gross)
            # Перевіряємо результат коригування
            quantity_adjusted_ok = adjusted_quantity is not None and adjusted_quantity > 0

            if not quantity_adjusted_ok:
                qty_gross_str_err = f"{quantity_gross:.8f}"
                logger.error(f"Не вдалося скоригувати кількість ({qty_gross_str_err}) відповідно до фільтрів LOT_SIZE.")
                error_payload_lotsize = {'source':'BotCore._calculate_position_size', 'message':'Помилка коригування кількості (LOT_SIZE).'}
                self._put_event({'type':'ERROR', 'payload': error_payload_lotsize})
                return None

            # Перевіряємо MIN_NOTIONAL з фінальною скоригованою кількістю
            passes_min_notional = self._check_min_notional(adjusted_quantity, current_price)
            if not passes_min_notional:
                # Логуємо помилку і повертаємо None
                adj_qty_str = f"{adjusted_quantity:.8f}"
                curr_price_str_fmt = f".{self.price_precision}f"
                curr_price_str = f"{current_price:{curr_price_str_fmt}}"
                logger.error(f"Розрахована позиція ({adj_qty_str} @ {curr_price_str}) не проходить фільтр MIN_NOTIONAL.")
                error_payload_notional = {'source':'BotCore._calculate_position_size', 'message':'Позиція не проходить MIN_NOTIONAL.'}
                self._put_event({'type':'ERROR', 'payload': error_payload_notional})
                return None

            # Логуємо фінальний результат
            qty_precision_fmt = f".{self.qty_precision}f"
            final_qty_str = f"{adjusted_quantity:{qty_precision_fmt}}"
            logger.info(f"Фінальний розрахований розмір позиції: {final_qty_str} {self.base_asset}")
            # Повертаємо розраховану та скориговану кількість
            return adjusted_quantity

        except Exception as e:
            # Логуємо будь-яку іншу помилку
            logger.error(f"Загальна помилка розрахунку розміру позиції: {e}", exc_info=True)
            error_payload_general = {'source':'BotCore._calculate_position_size', 'message':'Загальна помилка розрахунку розміру.'}
            self._put_event({'type':'ERROR', 'payload': error_payload_general})
            return None

    def _generate_client_order_id(self, prefix='bot') -> str:
        """Генерує унікальний Client Order ID."""
        # Отримуємо поточний час в мілісекундах
        now_ms = int(time.time() * 1000)
        # Отримуємо ідентифікатор поточного потоку
        thread_id = threading.get_ident()
        # Формуємо базовий ID
        raw_id = f"{prefix}_{now_ms}_{thread_id}"
        # Обрізаємо ID до максимальної довжини
        # Важливо перевірити документацію Binance щодо поточних обмежень
        max_len = 36 # Припустима довжина
        final_id = raw_id[-max_len:]
        return final_id
# END BLOCK 11
# BLOCK 12: Order Placement Methods
    def _place_entry_order(self, quantity: Decimal):
        """Розміщує ордер на вхід (MARKET BUY). Виконується в окремому потоці."""
        # Генеруємо унікальний ID
        client_order_id = self._generate_client_order_id('entry')
        # Логуємо спробу
        logger.info(f"Спроба розміщення ордера ENTRY MARKET BUY {quantity} {self.symbol}... [CID:{client_order_id}]")
        try:
            # Форматуємо кількість як рядок з потрібною точністю
            qty_precision_fmt = self.qty_precision
            qty_str = f"{quantity:.{qty_precision_fmt}f}"
            # Готуємо параметри для API
            params = {
                'symbol': self.symbol,
                'side': 'BUY',
                'type': 'MARKET',
                'quantity': qty_str,
                'newClientOrderId': client_order_id
            }
            logger.debug(f"API place_order params (ENTRY): {params}")
            # Викликаємо метод API
            api_response = self.binance_api.place_order(**params)

            # Аналізуємо відповідь від API
            order_id_binance = None
            order_status = None
            if api_response:
                 order_id_binance = api_response.get('orderId')
                 order_status = api_response.get('status')

            # Перевіряємо, чи ордер прийнято біржею
            is_accepted_by_exchange = False
            if order_status:
                is_accepted_by_exchange = order_status in ['NEW', 'PARTIALLY_FILLED', 'FILLED']

            if is_accepted_by_exchange:
                # Якщо ордер прийнято, логуємо успіх
                logger.info(f"Ордер ENTRY ({client_order_id}) успішно розміщено на біржі. Binance ID: {order_id_binance}, Статус: {order_status}")
                # Формуємо дані для збереження в активних ордерах
                active_order_details = {
                    'symbol': self.symbol,
                    'order_id_binance': order_id_binance,
                    'client_order_id': client_order_id,
                    'side': 'BUY',
                    'type': 'MARKET',
                    'quantity_req': quantity, # Зберігаємо запитану кількість
                    'status': order_status, # Зберігаємо статус з відповіді
                    'purpose': 'ENTRY'
                }
                # Додаємо ордер до стану через StateManager (потокобезпечно)
                self.state_manager.add_or_update_active_order(client_order_id, active_order_details)
                logger.debug(f"Ордер ENTRY {client_order_id} додано до active_orders (через StateManager).")
            else:
                # Якщо ордер не прийнято або відповідь некоректна
                error_msg = f"Не вдалося розмістити ордер ENTRY для {self.symbol}. Відповідь API: {api_response}"
                logger.error(error_msg)
                # Надсилаємо подію про помилку
                error_payload = {'source':'BotCore._place_entry_order', 'message':error_msg}
                self._put_event({'type':'ERROR', 'payload': error_payload})

        except BinanceAPIError as e:
            # Обробка специфічних помилок API Binance
            error_code = e.code
            error_msg_api = f"API помилка при розміщенні ордера ENTRY: Code={error_code}, Msg={e.msg}"
            logger.error(error_msg_api)
            error_payload_api = {'source':'BotCore._place_entry_order', 'message':error_msg_api}
            self._put_event({'type':'ERROR', 'payload': error_payload_api})
        except Exception as e:
            # Обробка інших можливих помилок
            error_msg_general = f"Загальна помилка при розміщенні ордера ENTRY: {e}"
            logger.error(error_msg_general, exc_info=True)
            error_payload_general = {'source':'BotCore._place_entry_order', 'message':error_msg_general}
            self._put_event({'type':'ERROR', 'payload': error_payload_general})


    def _place_oco_order(self, symbol: str, quantity: Decimal, entry_price: Decimal):
        """Розміщує OCO ордер (TP + SL). Виконується в окремому потоці."""
        # Перевіряємо наявність інфо про символ
        symbol_info_exists = self.symbol_info is not None
        if not symbol_info_exists:
            logger.error("Немає інфо символу для розміщення OCO")
            error_payload_no_info = {'source':'BotCore._place_oco_order', 'message':'Немає інфо символу для OCO.'}
            self._put_event({'type':'ERROR', 'payload': error_payload_no_info})
            return

        # Перевіряємо, чи увімкнено TP та SL
        tp_enabled = False
        if self.tp_percent is not None:
             if self.tp_percent > 0:
                  tp_enabled = True
        sl_enabled = False
        if self.sl_percent is not None:
             if self.sl_percent > 0:
                  sl_enabled = True

        # OCO вимагає і TP, і SL
        if not tp_enabled or not sl_enabled:
             logger.error("Для розміщення OCO ордера мають бути увімкнені і TP (>0), і SL (>0).")
             error_payload_tp_sl = {'source':'BotCore._place_oco_order', 'message':'Для OCO потрібні TP>0 і SL>0.'}
             self._put_event({'type':'ERROR', 'payload': error_payload_tp_sl})
             # TODO: Реалізувати розміщення окремих ордерів TP/SL, якщо один з них = 0.
             return

        # Якщо обидва увімкнені, продовжуємо
        logger.info(f"Спроба розміщення OCO для {symbol}, Qty={quantity}, Entry={entry_price}...")
        try:
            # Визначаємо крок ціни (tick_size)
            price_precision_local = self.price_precision # Використовуємо атрибут класу
            tick_size_power = Decimal('10') ** -price_precision_local
            tick_size = Decimal('1') * tick_size_power

            # Визначаємо крок кількості (step_size)
            qty_precision_local = self.qty_precision
            step_size_default = Decimal('1') * (Decimal('10') ** -qty_precision_local)
            step_size = step_size_default # Значення за замовчуванням
            lot_size_filter = None
            symbol_filters = self.symbol_info.get('filters',[])
            for f in symbol_filters:
                 filter_type = f.get('filterType')
                 if filter_type == 'LOT_SIZE':
                      lot_size_filter = f
                      break
            if lot_size_filter:
                 try:
                     step_size_str = lot_size_filter.get('stepSize')
                     if step_size_str: # Перевіряємо, чи значення існує
                          step_size = Decimal(step_size_str)
                 except Exception as step_e:
                     logger.warning(f"Не вдалося отримати stepSize з фільтру {lot_size_filter}, використано дефолтну точність. Помилка: {step_e}")

            # --- Розрахунок цін TP/SL ---
            # Take Profit Price (округлення донизу до tick_size)
            one_plus_tp = Decimal(1) + self.tp_percent
            price_tp_raw = entry_price * one_plus_tp
            tp_price_quantized = (price_tp_raw / tick_size).quantize(Decimal('1'), rounding=ROUND_DOWN)
            take_profit_price = tp_price_quantized * tick_size
            tp_price_str_log = f"{take_profit_price:.{price_precision_local}f}"
            logger.debug(f"Розраховано TP Price: {tp_price_str_log}")

            # Stop Loss Trigger Price (округлення доверху до tick_size)
            one_minus_sl = Decimal(1) - self.sl_percent
            price_sl_trigger_raw = entry_price * one_minus_sl
            sl_trigger_quantized = (price_sl_trigger_raw / tick_size).quantize(Decimal('1'), rounding=ROUND_UP)
            stop_loss_price = sl_trigger_quantized * tick_size

            # Stop Loss Limit Price (трохи нижче тригера)
            stop_limit_price = stop_loss_price - tick_size
            # Перевірка, щоб Stop Limit не був <= 0
            if stop_limit_price <= 0:
                  stop_limit_price = tick_size # Мінімально можлива ціна
                  sl_trigger_str = f"{stop_loss_price}" # Для логу
                  logger.warning(f"Stop Loss Trigger ({sl_trigger_str}) дуже низький, Stop Limit Price встановлено на {tick_size}")
            sl_trigger_str_log = f"{stop_loss_price:.{price_precision_local}f}"
            sl_limit_str_log = f"{stop_limit_price:.{price_precision_local}f}"
            logger.debug(f"Розраховано SL Trigger: {sl_trigger_str_log}, SL Limit: {sl_limit_str_log}")

            # Коригуємо кількість для OCO (має відповідати step_size)
            quantity_oco_quantized = (quantity / step_size).quantize(Decimal('1'), rounding=ROUND_DOWN)
            quantity_oco = quantity_oco_quantized * step_size
            qty_oco_str_log = f"{quantity_oco:.{qty_precision_local}f}"
            logger.debug(f"Скоригована кількість для OCO: {qty_oco_str_log}")

            # --- Перевірки розрахованих значень ---
            if take_profit_price <= entry_price:
                 tp_check_err = ValueError(f"TP Price ({take_profit_price}) <= Entry Price ({entry_price})")
                 raise tp_check_err
            if stop_loss_price >= entry_price:
                 sl_check_err = ValueError(f"SL Price ({stop_loss_price}) >= Entry Price ({entry_price})")
                 raise sl_check_err
            if stop_limit_price >= stop_loss_price:
                 sl_limit_check_err = ValueError(f"SL Limit Price ({stop_limit_price}) >= SL Trigger ({stop_loss_price})")
                 raise sl_limit_check_err
            if quantity_oco <= 0:
                 qty_check_err = ValueError(f"Кількість OCO <= 0 ({quantity_oco})")
                 raise qty_check_err

            # --- Формуємо параметри для API ---
            qty_oco_api_str = f"{quantity_oco:.{qty_precision_local}f}"
            tp_price_api_str = f"{take_profit_price:.{price_precision_local}f}"
            sl_trigger_api_str = f"{stop_loss_price:.{price_precision_local}f}"
            sl_limit_api_str = f"{stop_limit_price:.{price_precision_local}f}"
            params={
                'symbol': symbol,
                'side': 'SELL',
                'quantity': qty_oco_api_str,
                'stopPrice': sl_trigger_api_str,
                'stopLimitPrice': sl_limit_api_str,
                'stopLimitTimeInForce': 'GTC',
                'price': tp_price_api_str, # Ціна для Limit Maker (TP)
            }
            # Генеруємо унікальні ID
            list_cid = self._generate_client_order_id('oco')
            limit_cid = self._generate_client_order_id('tp')
            stop_cid = self._generate_client_order_id('sl')
            # Додаємо ID до параметрів
            params['listClientOrderId'] = list_cid
            params['limitClientOrderId'] = limit_cid
            params['stopClientOrderId'] = stop_cid

            # Логуємо спробу розміщення
            logger.info(f"Спроба розміщення OCO: Qty={qty_oco_str_log}, TP={tp_price_str_log}, SLTrigger={sl_trigger_str_log}, SLLimit={sl_limit_str_log}. [ListCID:{list_cid}]")
            logger.debug(f"API place_oco_order params: {params}")

            # Викликаємо API метод
            api_response = self.binance_api.place_oco_order(**params)

            # --- Обробка результату ---
            order_list_accepted = False
            if api_response:
                 if 'listClientOrderId' in api_response:
                      order_list_accepted = True

            if order_list_accepted:
                returned_list_cid = api_response['listClientOrderId']
                binance_order_list_id = api_response.get('orderListId', -1) # Отримуємо ID від Binance
                logger.info(f"OCO ордер ({returned_list_cid}) успішно розміщено. Binance OrderListId: {binance_order_list_id}")

                # Оновлюємо запис позиції через StateManager
                oco_id_to_save = binance_order_list_id
                if oco_id_to_save == -1: # Якщо Binance не повернув ID списку
                     oco_id_to_save = returned_list_cid # Використовуємо наш ID
                self.state_manager.update_open_position_field('oco_list_id', oco_id_to_save)
                logger.info(f"OCO ID {oco_id_to_save} збережено для позиції (через StateManager).")

                # Додаємо обидві частини OCO до активних ордерів через StateManager
                order_reports = api_response.get('orderReports', [])
                for report in order_reports:
                    client_order_id_part = report.get('clientOrderId')
                    binance_order_id_part = report.get('orderId')
                    order_type_part = report.get('type')
                    order_status_part = report.get('status', 'NEW')
                    order_purpose = 'UNKNOWN_OCO'
                    if order_type_part == 'LIMIT_MAKER': purpose = 'TP'
                    elif order_type_part == 'STOP_LOSS_LIMIT': purpose = 'SL'
                    else: purpose = order_purpose

                    # Додаємо тільки якщо є Client Order ID
                    if client_order_id_part:
                         active_order_data = {
                             'symbol': symbol, 'order_id_binance': binance_order_id_part,
                             'client_order_id': client_order_id_part, 'list_client_order_id': returned_list_cid,
                             'order_list_id_binance': binance_order_list_id, 'side': 'SELL',
                             'type': order_type_part, 'quantity_req': Decimal(report.get('origQty','0')),
                             'price': Decimal(report.get('price','0')), 'stop_price': Decimal(report.get('stopPrice','0')),
                             'status': order_status_part, 'purpose': purpose
                         }
                         self.state_manager.add_or_update_active_order(client_order_id_part, active_order_data)
                         logger.debug(f"Активний ордер OCO {client_order_id_part} ({purpose}) додано (через StateManager).")

                # Надсилаємо подію про успішне розміщення
                info_payload_oco = {'source':'BotCore._place_oco_order', 'message':f'OCO ордер для {symbol} розміщено.'}
                self._put_event({'type':'INFO', 'payload': info_payload_oco})

            else:
                # Якщо ордер не було розміщено
                error_msg_oco_place = f"Не вдалося розмістити OCO ордер для {symbol}. Відповідь API: {api_response}"
                logger.error(error_msg_oco_place)
                error_payload_oco_place = {'source':'BotCore._place_oco_order', 'message': error_msg_oco_place}
                self._put_event({'type':'ERROR', 'payload': error_payload_oco_place})

        except BinanceAPIError as e:
            error_code = e.code
            error_msg_api = f"API помилка при розміщенні OCO: Code={error_code}, Msg={e.msg}"
            logger.error(error_msg_api)
            error_payload_api = {'source':'BotCore._place_oco_order', 'message': error_msg_api}
            self._put_event({'type':'ERROR', 'payload': error_payload_api})
        except Exception as e:
            error_msg_general = f"Загальна помилка при розміщенні OCO: {e}"
            logger.error(error_msg_general, exc_info=True)
            error_payload_general = {'source':'BotCore._place_oco_order', 'message': error_msg_general}
            self._put_event({'type':'ERROR', 'payload': error_payload_general})


    def _cancel_other_oco_part(self, symbol: str, list_id: str, executed_order_id: int):
        """Скасовує іншу частину OCO ордера після виконання однієї з частин. Виконується в окремому потоці."""
        logger.info(f"Обробка скасування іншої частини OCO (ListID: {list_id}) після виконання ордера {executed_order_id}...")
        try:
            # Логуємо, що очікуємо автоматичного скасування біржею
            logger.info(f"Інша частина OCO ({list_id}) має бути автоматично скасована біржею.")

            # Очищаємо залишки OCO з нашого списку активних ордерів через StateManager
            # Отримуємо поточні активні ордери
            all_active_orders = self.state_manager.get_all_active_orders()
            orders_to_remove_cids = []
            # Ітеруємо по копії ключів, щоб уникнути помилок зміни розміру під час ітерації
            current_cids = list(all_active_orders.keys())
            for cid in current_cids:
                 order_details = all_active_orders.get(cid)
                 if order_details:
                      # Отримуємо ID списків (клієнтський та біржовий)
                      binance_list_id = order_details.get('order_list_id_binance')
                      client_list_id = order_details.get('list_client_order_id')
                      # Перевіряємо відповідність будь-якого з ID списку
                      is_same_list = False
                      # Порівнюємо як рядки для універсальності
                      list_id_str = str(list_id)
                      if binance_list_id is not None:
                           binance_list_id_str = str(binance_list_id)
                           if binance_list_id_str == list_id_str:
                                is_same_list = True
                      # Перевіряємо клієнтський ID, якщо біржовий не співпав
                      if not is_same_list:
                           if client_list_id is not None:
                                client_list_id_str = str(client_list_id)
                                if client_list_id_str == list_id_str:
                                     is_same_list = True

                      # Перевіряємо, що це НЕ той ордер, який щойно виконався
                      order_id_binance_part = order_details.get('order_id_binance')
                      is_not_executed = True
                      if order_id_binance_part is not None:
                           is_not_executed = order_id_binance_part != executed_order_id

                      # Якщо це той самий OCO список і не виконаний ордер, додаємо до видалення
                      if is_same_list and is_not_executed:
                           orders_to_remove_cids.append(cid)

            # Видаляємо знайдені ордери через StateManager
            if orders_to_remove_cids:
                 ids_str = ", ".join(map(str, orders_to_remove_cids)) # Формуємо рядок ID
                 logger.info(f"Видалення неактуальних частин OCO ({list_id}) з active_orders: [{ids_str}]")
                 # Видаляємо кожен ордер
                 for cid_to_remove in orders_to_remove_cids:
                      self.state_manager.remove_active_order(cid_to_remove)
            else:
                 # Якщо не знайшли, що видаляти
                 logger.info(f"Не знайдено інших активних частин OCO ({list_id}) для видалення з active_orders.")

        except Exception as e:
            # Обробляємо будь-які помилки доступу до даних або StateManager
            logger.error(f"Загальна помилка при обробці скасування OCO ({list_id}): {e}", exc_info=True)


    def _place_exit_order(self, quantity: Decimal, purpose="EXIT_MANUAL"):
        """Розміщує ордер на вихід (MARKET SELL). Виконується в окремому потоці."""
        # Генеруємо ID
        purpose_prefix = purpose.lower().replace(' ','_') # Форматуємо префікс
        client_order_id = self._generate_client_order_id(purpose_prefix)
        # Логуємо
        logger.info(f"Спроба розміщення ордера EXIT MARKET SELL {quantity} {self.symbol} (Причина: {purpose})... [CID:{client_order_id}]")
        try:
            # Форматуємо кількість
            qty_precision_local = self.qty_precision
            qty_str = f"{quantity:.{qty_precision_local}f}"
            # Готуємо параметри
            params = {
                'symbol': self.symbol,
                'side': 'SELL',
                'type': 'MARKET',
                'quantity': qty_str,
                'newClientOrderId': client_order_id
            }
            logger.debug(f"API place_order params (EXIT): {params}")
            # Розміщуємо ордер
            api_response = self.binance_api.place_order(**params)

            # Аналізуємо відповідь
            order_id_binance = None
            order_status = None
            if api_response:
                 order_id_binance = api_response.get('orderId')
                 order_status = api_response.get('status')

            # Перевіряємо статус
            is_accepted_by_exchange = False
            if order_status:
                is_accepted_by_exchange = order_status in ['NEW', 'PARTIALLY_FILLED', 'FILLED']

            if is_accepted_by_exchange:
                # Якщо ордер прийнято
                logger.info(f"Ордер EXIT ({client_order_id}) успішно розміщено на біржі. Binance ID: {order_id_binance}, Статус: {order_status}")
                # Додаємо до активних через StateManager
                active_order_details = {
                     'symbol': self.symbol,
                     'order_id_binance': order_id_binance,
                     'client_order_id': client_order_id,
                     'side': 'SELL',
                     'type': 'MARKET',
                     'quantity_req': quantity,
                     'status': order_status,
                     'purpose': purpose
                }
                self.state_manager.add_or_update_active_order(client_order_id, active_order_details)
                logger.debug(f"Ордер EXIT {client_order_id} додано до active_orders (через StateManager).")
            else:
                # Якщо ордер не прийнято
                error_msg_exit_place = f"Не вдалося розмістити ордер EXIT для {self.symbol}. Відповідь API: {api_response}"
                logger.error(error_msg_exit_place)
                error_payload_exit_place = {'source':'BotCore._place_exit_order', 'message':error_msg_exit_place}
                self._put_event({'type':'ERROR', 'payload': error_payload_exit_place})
                # Це критична ситуація!
                logger.critical(f"НЕ ВДАЛОСЯ РОЗМІСТИТИ ОРДЕР НА ВИХІД для {self.symbol}!")
                critical_payload_exit = {'source':'BotCore._place_exit_order', 'message':f"КРИТИЧНО! Не вдалося розмістити ордер на вихід для {self.symbol}!"}
                self._put_event({'type':'CRITICAL_ERROR', 'payload': critical_payload_exit})

        except BinanceAPIError as e:
            # Обробка помилок API
            error_code = e.code
            error_msg_api = f"API помилка при розміщенні ордера EXIT: Code={error_code}, Msg={e.msg}"
            logger.error(error_msg_api)
            error_payload_api = {'source':'BotCore._place_exit_order', 'message':error_msg_api}
            self._put_event({'type':'ERROR', 'payload': error_payload_api})
            critical_payload_exit_api = {'source':'BotCore._place_exit_order', 'message':f"КРИТИЧНО! Не вдалося розмістити ордер на вихід (API Помилка {error_code})!"}
            logger.critical(critical_payload_exit_api['message']) # Логуємо критичну помилку
            self._put_event({'type':'CRITICAL_ERROR', 'payload': critical_payload_exit_api})

        except Exception as e:
            # Обробка інших помилок
            error_msg_general = f"Загальна помилка при розміщенні ордера EXIT: {e}"
            logger.error(error_msg_general, exc_info=True)
            error_payload_general = {'source':'BotCore._place_exit_order', 'message':error_msg_general}
            self._put_event({'type':'ERROR', 'payload': error_payload_general})
            critical_payload_exit_gen = {'source':'BotCore._place_exit_order', 'message':f"КРИТИЧНО! Не вдалося розмістити ордер на вихід (Загальна помилка)!"}
            logger.critical(critical_payload_exit_gen['message']) # Логуємо критичну помилку
            self._put_event({'type':'CRITICAL_ERROR', 'payload': critical_payload_exit_gen})
# END BLOCK 12
# BLOCK 13: Public State Getter Methods
    def get_status(self) -> dict:
        """Повертає поточний стан бота, використовуючи StateManager."""
        # Отримуємо дані стану через StateManager
        # Ці методи повертають копії або None
        pos = self.state_manager.get_open_position()
        active_orders_dict = self.state_manager.get_all_active_orders()

        # Отримуємо дані з самого BotCore
        is_running = self.is_running
        mode = self.active_mode
        sym = self.symbol
        strat = self.selected_strategy_name

        # Перевіряємо стан WebSocket
        ws_conn = False
        ws_handler_instance = self.ws_handler
        # Перевіряємо, чи існує обробник і викликаємо is_connected
        if ws_handler_instance:
             ws_conn = ws_handler_instance.is_connected()

        # Визначаємо інші показники
        pos_act = pos is not None
        act_ord_cnt = len(active_orders_dict)

        # Формуємо фінальний словник статусу
        status_dict = {
            "is_running": is_running,
            "active_mode": mode,
            "symbol": sym,
            "strategy": strat,
            "websocket_connected": ws_conn,
            "position_active": pos_act,
            "position_details": pos, # Передаємо отриману копію або None
            "active_orders_count": act_ord_cnt
        }
        return status_dict

    def get_active_orders(self) -> dict:
        """Повертає копію словника активних ордерів через StateManager."""
        # Делегуємо отримання StateManager, він повертає копію
        active_orders_copy = self.state_manager.get_all_active_orders()
        return active_orders_copy

    def get_open_position(self) -> dict | None: # Однина
        """Повертає копію відкритої позиції (або None) через StateManager."""
        # Делегуємо отримання StateManager
        open_position_copy = self.state_manager.get_open_position()
        return open_position_copy

    def get_balance(self) -> dict:
        """Повертає копію словника балансів (ініціює фонове завантаження, якщо потрібно)."""
        # Отримуємо баланс через StateManager (він повертає копію)
        current_balances = self.state_manager.get_balances()

        # Перевіряємо, чи потрібно ініціювати фонове завантаження
        # Робимо це тільки якщо словник порожній і API ініціалізовано
        no_balance_data = not current_balances # Перевірка на порожній словник
        api_exists = self.binance_api is not None
        should_initiate_fetch = no_balance_data and api_exists

        if should_initiate_fetch:
             logger.info("get_balance: Баланс порожній, ініціюємо фонове завантаження...")
             # Запускаємо завантаження в окремому потоці
             fetch_thread_name = "FetchBalanceOnDemandGet"
             fetch_thread = threading.Thread(
                 target=self._fetch_initial_balance, # Викликаємо внутрішній метод ядра
                 daemon=True,
                 name=fetch_thread_name
             )
             fetch_thread.start()

        # Повертаємо поточну копію балансу
        return current_balances

    def get_trade_history(self, count: int = 50) -> list:
        """Повертає копію останніх N записів історії угод через StateManager."""
        # Делегуємо отримання StateManager (він повертає копію)
        history_copy = self.state_manager.get_trade_history(count)
        return history_copy

    def get_session_stats(self) -> dict:
        """Повертає копію статистики поточної сесії (конвертовано у float)."""
        # Статистика зберігається локально в BotCore
        stats_local_copy = {}
        duration_str = "N/A"

        # Читаємо локальну статистику (немає потреби в lock, якщо оновлення тільки в одному потоці)
        current_session_stats = self.session_stats
        # Робимо глибоку копію
        stats_local_copy = copy.deepcopy(current_session_stats)

        # Розраховуємо тривалість сесії
        is_bot_running_now = self.is_running
        session_start_time = self.start_time
        # Перевіряємо, чи бот запущено і чи є час старту
        if is_bot_running_now and session_start_time:
            # Розраховуємо різницю в часі
            current_time_utc = datetime.now(timezone.utc)
            duration = current_time_utc - session_start_time
            # Форматуємо тривалість (прибираємо мікросекунди)
            duration_str = str(duration).split('.')[0]

        # Додаємо тривалість до копії статистики
        stats_local_copy['session_duration'] = duration_str

        # Конвертуємо значення Decimal у float для зручності UI/JSON
        final_stats_float = {}
        # Ітеруємо по скопійованому словнику
        for key, value in stats_local_copy.items():
            # Перевіряємо тип значення
            is_decimal_value = isinstance(value, Decimal)
            if is_decimal_value:
                # Конвертуємо в float
                final_stats_float[key] = float(value)
            else:
                # Залишаємо значення як є
                final_stats_float[key] = value
        # Повертаємо фінальний словник зі значеннями float
        return final_stats_float
# END BLOCK 13
# BLOCK 14: Command Processing Method
    def process_command(self, command: dict):
        """Обробляє команду, отриману з command_queue."""
        # Отримуємо тип команди та дані
        cmd_type = command.get('type')
        payload = command.get('payload', {}) # Використовуємо порожній словник як дефолт

        # Логуємо отриману команду
        payload_str = str(payload) # Конвертуємо payload в рядок для логу
        logger.info(f"[CMD] Отримано: {cmd_type} | Payload: {payload_str}")

        # Встановлюємо відповідь за замовчуванням
        response_success = False
        response_message = f"Невідомий тип команди: {cmd_type}" # Повідомлення за замовчуванням

        try:
            # --- Обробка команди START ---
            if cmd_type == 'START':
                is_already_running = self.is_running
                if is_already_running:
                     # Якщо ядро вже запущено, встановлюємо відповідь про помилку
                     response_success = False
                     response_message = 'Ядро вже запущено.'
                     # Надсилаємо інформаційне повідомлення
                     info_payload_start_err = {'source':'BotCore.process_command', 'message': response_message}
                     self._put_event({'type':'INFO', 'payload': info_payload_start_err})
                else:
                     # Якщо ядро не запущено, запускаємо метод start в окремому потоці
                     start_thread_name = "BotCoreStartThread"
                     start_thread = threading.Thread(target=self.start, daemon=True, name=start_thread_name)
                     start_thread.start()
                     # Встановлюємо успішну відповідь
                     response_success = True
                     response_message = 'Команду запуску ядра прийнято.'
                     # Надсилаємо інформаційне повідомлення про початок запуску
                     info_payload_start_ok = {'source':'BotCore.process_command', 'message': response_message}
                     self._put_event({'type':'INFO', 'payload': info_payload_start_ok})

            # --- Обробка команди STOP ---
            elif cmd_type == 'STOP':
                 is_currently_running = self.is_running
                 if not is_currently_running:
                      # Якщо ядро вже зупинено
                      response_success = False
                      response_message = 'Ядро вже зупинено.'
                      # Надсилаємо інформаційне повідомлення
                      info_payload_stop_err = {'source':'BotCore.process_command', 'message': response_message}
                      self._put_event({'type':'INFO', 'payload': info_payload_stop_err})
                 else:
                      # Якщо ядро працює, запускаємо метод stop в окремому потоці
                      stop_thread_name = "BotCoreStopThread"
                      stop_thread = threading.Thread(target=self.stop, daemon=True, name=stop_thread_name)
                      stop_thread.start()
                      # Встановлюємо успішну відповідь
                      response_success = True
                      response_message = 'Команду зупинки ядра прийнято.'
                      # Надсилаємо інформаційне повідомлення про початок зупинки
                      info_payload_stopping = {'source':'BotCore.process_command', 'message': response_message}
                      self._put_event({'type':'INFO', 'payload': info_payload_stopping})

            # --- Обробка команди CLOSE_POSITION ---
            elif cmd_type == 'CLOSE_POSITION':
                # Визначаємо символ для закриття (з payload або дефолтний)
                symbol_to_close = payload.get('symbol', self.symbol)
                # Перевіряємо, чи символ збігається з активним
                symbols_match = (symbol_to_close == self.symbol)
                # Перевіряємо, чи ядро запущено
                core_is_running_for_close = self.is_running

                if not symbols_match:
                     # Якщо символи не збігаються
                     response_success = False
                     response_message = f"Символ {symbol_to_close} не є активним символом бота ({self.symbol})."
                     # Надсилаємо попередження
                     warning_payload_symbol = {'source':'BotCore.process_command', 'message': response_message}
                     self._put_event({'type':'WARNING', 'payload': warning_payload_symbol})
                elif not core_is_running_for_close:
                     # Якщо ядро не запущено
                     response_success = False
                     response_message = 'Ядро бота не запущено для закриття позиції.'
                     # Надсилаємо попередження
                     warning_payload_running = {'source':'BotCore.process_command', 'message': response_message}
                     self._put_event({'type':'WARNING', 'payload': warning_payload_running})
                else:
                    # Якщо символ правильний і ядро працює, перевіряємо позицію
                    current_pos = self.state_manager.get_open_position()
                    has_position = current_pos is not None
                    if has_position:
                         # Якщо позиція є, ініціюємо вихід в окремому потоці
                         exit_thread_name_manual = f"ManualExit-{symbol_to_close}"
                         exit_reason_manual = "Manual Command"
                         exit_thread_manual = threading.Thread(
                             target=self._initiate_exit,
                             args=(exit_reason_manual,),
                             daemon=True,
                             name=exit_thread_name_manual
                         )
                         exit_thread_manual.start()
                         # Встановлюємо успішну відповідь
                         response_success = True
                         response_message = f'Команду примусового закриття позиції {symbol_to_close} надіслано.'
                         # Надсилаємо інформаційне повідомлення
                         info_payload_closing = {'source':'BotCore.process_command', 'message': response_message}
                         self._put_event({'type':'INFO', 'payload': info_payload_closing})
                    else:
                         # Якщо позиції немає
                         response_success = False
                         response_message = f"Немає відкритої позиції {symbol_to_close} для закриття."
                         # Надсилаємо інформаційне повідомлення
                         info_payload_no_pos = {'source':'BotCore.process_command', 'message': response_message}
                         self._put_event({'type':'INFO', 'payload': info_payload_no_pos})

            # --- Обробка команди RELOAD_SETTINGS ---
            elif cmd_type == 'RELOAD_SETTINGS':
                logger.info("Перезавантаження загальних налаштувань бота...")
                try:
                    # Зберігаємо старий конфіг для порівняння
                    old_config = copy.deepcopy(self.config)
                    # Завантажуємо нові налаштування
                    self._load_bot_settings()
                    # Порівнюємо старий та новий конфіги (просте порівняння словників)
                    config_changed = (old_config != self.config)
                    # Встановлюємо відповідь
                    if config_changed:
                         response_message = 'Загальні налаштування бота перезавантажено та оновлено.'
                         logger.info(response_message) # Логуємо оновлення
                    else:
                         response_message = 'Загальні налаштування перезавантажено (змін не виявлено).'
                    response_success = True
                    # Повідомляємо UI/інші компоненти про оновлення конфігу
                    settings_reloaded_payload = self.config # Надсилаємо новий конфіг
                    settings_reloaded_event = {'type':'SETTINGS_RELOADED', 'payload': settings_reloaded_payload}
                    self._put_event(settings_reloaded_event)
                except ValueError as e:
                    # Якщо помилка під час завантаження/валідації
                    response_success = False
                    response_message = f"Помилка перезавантаження налаштувань: {e}"
                    # Надсилаємо подію про помилку
                    error_payload_reload = {'source':'BotCore.process_command', 'message': response_message}
                    self._put_event({'type':'ERROR', 'payload': error_payload_reload})

            # --- Обробка команд отримання стану ---
            elif cmd_type == 'GET_STATUS':
                status_data = self.get_status()
                status_payload = status_data
                status_event = {'type':'STATUS_RESPONSE', 'payload': status_payload}
                self._put_event(status_event)
                response_success = True
                response_message = 'Статус надіслано.'
            elif cmd_type == 'GET_BALANCE':
                balance_data = self.get_balance()
                balance_payload = balance_data
                balance_event = {'type':'BALANCE_RESPONSE', 'payload': balance_payload}
                self._put_event(balance_event)
                response_success = True
                response_message = 'Баланс надіслано.'
            elif cmd_type == 'GET_POSITION':
                pos_data = self.get_open_position()
                position_payload = pos_data
                position_event = {'type':'POSITION_RESPONSE', 'payload': position_payload}
                self._put_event(position_event)
                response_success = True
                response_message = 'Позицію надіслано.'
            elif cmd_type == 'GET_STATS':
                stats_data = self.get_session_stats()
                stats_payload = stats_data
                stats_event = {'type':'STATS_RESPONSE', 'payload': stats_payload}
                self._put_event(stats_event)
                response_success = True
                response_message = 'Статистику надіслано.'
            elif cmd_type == 'GET_HISTORY':
                default_history_count = 10 # Кількість за замовчуванням
                count = payload.get('count', default_history_count)
                # Перевіряємо, чи count є числом і > 0
                try:
                     history_count = int(count)
                     if history_count <= 0:
                          history_count = default_history_count # Використовуємо дефолт, якщо <= 0
                except (ValueError, TypeError):
                     history_count = default_history_count # Використовуємо дефолт, якщо не число
                # Отримуємо історію
                history_data = self.get_trade_history(history_count)
                history_len = len(history_data) # Фактична кількість отриманих записів
                # Формуємо payload для події
                history_payload = {'count': history_count, 'trades': history_data}
                history_event = {'type':'HISTORY_RESPONSE', 'payload': history_payload}
                self._put_event(history_event)
                response_success = True
                response_message = f'Історію ({history_len}) надіслано.'

            # --- Обробка невідомої команди ---
            else:
                logger.warning(f"Отримано невідомий тип команди: {cmd_type}")
                # response_success залишається False
                # response_message вже встановлено за замовчуванням
                # Надсилаємо попередження
                warning_payload_unknown = {'source':'BotCore.process_command', 'message': response_message}
                self._put_event({'type':'WARNING', 'payload': warning_payload_unknown})

            # Логуємо фінальний результат обробки команди
            # (не плутати з відповіддю на запит даних, яка надсилається через _put_event)
            final_response = {'success': response_success, 'message': response_message}
            logger.info(f"[CMD] Результат обробки {cmd_type}: {final_response}")

        except Exception as e:
            # Обробка будь-яких непередбачених помилок під час виконання команди
            logger.error(f"[CMD] Критична помилка обробки команди {cmd_type}: {e}", exc_info=True)
            # Встановлюємо відповідь про помилку
            response_success = False
            response_message = f'Внутрішня помилка сервера при обробці команди {cmd_type}.'
            # Надсилаємо подію про помилку через event_queue
            error_payload_cmd = {'source':'BotCore.process_command', 'message': response_message}
            self._put_event({'type':'ERROR', 'payload': error_payload_cmd})
# END BLOCK 14
# BLOCK 15: Testing Block (if __name__ == '__main__')
if __name__ == '__main__':
    # Імпортуємо необхідні модулі для тестування
    from dotenv import load_dotenv
    from modules.logger import setup_logger # Використовуємо setup_logger
    import queue
    import threading
    import logging

    # Завантажуємо змінні середовища
    load_dotenv()
    # Налаштовуємо окремий логер для тестування
    # Встановлюємо рівень DEBUG для детальних логів
    test_logger_name = "BotCoreTestLogger"
    test_log_level = logging.DEBUG
    logger = setup_logger(level=test_log_level, name=test_logger_name)

    logger.info("--- Запуск BotCore напряму (для тестування) ---")
    # Ініціалізуємо змінні перед блоком try
    bot_instance = None
    # bot_start_thread = None # Більше не потрібен, запуск через команду
    event_listener_thread = None
    stop_event_listener_flag = threading.Event()

    try:
        # Створюємо менеджер конфігурації
        test_config_file = 'config.json' # Використовуємо стандартний
        config_mgr = ConfigManager(config_file=test_config_file)
        # Отримуємо конфіг
        cfg = config_mgr.get_bot_config()

        # Переконуємось, що в конфігу є стратегія для тесту
        strategy_in_config = cfg.get('selected_strategy')
        if not strategy_in_config:
            logger.warning("Встановлюємо тестову стратегію в конфіг...")
            # Встановлюємо значення за замовчуванням
            cfg['selected_strategy'] = 'MovingAverageCrossoverStrategy'
            default_mode = 'TestNet'
            cfg['active_mode'] = cfg.get('active_mode') or default_mode
            default_symbol = 'BTCUSDT'
            cfg['trading_symbol'] = cfg.get('trading_symbol') or default_symbol
            # Зберігаємо оновлений конфіг
            config_mgr.set_bot_config(cfg)
            logger.info(f"Використовується оновлений конфіг: {cfg}")

        # Створюємо черги для тестування
        cmd_q = queue.Queue()
        evt_q = queue.Queue()
        # Створюємо екземпляр ядра бота
        bot_instance = BotCore(config_mgr, command_queue=cmd_q, event_queue=evt_q)

        # Перевіряємо, чи ініціалізація пройшла успішно
        api_ready_test = bot_instance.binance_api is not None
        strategy_ready_test = bot_instance.strategy is not None

        if api_ready_test and strategy_ready_test:
            # Якщо ініціалізація ОК, надсилаємо команду START через чергу
            logger.info("Надсилання команди START до ядра...")
            start_command = {'type':'START'}
            cmd_q.put(start_command)

            # Чекаємо на запуск ядра (з таймаутом)
            logger.info("Очікування запуску ядра... (Max 15 сек)")
            start_wait_begin = time.time()
            max_wait_seconds = 15
            core_started_successfully = False
            # Цикл очікування
            while True:
                 current_loop_time = time.time()
                 elapsed_wait_time = current_loop_time - start_wait_begin
                 # Перевірка таймауту
                 if elapsed_wait_time >= max_wait_seconds:
                      break # Виходимо, якщо час вийшов
                 # Перевірка стану ядра
                 is_core_running_check = bot_instance.is_running
                 if is_core_running_check:
                      core_started_successfully = True
                      break # Виходимо, якщо ядро запустилося
                 # Невелика пауза
                 time.sleep(0.5)

            # Перевіряємо, чи ядро запустилося
            if not core_started_successfully:
                 # Якщо не запустилося за відведений час
                 error_msg_timeout = f"BotCore не запустився протягом {max_wait_seconds} секунд!"
                 logger.error(error_msg_timeout)
                 # Зупиняємо обробник команд
                 if bot_instance:
                      bot_instance._stop_command_processor()
                 # Генеруємо помилку
                 runtime_error = RuntimeError(error_msg_timeout)
                 raise runtime_error

            # Якщо ядро успішно запустилося
            logger.info("BotCore підтвердив запуск. Натисніть Ctrl+C для зупинки.")

            # Запускаємо слухача подій для відображення подій в консолі
            def event_listener():
                logger.debug("TestEventListener: Потік слухача подій запущено.")
                # Цикл працює, доки не встановлено прапорець зупинки
                while not stop_event_listener_flag.is_set():
                    try:
                        # Отримуємо подію з черги evt_q
                        event = evt_q.get(timeout=1)
                        # Логуємо отриману подію
                        event_type_log = event.get('type')
                        logger.info(f"--- Подія з черги: {event_type_log} ---")
                        payload_log = event.get('payload')
                        # Використовуємо json.dumps для гарного виводу словника
                        payload_str_log = json.dumps(payload_log, indent=2, default=str)
                        logger.info(payload_str_log)
                        # Позначаємо завдання як виконане
                        evt_q.task_done()
                    except queue.Empty:
                        # Якщо черга порожня, просто продовжуємо
                        continue
                    except Exception as e_listener:
                        # Логуємо помилки слухача
                        logger.error(f"Помилка event_listener: {e_listener}")
                # Лог завершення роботи слухача
                logger.debug("TestEventListener: Потік слухача подій завершує роботу.")

            # Створюємо та запускаємо потік слухача подій
            listener_thread_name = "TestEventListener"
            event_listener_thread = threading.Thread(
                target=event_listener,
                daemon=True,
                name=listener_thread_name
            )
            event_listener_thread.start()

            # Тримаємо основний потік живим, доки бот працює
            while True:
                 # Перевіряємо прапорець ядра
                 bot_is_still_running = bot_instance.is_running
                 if not bot_is_still_running:
                      logger.info("BotCore.is_running став False. Завершення основного циклу тестування.")
                      break # Виходимо з циклу очікування
                 # Пауза між перевірками
                 time.sleep(1)

        else:
            # Якщо початкова ініціалізація не вдалася
            logger.error("Не вдалося ініціалізувати основні компоненти BotCore (API або Стратегія).")
            # Зупиняємо Command Processor, якщо він запустився в __init__
            if bot_instance:
                 bot_instance._stop_command_processor()

    except KeyboardInterrupt:
        # Обробка Ctrl+C
        logger.info("Отримано KeyboardInterrupt. Ініціюємо зупинку BotCore...")
        # Надсилаємо команду STOP через чергу, якщо бот існує і працює
        if bot_instance:
             if bot_instance.is_running:
                  if bot_instance.command_queue:
                       stop_command = {'type': 'STOP'}
                       try:
                            bot_instance.command_queue.put(stop_command, timeout=1.0)
                       except queue.Full:
                            logger.error("Не вдалося надіслати команду STOP (черга переповнена).")

    except Exception as e:
        # Логуємо будь-які інші неперехоплені помилки
        logger.critical(f"Неперехоплена помилка в main тестуванні: {e}", exc_info=True)

    finally:
        # Коректна зупинка при будь-якому завершенні
        logger.info("Блок finally: Початок процедури очищення...")
        # Зупиняємо бота, якщо він був створений і (можливо) ще працює
        if 'bot_instance' in locals():
             current_bot_instance = bot_instance
             if isinstance(current_bot_instance, BotCore):
                  is_running_at_finally = current_bot_instance.is_running
                  if is_running_at_finally:
                       logger.info("Блок finally: Бот ще працює, надсилаємо команду STOP...")
                       if current_bot_instance.command_queue:
                            try:
                                 stop_cmd_final = {'type': 'STOP'}
                                 current_bot_instance.command_queue.put(stop_cmd_final, timeout=1.0)
                                 # Даємо час на обробку команди stop
                                 time.sleep(3)
                            except queue.Full:
                                 logger.error("Блок finally: Черга команд переповнена при надсиланні STOP.")
                            except Exception as e_put_final:
                                  logger.error(f"Блок finally: Помилка надсилання команди STOP: {e_put_final}")

                       # Перевіряємо стан знову після паузи
                       is_still_running_final = current_bot_instance.is_running
                       if is_still_running_final:
                            logger.warning("Блок finally: Бот все ще 'is_running' після команди STOP та паузи.")
                            # Тут не викликаємо stop() напряму, щоб уникнути потенційних проблем з потоками
                  else:
                       # Якщо бот вже не працює, просто зупиняємо Command Processor про всяк випадок
                       logger.info("Блок finally: Бот вже зупинено (is_running=False). Зупинка Command Processor (про всяк випадок)...")
                       current_bot_instance._stop_command_processor()

        # Зупиняємо слухача подій
        # Перевіряємо, чи змінна була створена
        if 'stop_event_listener_flag' in locals():
            stop_event_listener_flag.set() # Сигналізуємо потоку про зупинку
        # Чекаємо завершення потоку слухача
        if 'event_listener_thread' in locals():
             current_listener_thread = event_listener_thread
             # Перевіряємо, чи змінна існує і чи потік живий
             if current_listener_thread is not None:
                  if current_listener_thread.is_alive():
                       logger.debug("Блок finally: Очікування завершення потоку слухача подій...")
                       current_listener_thread.join(timeout=1.0) # Чекаємо недовго
                       # Перевіряємо знову
                       if current_listener_thread.is_alive():
                            logger.warning("Блок finally: Потік слухача подій не завершився вчасно.")

    logger.info("--- Тестування BotCore завершено ---")
# END BLOCK 15
