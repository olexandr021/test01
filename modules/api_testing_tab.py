# modules/api_testing_tab.py
import streamlit as st
from modules.binance_integration import BinanceAPI, BinanceAPIError
from modules.logger import logger # Додано імпорт logger
import time
import traceback
from decimal import Decimal # Використовуємо Decimal
import math # Для math.floor
from datetime import datetime, timezone # Для часу сервера

def show_api_testing_tab(binance_api: BinanceAPI | None):
    """Відображає вкладку для тестування API Binance."""
    st.header("📡 Тестування API Binance")

    if binance_api is None:
        st.error("Помилка: Binance API не ініціалізовано.")
        return

    if binance_api.use_testnet:
        api_mode = 'TestNet'
    else:
        api_mode = 'MainNet'
    st.info(f"Режим API: **{api_mode}**")

    # Вибір символу для тестування
    test_symbol = st.text_input("Символ для тестування", "BTCUSDT", key="api_test_symbol").upper()

    col1, col2, col3 = st.columns(3)

    with col1:
        st.subheader("Ордери")
        if st.button("Тест: Розмістити + Скасувати MARKET BUY", key="test_market_buy"):
            # --- ПОПЕРЕДЖЕННЯ ДЛЯ РЕАЛЬНОГО РЕЖИМУ ---
            confirm_real = True
            if not binance_api.use_testnet:
                st.error("🔴 УВАГА! РЕАЛЬНИЙ РЕЖИМ! 🔴")
                if 'confirm_real_market_buy' not in st.session_state:
                     st.session_state.confirm_real_market_buy = False
                if not st.session_state.confirm_real_market_buy:
                    st.session_state.confirm_real_market_buy = True
                    st.warning("**Натисніть кнопку ще раз для підтвердження РЕАЛЬНОГО ордера!**")
                    st.rerun()
                else:
                    # confirm_real залишається True
                    st.session_state.confirm_real_market_buy = False # Скидаємо для наступного разу

            if confirm_real:
                with st.spinner(f"Тестування MARKET BUY для {test_symbol}..."):
                    order_id = None
                    try:
                        # --- Розрахунок мінімальної кількості ---
                        symbol_info = binance_api.get_symbol_info(test_symbol)
                        min_qty = Decimal("0.00001") # fallback
                        step_size = Decimal("0.00001") # fallback
                        min_notional = Decimal("10.0") # fallback
                        if symbol_info and 'filters' in symbol_info:
                            for f in symbol_info['filters']:
                                if f['filterType'] == 'LOT_SIZE':
                                     min_qty = Decimal(f['minQty'])
                                     step_size = Decimal(f['stepSize'])
                                if f['filterType'] == 'MIN_NOTIONAL':
                                     min_notional = Decimal(f['minNotional'])
                        # Розраховуємо кількість на основі min_notional
                        current_price = binance_api.get_current_price(test_symbol)
                        if current_price is None:
                             raise ValueError("Не вдалося отримати ціну")
                        test_quantity = (min_notional / current_price) * Decimal("1.1") # Трохи більше min_notional
                        # Коригуємо крок
                        if step_size > 0:
                             test_quantity = (test_quantity // step_size) * step_size
                        test_quantity = max(test_quantity, min_qty) # Гарантуємо minQty
                        st.info(f"Розрахована тестова к-сть: {test_quantity}")
                        # --- Розміщення ---
                        order = binance_api.place_order(symbol=test_symbol, side='BUY', type='MARKET', quantity=float(test_quantity))
                        if order and 'orderId' in order:
                            order_id = order['orderId']
                            st.success(f"Ордер MARKET BUY розміщено: ID={order_id}, Qty={test_quantity}")
                            time.sleep(1)
                            # --- Скасування (для MARKET зазвичай неможливо, але спробуємо) ---
                            # Краще було б одразу продати, але для тесту API спробуємо cancel
                            st.info(f"Спроба скасування ордера {order_id}...")
                            cancel_res = binance_api.cancel_order(symbol=test_symbol, orderId=order_id)
                            if cancel_res:
                                st.info(f"Ордер {order_id} скасовано (або вже виконано/не існує). Відповідь: {cancel_res}")
                            else:
                                st.warning(f"Не вдалося скасувати ордер {order_id}. Ймовірно, він виконаний.")
                        else:
                             st.error(f"Помилка розміщення MARKET BUY: {order}")
                    except BinanceAPIError as api_e:
                         st.error(f"Помилка API: Code={api_e.code}, Msg={api_e.msg}")
                    except Exception as e:
                         st.error(f"Загальна помилка: {e}")
                         logger.error("API Test Error:", exc_info=True)

    with col2:
        st.subheader("Акаунт")
        if st.button("Тест: Інфо Акаунту", key="test_account_info"):
            with st.spinner("Отримання інфо акаунту..."):
                try:
                    info = binance_api.get_account_info()
                    if info:
                         st.success("Інфо акаунту отримано.")
                         st.json(info)
                    else:
                         st.error("Не вдалося отримати інфо.")
                except BinanceAPIError as api_e:
                     st.error(f"Помилка API: Code={api_e.code}, Msg={api_e.msg}")
                except Exception as e:
                     st.error(f"Помилка: {e}")

        if st.button("Тест: Час Сервера", key="test_server_time"):
            with st.spinner("Отримання часу..."):
                t = binance_api.get_server_time()
                if t:
                     dt = datetime.fromtimestamp(t/1000, tz=timezone.utc)
                     st.success(f"Час сервера: {dt.strftime('%Y-%m-%d %H:%M:%S %Z')}")
                else:
                     st.error("Не отримано час.")

    with col3:
        st.subheader("Ринкові Дані")
        if st.button(f"Тест: Ціна {test_symbol}", key="test_get_price"):
            with st.spinner(f"Отримання ціни {test_symbol}..."):
                price = binance_api.get_current_price(test_symbol)
                if price:
                     st.success(f"Поточна ціна {test_symbol}: {price}")
                else:
                     st.error(f"Не вдалося отримати ціну {test_symbol}.")

        if st.button(f"Тест: Klines {test_symbol} (1h, 5)", key="test_get_klines"):
            with st.spinner(f"Отримання klines {test_symbol}..."):
                try:
                    klines = binance_api.get_historical_klines(symbol=test_symbol, interval='1h', limit=5)
                    if klines:
                         st.success(f"Отримано {len(klines)} klines.")
                         st.json(klines)
                    else:
                         st.error("Не вдалося отримати klines.")
                except BinanceAPIError as api_e:
                     st.error(f"Помилка API: Code={api_e.code}, Msg={api_e.msg}")
                except Exception as e:
                     st.error(f"Помилка: {e}")
