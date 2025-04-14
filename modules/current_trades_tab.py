# modules/current_trades_tab.py

# BLOCK 1: Imports and Function Definition
import streamlit as st
# --- ЗМІНА: Імпортуємо BotCore з нового місця ---
from modules.bot_core.core import BotCore
# --- КІНЕЦЬ ЗМІНИ ---
from modules.binance_integration import BinanceAPI # Для отримання ціни
from modules.logger import logger
import pandas as pd # Може знадобитися для DataFrame пізніше
from decimal import Decimal
from datetime import datetime
import time # Для time.sleep

def show_current_trades_tab(bot_core: BotCore | None, binance_api: BinanceAPI | None):
    """ Відображає вкладку з поточною позицією та активними ордерами. """
    # Заголовок вкладки
    st.header("📈 Позиція та Активні Ордери")

    # Перевіряємо наявність ядра бота
    if bot_core is None:
        st.error("Помилка: Ядро бота не ініціалізовано.")
        # Зупиняємо виконання функції, якщо ядра немає
        return
    # Перевіряємо наявність API (для отримання ціни PnL)
    if binance_api is None:
        st.warning("Binance API не ініціалізовано, отримання поточної ціни для PnL неможливе.")
        # Продовжуємо виконання, але PnL не буде розраховано
