# modules/overview_tab.py

# BLOCK 1: Imports and Header
import streamlit as st
# --- ЗМІНА: Використовуємо новий шлях для імпорту ---
from modules.bot_core.core import BotCore
# --- КІНЕЦЬ ЗМІНИ ---
from modules.binance_integration import BinanceAPI # Може бути None, якщо API не ініціалізовано
from modules.logger import logger # Залишаємо імпорт, хоч зараз і не використовується
import pandas as pd
from decimal import Decimal # Потрібен для роботи з даними позиції

# Приймаємо bot_core як аргумент
def show_overview_tab(bot_core: BotCore | None):
    """Відображає вкладку 'Огляд' з основною інформацією про стан бота."""
    st.header("📊 Огляд")

    if bot_core is None:
        st.error("Помилка: Ядро бота не ініціалізовано.")
        return # Виходимо, якщо ядра немає
# END BLOCK 1
# BLOCK 2: Main Display Logic
    # Отримуємо актуальний статус з BotCore
    bot_status = bot_core.get_status()
    # Витягуємо окремі значення статусу
    is_running = bot_status.get('is_running', False)
    active_mode = bot_status.get('active_mode', 'N/A')
    symbol = bot_status.get('symbol', 'N/A')
    strategy = bot_status.get('strategy', 'Не обрана')
    ws_connected = bot_status.get('websocket_connected', False)
    position_active = bot_status.get('position_active', False)

    # Відображення загального статусу в колонках
    col1, col2, col3 = st.columns(3)
    with col1:
        # Стан запуску
        if is_running:
            st.success("🟢 Бот **ЗАПУЩЕНО**")
        else:
            st.error("🔴 Бот **ЗУПИНЕНО**")
        # Режим роботи
        st.metric("Режим роботи", active_mode)
    with col2:
        # Символ
        st.metric("Торговий символ", symbol)
        # Стратегія
        if strategy:
            st.metric("Обрана стратегія", strategy)
        else:
            st.metric("Обрана стратегія", "Не обрана")
    with col3:
        # Стан WebSocket
        if ws_connected:
            st.success("🟢 WebSocket підкл.")
        else:
            st.error("🔴 WebSocket відкл.")
        # Наявність позиції
        position_status_str = "Так" if position_active else "Ні"
        st.metric("Активна позиція?", position_status_str)

    st.divider()
    # --- Відображення Балансу ---
    st.subheader("💰 Баланс акаунту")

    # Отримуємо баланс з BotCore
    balances = bot_core.get_balance()
    # Перевіряємо, чи отримано баланси
    if balances:
        st.success(f"Баланси ({active_mode}) отримано.")
        balance_list = []
        # Конвертуємо словник балансів у список для DataFrame
        for asset, data in balances.items():
            asset_data = {
                'asset': asset,
                'free': data.get('free', Decimal(0)), # Безпечний доступ
                'locked': data.get('locked', Decimal(0)) # Безпечний доступ
            }
            balance_list.append(asset_data)

        # Створюємо DataFrame
        df_balances = pd.DataFrame(balance_list)
        # Конвертація Decimal в float та розрахунок total
        df_balances['free'] = df_balances['free'].astype(float).round(8)
        df_balances['locked'] = df_balances['locked'].astype(float).round(8)
        df_balances['total'] = df_balances['free'] + df_balances['locked']
        # Фільтруємо нульові баланси та сортуємо
        min_display_balance = 1e-9
        df_balances_nonzero = df_balances[df_balances['total'] > min_display_balance]
        df_balances_sorted = df_balances_nonzero.sort_values(by='total', ascending=False)

        # Відображаємо таблицю балансів
        if not df_balances_sorted.empty:
            columns_to_display = ['asset', 'free', 'locked', 'total']
            df_display_balances = df_balances_sorted[columns_to_display].reset_index(drop=True)
            st.dataframe(
                df_display_balances,
                use_container_width=True,
                height=250 # Встановлюємо висоту таблиці
            )
            # Відображаємо баланс котирувального активу окремо
            quote_asset = getattr(bot_core, 'quote_asset', 'USDT') # Безпечний доступ до атрибуту
            usdt_balance_row = df_balances_sorted[df_balances_sorted['asset'] == quote_asset]
            if not usdt_balance_row.empty:
                # Отримуємо значення з рядка
                free_usdt = usdt_balance_row['free'].iloc[0]
                locked_usdt = usdt_balance_row['locked'].iloc[0]
                # Форматуємо для відображення
                free_usdt_str = f"{free_usdt:.2f}"
                locked_usdt_str = f"{locked_usdt:.2f}"
                # Відображаємо метрику
                st.metric(f"Баланс {quote_asset}", free_usdt_str, delta=f"Заблок: {locked_usdt_str}")
            else:
                # Якщо котирувального активу немає в списку
                 st.metric(f"Баланс {quote_asset}", "0.00")
        else:
            # Якщо немає ненульових балансів
            st.info("Не знайдено активів з позитивним балансом.")

    # Обробка випадків, коли баланс ще не завантажено
    elif bot_core.is_running: # Використовуємо змінну is_running, отриману раніше
        st.warning("Баланси ще не завантажено або порожні...")
    else: # Якщо бот не запущено
        st.info("Бот зупинено. Запустіть ядро для перегляду актуальних балансів.")

    st.divider()
    # --- Відображення Деталей Позиції ---
    st.subheader("ℹ️ Деталі Позиції")
    # --- ВИПРАВЛЕНО: Використовуємо get_open_position ---
    position_details = bot_core.get_open_position()
    # --- КІНЕЦЬ ВИПРАВЛЕННЯ ---

    # Перевіряємо, чи є деталі позиції
    if position_details:
        # Використовуємо внутрішній метод BotCore для конвертації (як було раніше)
        # АБО краще реалізувати конвертацію тут чи в StateManager
        # Поки залишаємо виклик внутрішнього методу, хоча це не ідеально
        position_details_str_dict = None
        try:
             # Перевіряємо, чи метод існує перед викликом
             if hasattr(bot_core, '_decimal_dict_to_str'):
                  position_details_str_dict = bot_core._decimal_dict_to_str(position_details)
             else:
                  # Якщо методу немає, спробуємо стандартний json.dumps
                  logger.warning("Метод _decimal_dict_to_str не знайдено в BotCore, спроба стандартної серіалізації.")
                  position_details_str_dict = position_details # Передаємо як є
        except Exception as conv_err:
             logger.error(f"Помилка конвертації деталей позиції: {conv_err}")
             position_details_str_dict = {"error": f"Помилка конвертації: {conv_err}"}

        # Відображаємо як JSON
        if position_details_str_dict is not None:
             st.json(position_details_str_dict)
        else:
             # Якщо конвертація не вдалась
             st.warning("Не вдалося сконвертувати деталі позиції для відображення.")
             # Спробуємо відобразити як текст
             st.text(str(position_details))
    else:
        # Якщо позиції немає
        st.info("Відкритих позицій немає.")
# END BLOCK 2
