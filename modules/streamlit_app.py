# modules/streamlit_app.py
# Головний файл Streamlit додатка (з виправленим порядком імпорту logger)

# BLOCK 1: Initial Imports and Environment Loading
import os
import threading
import time
import queue
import pandas as pd
from datetime import datetime, timezone, timedelta
import inspect
import traceback
import json

# Імпортуємо Streamlit
import streamlit as st

# Імпортуємо dotenv і завантажуємо .env ЯКНАЙРАНІШЕ
from dotenv import load_dotenv
load_dotenv()
# Прибираємо лог звідси, бо logger ще не імпортовано

# Імпортуємо наші модулі ПІСЛЯ load_dotenv
from modules.config_manager import ConfigManager
from modules.binance_integration import BinanceAPI, BinanceAPIError
from modules.backtesting import Backtester
# Імпортуємо логер
from modules.logger import logger
# --- ВИПРАВЛЕНО: Логуємо завантаження .env ПІСЛЯ імпорту logger ---
logger.info("streamlit_app: Спроба завантаження .env виконана.")
# --- КІНЕЦЬ ВИПРАВЛЕННЯ ---
# Імпортуємо BotCore та помилку з нової структури
from modules.bot_core.core import BotCore, BotCoreError
from modules.telegram_integration import TelegramBotHandler

# Імпортуємо модулі вкладок
from modules import (
    overview_tab,
    current_trades_tab,
    settings_tab,
    testing_tab,
    predictions_tab,
    history_tab,
    strategies_tab,
    api_testing_tab
)
# Імпортуємо функцію завантаження стратегій
from modules.strategies_tab import load_strategies
# END BLOCK 1
# BLOCK 2: Component Initialization Functions (@st.cache_resource)

@st.cache_resource(show_spinner="Завантаження конфігурації...")
def init_config_manager():
    """Ініціалізує або повертає кешований ConfigManager."""
    logger.info("Ініціалізація ConfigManager (cached)")
    # Створюємо екземпляр менеджера конфігурації
    config_manager_instance = ConfigManager()
    return config_manager_instance

@st.cache_resource(show_spinner="Ініціалізація Binance API...")
def init_binance_api(use_testnet: bool):
    """
    Ініціалізує або повертає кешований BinanceAPI для вказаного режиму.
    Обробляє помилки і повертає None у разі невдачі.
    """
    mode_name = 'TestNet' if use_testnet else 'MainNet'
    logger.info(f"Створення/отримання BinanceAPI ({mode_name}) (cached)")
    try:
        # Визначаємо ключі API на основі режиму
        api_key = None
        api_secret = None
        if use_testnet:
            api_key = os.environ.get('BINANCE_TESTNET_API_KEY')
            api_secret = os.environ.get('BINANCE_TESTNET_SECRET_KEY')
        else:
            api_key = os.environ.get('BINANCE_API_KEY')
            api_secret = os.environ.get('BINANCE_API_SECRET')

        # Перевіряємо наявність ключів
        keys_found = api_key and api_secret
        if not keys_found:
            # Якщо ключів немає, генеруємо помилку
            error_msg = f"API Ключі не знайдено для {mode_name} в .env"
            # Використовуємо BotCoreError, як очікується в init_backtester
            keys_error = BotCoreError(error_msg)
            raise keys_error

        # Створюємо екземпляр API
        api = BinanceAPI(api_key=api_key, api_secret=api_secret, use_testnet=use_testnet)
        # Перевіряємо з'єднання
        server_time = api.get_server_time()
        if not server_time:
            # Якщо не вдалося підключитися
            connect_msg = f"Не вдалося підключитися до Binance API ({mode_name})."
            connect_error = ConnectionError(connect_msg)
            raise connect_error

        # Якщо все добре
        logger.info(f"BinanceAPI ({mode_name}) OK.")
        return api

    # Обробка будь-яких винятків під час ініціалізації API
    except (BotCoreError, ConnectionError, Exception) as e:
        # Логуємо критичну помилку
        logger.critical(f"Помилка ініціалізації Binance API ({mode_name}): {e}", exc_info=True)
        # Намагаємось показати помилку в UI (може не спрацювати, якщо кеш викликається поза контекстом)
        try:
            st.error(f"Помилка ініціалізації Binance API ({mode_name}): {e}")
        except Exception as st_err:
            logger.warning(f"Не вдалося показати помилку API в Streamlit: {st_err}")
            pass # Ігноруємо помилку Streamlit
        # Повертаємо None, сигналізуючи про невдалу ініціалізацію
        return None