# END BLOCK 1
# BLOCK 2: Main Display Logic for Current Trades and Orders

    # Отримуємо дані з ядра бота
    bot_status = bot_core.get_status()
    # --- ВИПРАВЛЕНО: Використовуємо get_open_position ---
    position = bot_core.get_open_position()
    # --- КІНЕЦЬ ВИПРАВЛЕННЯ ---
    active_orders_dict = bot_core.get_active_orders()

    # Отримуємо символ та статус з bot_status
    symbol = bot_status.get('symbol', 'N/A')
    is_bot_running = bot_status.get('is_running', False)

    # Інформаційне повідомлення про символ
    st.info(f"Активний символ: **{symbol}**")
    # Попередження, якщо бот не запущено
    if not is_bot_running:
        st.warning("Ядро бота не запущено. Дані можуть бути неактуальними.")

    # --- Відображення Позиції ---
    st.subheader("Відкрита Позиція")
    # Перевіряємо, чи існує позиція
    if position:
        # Отримуємо дані позиції
        # Використовуємо .get() з дефолтними значеннями для безпеки
        entry_price_str = position.get('entry_price', '0')
        quantity_str = position.get('quantity', '0')
        entry_time_obj = position.get('entry_time') # Може бути datetime або str/None
        status = position.get('status', 'N/A')
        oco_id = position.get('oco_list_id', 'N/A')

        # Конвертуємо в Decimal
        entry_price = Decimal(str(entry_price_str))
        quantity = Decimal(str(quantity_str))

        # Відображаємо основні дані позиції
        col1, col2, col3 = st.columns(3)
        with col1:
             st.metric("Статус", status)
        with col2:
             # Форматуємо кількість з потрібною точністю
             qty_prec = getattr(bot_core, 'qty_precision', 8)
             qty_formatted = f"{quantity:.{qty_prec}f}"
             st.metric("Кількість", qty_formatted)
        with col3:
             # Форматуємо ціну з потрібною точністю
             price_prec = getattr(bot_core, 'price_precision', 8)
             entry_price_formatted = f"{entry_price:.{price_prec}f}"
             st.metric("Ціна Входу", entry_price_formatted)

        # --- Розрахунок та відображення PnL ---
        cp_str = "N/A"      # Поточна ціна (рядок)
        pnl_str = "N/A"     # PnL сума (рядок)
        pnl_perc_str = "N/A" # PnL відсоток (рядок)

        # Розраховуємо PnL тільки якщо є API та валідні дані позиції
        can_calculate_pnl = binance_api is not None and entry_price > 0 and quantity > 0
        if can_calculate_pnl:
            try:
                # Отримуємо поточну ціну
                cp_val = binance_api.get_current_price(symbol)
                # Перевіряємо, чи ціну отримано
                if cp_val is not None:
                    # Конвертуємо в Decimal
                    cp = Decimal(str(cp_val))
                    # Форматуємо для відображення
                    price_prec_pnl = getattr(bot_core, 'price_precision', 2) # Використовуємо 2 знаки для PnL ціни
                    cp_str = f"{cp:.{price_prec_pnl}f}"

                    # Розраховуємо вартість входу та поточну вартість
                    cost = entry_price * quantity
                    current_value = cp * quantity
                    # Розраховуємо PnL
                    pnl = current_value - cost

                    # Розраховуємо PnL у відсотках
                    pnl_perc = Decimal(0)
                    # Уникаємо ділення на нуль
                    if cost != 0:
                        pnl_perc = (pnl / cost) * Decimal(100)

                    # Форматуємо PnL для відображення
                    quote_asset_pnl = getattr(bot_core, 'quote_asset', 'QUOTE')
                    pnl_str = f"{pnl:.2f} {quote_asset_pnl}" # 2 знаки для суми PnL
                    pnl_perc_str = f"{pnl_perc:.2f}%" # 2 знаки для відсотка PnL
                else:
                    # Якщо ціну не отримано (API повернуло None)
                    cp_str = "Помилка ціни"
                    logger.warning(f"Не вдалося отримати поточну ціну для {symbol} для розрахунку PnL.")

            except Exception as e:
                # Якщо виникла помилка під час розрахунку
                logger.error(f"Помилка розрахунку PnL: {e}")
                cp_str = "Помилка PnL"
                pnl_str = "Помилка PnL"
                pnl_perc_str = "Помилка PnL"

        # Відображаємо PnL
        col_pnl1, col_pnl2, col_pnl3 = st.columns(3)
        with col_pnl1:
             st.metric("Пот. Ціна", cp_str)
        with col_pnl2:
             st.metric("Unrealized P/L", pnl_str)
        with col_pnl3:
             st.metric("Unrealized P/L(%)", pnl_perc_str)

        # Відображення часу входу та OCO ID
        entry_time_str = str(entry_time_obj) # Дефолтне значення
        if isinstance(entry_time_obj, datetime):
            # Форматуємо datetime об'єкт
            try:
                 entry_time_str = entry_time_obj.strftime('%Y-%m-%d %H:%M UTC')
            except Exception as format_e:
                 logger.warning(f"Не вдалося відформатувати час входу: {format_e}")
                 entry_time_str = str(entry_time_obj) # Залишаємо як є
        st.caption(f"Час входу: {entry_time_str} | OCO List ID: {oco_id}")

        # --- Кнопка закриття позиції ---
        # Визначаємо текст кнопки
        close_button_label = f"🚨 Закрити позицію {symbol} @ Market"
        # Визначаємо, чи кнопка активна
        close_button_disabled = not is_bot_running # Вимкнена, якщо бот не запущено

        # Створюємо кнопку
        close_pressed = st.button(
            close_button_label,
            key="manual_close",
            disabled=close_button_disabled
        )
        # Обробляємо натискання
        if close_pressed:
            # Показуємо спіннер
            with st.spinner("Надсилання команди закриття..."):
                # Формуємо команду
                close_command = {'type':'CLOSE_POSITION', 'payload':{'symbol': symbol}}
                try:
                    # Надсилаємо команду в чергу ядра
                    bot_core.command_queue.put(close_command)
                    # Невелика пауза для обробки
                    time.sleep(1.0) # Збільшено паузу
                    # Показуємо повідомлення про успіх
                    st.success(f"Команду CLOSE {symbol} надіслано.")
                    # Перезавантажуємо UI, щоб оновити стан
                    st.rerun()
                except Exception as e:
                    # Обробляємо помилку надсилання команди
                    st.error(f"Помилка надсилання команди закриття: {e}")
                    logger.error(f"Помилка надсилання команди CLOSE: {e}", exc_info=True)

    else:
        # Якщо позиції немає
        st.info("Відкритих позицій немає.")

    st.divider()
    # --- Відображення Активних Ордерів ---
    st.subheader("Активні Ордери")
    # Перевіряємо, чи є активні ордери
    if active_orders_dict:
        active_orders_list = []
        # Конвертуємо словник ордерів у список для DataFrame
        for order_key, order_details in active_orders_dict.items():
            # Створюємо копію, щоб не змінювати оригінал
            details_copy = order_details.copy()
            # Додаємо внутрішній ID (може бути clientOrderId або orderId)
            details_copy['internal_id'] = order_key
            # Конвертуємо Decimal в float для відображення в таблиці
            for k, v in details_copy.items():
                if isinstance(v, Decimal):
                    details_copy[k] = float(v)
                # Можна додати конвертацію datetime в рядок, якщо потрібно
                # elif isinstance(v, datetime):
                #     details_copy[k] = v.strftime('%Y-%m-%d %H:%M:%S')

            active_orders_list.append(details_copy)

        # Створюємо DataFrame
        df_orders = pd.DataFrame(active_orders_list)

        # Визначаємо бажаний порядок та імена колонок
        # (враховуємо можливі імена з різних джерел)
        cols_show_ordered = [
            'internal_id', 'client_order_id', 'order_id_binance',
            'symbol', 'side', 'type', 'purpose',
            'quantity_req', 'price', 'stop_price', 'status',
            'list_client_order_id', 'order_list_id_binance'
        ]
        # Залишаємо тільки ті колонки, які реально є в DataFrame
        cols_to_display = [col for col in cols_show_ordered if col in df_orders.columns]

        # Відображаємо DataFrame
        if cols_to_display:
            df_display = df_orders[cols_to_display]
            # Можна додати перейменування колонок для кращої читабельності
            # df_display = df_display.rename(columns={'quantity_req':'К-сть', ...})
            st.dataframe(
                df_display,
                use_container_width=True,
                hide_index=True, # Ховаємо індекс DataFrame
                height=200 # Обмежуємо висоту
            )
        else:
            # Якщо не вдалося відфільтрувати (малоймовірно), показуємо все
            logger.warning("Не знайдено стандартних колонок для відображення активних ордерів.")
            st.dataframe(
                df_orders,
                use_container_width=True,
                hide_index=True,
                height=200
            )
        # TODO: Додати кнопки для скасування окремих ордерів тут
    else:
        # Якщо активних ордерів немає
        st.info("Немає активних ордерів.")
# END BLOCK 2