@st.cache_resource(show_spinner="Ініціалізація Backtester...")
def init_backtester():
    """Ініціалізує або повертає кешований Backtester."""
    logger.info("Створення/отримання Backtester (cached)")
    # Backtester завжди використовує MainNet API для історичних даних
    # Викликаємо функцію ініціалізації API для MainNet
    api_mainnet = init_binance_api(use_testnet=False)

    # Перевіряємо результат ініціалізації MainNet API
    if not api_mainnet:
        # Якщо не вдалося, показуємо попередження в UI та логах
        warning_msg = "Не вдалося ініціалізувати MainNet API для Backtester (перевірте ключі в .env). Функціонал завантаження даних може бути обмежений."
        try:
             st.warning(warning_msg)
        except Exception as st_err:
             logger.warning(f"Не вдалося показати попередження Backtester в Streamlit: {st_err}")
             pass
        logger.warning("Не вдалося ініціалізувати MainNet API для Backtester.")
        # Backtester може працювати і без API (наприклад, з кешованими даними),
        # тому ми все одно створюємо екземпляр, передаючи None

    # Створюємо екземпляр Backtester, передаючи результат ініціалізації API
    backtester_instance = Backtester(api_mainnet)
    return backtester_instance


@st.cache_resource(show_spinner="Ініціалізація ядра бота...")
def init_bot_core(_config_manager: ConfigManager):
    """Ініціалізує або повертає кешований BotCore."""
    logger.info("Створення/отримання BotCore (cached)")
    core_instance = None # Для обробки винятків
    try:
        # Створюємо черги для взаємодії
        cmd_queue = queue.Queue()
        evt_queue = queue.Queue()
        # Створюємо екземпляр BotCore, передаючи залежності
        core_instance = BotCore(
            config_manager=_config_manager,
            command_queue=cmd_queue,
            event_queue=evt_queue
        )
        logger.info("BotCore ініціалізовано успішно (у функції init_bot_core).")
        # Повертаємо створений екземпляр
        return core_instance
    except Exception as e:
        # Якщо виникла помилка під час ініціалізації BotCore
        logger.critical(f"Не вдалося ініц. BotCore: {e}", exc_info=True)
        # Намагаємось показати помилку в UI
        try:
            error_msg = f"Критична помилка ініціалізації ядра бота: {e}. Додаток не може продовжити роботу."
            st.error(error_msg)
        except Exception as st_err:
             logger.warning(f"Не вдалося показати помилку BotCore в Streamlit: {st_err}")
             pass
        # Повертаємо None, сигналізуючи про невдачу
        return None

@st.cache_resource(show_spinner="Ініціалізація Telegram бота...")
def init_telegram_bot(_command_queue: queue.Queue, _event_queue: queue.Queue):
    """Ініціалізує або повертає кешований TelegramBotHandler."""
    logger.info("Створення/отримання TelegramBotHandler (cached)")
    try:
        # Створюємо екземпляр обробника Telegram, передаючи черги
        tg_handler_instance = TelegramBotHandler(
            command_queue=_command_queue,
            event_queue=_event_queue
        )
        # Логуємо статус (увімкнено/вимкнено)
        if tg_handler_instance.is_enabled:
            logger.info("TelegramBotHandler ініціалізовано та увімкнено.")
        else:
            logger.info("TelegramBotHandler ініціалізовано, але вимкнено (в конфігурації).")
        # Повертаємо екземпляр
        return tg_handler_instance
    except Exception as e:
        # Якщо помилка під час ініціалізації Telegram
        logger.error(f"Помилка ініціалізації Telegram: {e}", exc_info=True)
        # Намагаємось показати попередження в UI
        try:
             warning_msg = f"Не вдалося ініціалізувати Telegram бота: {e}"
             st.warning(warning_msg)
        except Exception as st_err:
             logger.warning(f"Не вдалося показати попередження Telegram в Streamlit: {st_err}")
             pass
        # Повертаємо None у разі помилки
        return None
# END BLOCK 2
# BLOCK 3: UI Session State Initialization
def initialize_session_state():
    """Встановлює значення за замовчуванням у st.session_state."""
    # Словник зі значеннями за замовчуванням для стану сесії UI
    default_session_values = {
        'backtest_results': None,      # Для зберігання результатів останнього бектесту
        'confirm_real_start': False,   # Прапорець для підтвердження запуску в реальному режимі
        # Можна додати інші ключі стану UI за потреби
    }

    # Проходимо по словнику і встановлюємо значення, якщо ключ відсутній
    for key, default_value in default_session_values.items():
        # Перевіряємо, чи ключ вже існує в session_state
        key_exists = key in st.session_state
        if not key_exists:
            # Якщо ключ відсутній, додаємо його зі значенням за замовчуванням
            st.session_state[key] = default_value
            logger.debug(f"Встановлено session_state['{key}'] = {default_value}")
# END BLOCK 3
# BLOCK 4: Main Streamlit Application Logic
def main():
    """Головна функція, що запускає Streamlit додаток."""
    # Налаштування сторінки (широкий макет, заголовок)
    st.set_page_config(layout="wide", page_title="Торговий бот Gemini")
    # Головний заголовок додатка
    st.title("💎 Торговий бот Gemini")

    # --- Ініціалізація основних компонентів ---
    # Викликаємо кешовані функції ініціалізації
    config_manager = init_config_manager()
    # Перевіряємо результат ініціалізації ConfigManager
    if config_manager is None:
        st.error("Критична помилка: Не вдалося завантажити конфігурацію. Перевірте файл config.json.")
        # Зупиняємо виконання, якщо конфіг не завантажено
        return

    # Ініціалізуємо стан сесії UI
    initialize_session_state()
    # Завантажуємо доступні стратегії
    available_strategies = load_strategies()

    # Ініціалізуємо ядро бота
    bot_core = init_bot_core(config_manager)
    # Перевіряємо результат ініціалізації BotCore
    if bot_core is None:
        # Помилка вже мала бути показана в init_bot_core
        logger.critical("Ініціалізація ядра бота не вдалася. Робота додатку зупинена.")
        # Додатково показуємо повідомлення тут (хоча воно дублює те, що в init_bot_core)
        st.error("Ініціалізація ядра бота не вдалася. Перевірте логи для деталей.")
        # Зупиняємо виконання, якщо ядро не ініціалізовано
        return

    # Ініціалізуємо API для UI (відповідно до режиму ядра)
    binance_api_ui = None
    # Перевіряємо наявність атрибута перед доступом
    bot_core_has_use_testnet = hasattr(bot_core, 'use_testnet')
    if bot_core_has_use_testnet:
         # Отримуємо режим роботи ядра
         core_use_testnet = bot_core.use_testnet
         # Ініціалізуємо API для UI
         binance_api_ui = init_binance_api(core_use_testnet)
    else:
         # Якщо атрибута немає (малоймовірно після успішної ініціалізації)
         logger.error("Атрибут 'use_testnet' не знайдено в bot_core після ініціалізації.")
         # Пробуємо отримати режим з конфігу як запасний варіант
         try:
              bot_config_fallback = config_manager.get_bot_config()
              active_mode_fallback = bot_config_fallback.get('active_mode', 'TestNet')
              use_testnet_fallback = (active_mode_fallback == 'TestNet')
              binance_api_ui = init_binance_api(use_testnet_fallback)
              logger.warning("Використано режим з конфігурації для UI API як запасний варіант.")
         except Exception as config_err:
              logger.error(f"Не вдалося отримати режим з конфігурації: {config_err}")
              st.error("Не вдалося визначити режим роботи для UI API.")
    # Перевіряємо, чи вдалося ініціалізувати UI API
    if binance_api_ui is None:
         st.warning("Не вдалося ініціалізувати API для взаємодії з UI (Тест API, Позиція). Перевірте API ключі.")


    # Ініціалізуємо Backtester
    backtester = init_backtester()
    # Перевіряємо результат (Backtester може працювати без API)
    if backtester is None:
         st.warning("Backtester не вдалося ініціалізувати.")
         logger.warning("Backtester не ініціалізовано.")

    # Ініціалізуємо Telegram Handler
    telegram_bot = None
    # Перевіряємо наявність черг в ядрі
    core_has_cmd_queue = hasattr(bot_core, 'command_queue')
    core_has_evt_queue = hasattr(bot_core, 'event_queue')
    if core_has_cmd_queue and core_has_evt_queue:
        # Передаємо черги в ініціалізатор
        telegram_bot = init_telegram_bot(bot_core.command_queue, bot_core.event_queue)
    else:
        # Якщо черг немає (малоймовірно)
        logger.error("Черги command_queue або event_queue не знайдено в bot_core.")
        st.warning("Не вдалося ініціалізувати Telegram бота через відсутність черг в ядрі.")

    # --- Бічна панель (Sidebar) ---
    with st.sidebar:
        st.header("⚙️ Керування")

        # Отримуємо актуальний статус ядра (з перевіркою)
        bot_status = {}
        is_bot_running = False
        core_active_mode = 'N/A'
        if bot_core:
             try:
                  bot_status = bot_core.get_status()
                  is_bot_running = bot_status.get('is_running', False)
                  core_active_mode = bot_status.get('active_mode', 'N/A')
             except Exception as status_err:
                  logger.error(f"Помилка отримання статусу bot_core: {status_err}")
                  st.warning("Не вдалося отримати актуальний статус ядра бота.")

        # Відображення режиму роботи
        st.subheader("Режим Роботи Ядра")
        st.info(f"Поточний режим: **{core_active_mode}**")
        st.caption("Змінити режим можна у вкладці 'Налаштування'. Потребує перезапуску ядра.")

        # Попередження про реальний режим
        is_real_mode = (core_active_mode == 'Реальний')
        if is_real_mode:
            st.error("🔴 УВАГА! АКТИВОВАНО РЕАЛЬНИЙ РЕЖИМ! 🔴")
            st.warning("Торгівля ведеться з **реальними коштами**!")
        st.divider()

        # --- Керування ядром бота ---
        st.subheader("🚀 Керування Ядром")
        # Відображення статусу ядра
        if is_bot_running:
            st.success("🟢 Ядро **ЗАПУЩЕНО**")
            current_symbol_status = bot_status.get('symbol', '?')
            current_strategy_status = bot_status.get('strategy', '?')
            st.caption(f"Символ: {current_symbol_status} | Стратегія: {current_strategy_status}")
        else:
            st.error("🔴 Ядро **ЗУПИНЕНО**")

        # Кнопки Запуск/Зупинка
        col_start, col_stop = st.columns(2)
        with col_start:
            # Визначаємо, чи активна кнопка запуску
            start_button_disabled = True # За замовчуванням вимкнена
            if bot_core:
                # Перевіряємо, чи бот вже запущено, чи є API та стратегія
                is_api_ready_start = bot_core.binance_api is not None
                is_strategy_ready_start = bot_core.strategy is not None
                start_button_disabled = is_bot_running or not is_api_ready_start or not is_strategy_ready_start

            # Створюємо кнопку запуску
            start_pressed = st.button(
                "🟢 Запустити ядро",
                key="start_core",
                disabled=start_button_disabled,
                use_container_width=True
            )
            # Обробляємо натискання кнопки запуску
            if start_pressed:
                 if bot_core: # Додаткова перевірка
                      start_confirmed = True # Прапорець підтвердження
                      # Логіка підтвердження для реального режиму
                      if core_active_mode == 'Реальний':
                           # Використовуємо st.session_state для збереження стану підтвердження
                           needs_confirmation = not st.session_state.confirm_real_start
                           if needs_confirmation:
                                # Якщо потрібне перше підтвердження
                                st.session_state.confirm_real_start = True
                                st.warning("**Натисніть 'Запустити ядро' ще раз для підтвердження РЕАЛЬНОЇ торгівлі!**")
                                st.rerun() # Перезапускаємо скрипт для оновлення UI
                           else:
                                # Якщо друге натискання (підтвердження отримано)
                                st.session_state.confirm_real_start = False # Скидаємо прапорець для наступного разу
                                start_confirmed = True # Підтвердження отримано
                      # Якщо підтвердження не потрібне (TestNet) або отримане (RealNet)
                      should_proceed_start = start_confirmed and (core_active_mode != 'Реальний' or not st.session_state.confirm_real_start)
                      if should_proceed_start:
                           # Перевіряємо готовність компонентів перед надсиланням команди
                           is_api_ready_final = bot_core.binance_api is not None
                           is_strategy_ready_final = bot_core.strategy is not None
                           if not is_api_ready_final:
                                st.error("Помилка API: Не вдалося ініціалізувати API.")
                           elif not is_strategy_ready_final:
                                st.error("Стратегія не завантажена або не обрана.")
                           else:
                                # Якщо все готово, надсилаємо команду START
                                with st.spinner("Запуск ядра бота..."):
                                     start_command = {'type': 'START'}
                                     bot_core.command_queue.put(start_command)
                                     # Даємо час BotCore відреагувати та оновити статус
                                     time.sleep(1.5) # Можна підібрати оптимальний час
                                     st.success("Команду запуску надіслано.")
                                     # Оновлюємо інтерфейс, щоб побачити зміну статусу
                                     st.rerun()

        with col_stop:
            # Створюємо кнопку зупинки
            stop_pressed = st.button(
                "🛑 Зупинити ядро",
                key="stop_core",
                disabled=not is_bot_running, # Активна тільки якщо бот запущено
                use_container_width=True
            )
            # Обробляємо натискання кнопки зупинки
            if stop_pressed:
                if bot_core: # Додаткова перевірка
                    with st.spinner("Зупинка ядра бота..."):
                        # Надсилаємо команду STOP
                        stop_command = {'type': 'STOP'}
                        bot_core.command_queue.put(stop_command)
                        # Даємо час на обробку
                        time.sleep(1.0)
                        st.success("Команду зупинки надіслано.")
                        # Оновлюємо інтерфейс
                        st.rerun()

        # --- Статус Telegram ---
        st.divider()
        st.subheader("💬 Telegram Бот")
        # Перевіряємо стан Telegram обробника
        if telegram_bot:
            # Перевіряємо, чи увімкнено в конфігурації
            if telegram_bot.is_enabled:
                # Отримуємо посилання на потік
                tg_thread = getattr(telegram_bot, 'polling_thread', None)
                # Перевіряємо, чи створено об'єкт бота і чи живий потік
                bot_object_exists = telegram_bot.bot is not None
                thread_is_alive = tg_thread is not None and tg_thread.is_alive()

                if bot_object_exists and thread_is_alive:
                    # Якщо все добре
                    st.success("🟢 Telegram бот активний.")
                elif bot_object_exists:
                    # Якщо об'єкт є, але потік не живий
                    st.warning("🟠 Потік Telegram неактивний (перевірте логи).")
                    thread_status = 'не існує' if not tg_thread else 'не активний'
                    logger.warning(f"Telegram бот увімкнено, але потік polling_thread {thread_status}.")
                else:
                    # Якщо об'єкт бота не створено (проблема з токеном/ID)
                    st.error("🔴 Не вдалося запустити Telegram (перевірте токен/chat_id).")
                    logger.error("Telegram бот увімкнено, але об'єкт telegram_bot.bot не створено.")
            else:
                # Якщо вимкнено в налаштуваннях
                st.info("⚪ Telegram бот вимкнено (в налаштуваннях).")
        else:
            # Якщо сам обробник не ініціалізувався
            st.warning("🟡 Telegram бот не був ініціалізований (можливо, помилка під час запуску).")


    # --- Основний контент (Вкладки) ---
    # Визначаємо список назв вкладок
    tab_list = [
        "📊 Огляд",
        "📈 Позиція",
        "⚙️ Налаштування",
        "🔬 Тестування",
        "🔮 Прогнози",
        "💾 Історія",
        "🧭 Стратегії",
        "📡 Тест API"
    ]
    # Створюємо вкладки
    tabs = st.tabs(tab_list)

    # Наповнюємо кожну вкладку, передаючи необхідні компоненти
    # Використовуємо 'with' для кожної вкладки
    with tabs[0]: # Огляд
        if bot_core:
            overview_tab.show_overview_tab(bot_core)
        else:
            st.error("Ядро бота недоступне.")
    with tabs[1]: # Позиція
        if bot_core and binance_api_ui:
            current_trades_tab.show_current_trades_tab(bot_core, binance_api_ui)
        else:
            st.error("Ядро бота або UI API недоступне.")
    with tabs[2]: # Налаштування
        if bot_core and config_manager:
            # Передаємо st.session_state для збереження стану UI між перезапусками
            settings_tab.show_settings_tab(st.session_state, config_manager, available_strategies, bot_core)
        else:
            st.error("Ядро бота або менеджер конфігурації недоступні.")
    with tabs[3]: # Тестування
        if backtester and config_manager:
            # Передаємо st.session_state для збереження результатів бектесту
            testing_tab.show_testing_tab(st.session_state, available_strategies, backtester, config_manager)
        else:
            st.error("Бектестер або менеджер конфігурації недоступні.")
    with tabs[4]: # Прогнози
        # Передаємо статус з перевіркою на None
        status_payload_pred = None
        if bot_core:
             status_payload_pred = bot_core.get_status()
        predictions_tab.show_predictions_tab(status_payload_pred)
    with tabs[5]: # Історія
        if bot_core:
            history_tab.show_history_tab(bot_core)
        else:
            st.error("Ядро бота недоступне.")
    with tabs[6]: # Стратегії
        if config_manager:
            strategies_tab.show_strategies_tab(available_strategies, config_manager)
        else:
            st.error("Менеджер конфігурації недоступний.")
    with tabs[7]: # Тест API
        if binance_api_ui:
            api_testing_tab.show_api_testing_tab(binance_api_ui) # Передаємо UI API
        else:
            st.error("UI API недоступне.")
# END BLOCK 4
# BLOCK 5: Script Execution Entry Point
if __name__ == "__main__":
    # Цей блок виконується, коли скрипт запускається напряму
    # (наприклад, командою `python -m streamlit run modules/streamlit_app.py`)

    # --- Перевірка та Завантаження .env ---
    # Перевіряємо, чи існують ключові змінні середовища API
    # (проста перевірка наявності одного з ключів)
    env_keys_exist = False
    if "BINANCE_API_KEY" in os.environ:
        env_keys_exist = True
    if "BINANCE_TESTNET_API_KEY" in os.environ:
        env_keys_exist = True

    # Якщо ключі не знайдено в середовищі
    if not env_keys_exist:
        logger.info("Змінні середовища API ключів не знайдено. Спроба завантажити з .env...")
        # Намагаємось завантажити файл .env
        # load_dotenv() повертає True, якщо файл знайдено і завантажено
        loaded_ok = load_dotenv()
        if loaded_ok:
            logger.info(".env файл успішно завантажено.")
        else:
            # Якщо .env не знайдено
            logger.warning(".env файл не знайдено. API ключі мають бути встановлені як змінні середовища для коректної роботи.")
    else:
        # Якщо ключі вже існують у середовищі
        logger.info("Змінні середовища API ключів вже існують.")

    # --- Запуск Головної Функції ---
    logger.info("Запуск Streamlit додатку (main function)...")
    # Викликаємо головну функцію для побудови та запуску UI
    main()
# END BLOCK 5
