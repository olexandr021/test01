# modules/testing_tab.py
import streamlit as st
# ----- ЗМІНЕНО ТУТ: Додано timezone -----
from datetime import datetime, date, timedelta, timezone
# ----- КІНЕЦЬ ЗМІН -----
from modules.backtesting import Backtester
import pandas as pd
from modules.logger import logger
import inspect
from modules.config_manager import ConfigManager
from decimal import Decimal # Для параметрів
import math # Для порівняння float
import json # Для st.json

# Імпортуємо BaseStrategy для отримання дефолтних параметрів
try:
     from strategies.base_strategy import BaseStrategy
except ImportError:
     logger.error("BT Tab: Не імпортовано BaseStrategy.")
     # Створюємо заглушку, якщо BaseStrategy не знайдено
     class BaseStrategy:
         @staticmethod
         def get_default_params_with_types():
             return {} # Повертаємо порожній словник, якщо клас не знайдено

def show_testing_tab(session_state, available_strategies: dict, backtester: Backtester, config_manager: ConfigManager):
    """Відображає вкладку для бектестування стратегій."""
    st.header("🔬 Тестування стратегій (Backtesting)")

    if not available_strategies:
        st.warning("Не знайдено доступних стратегій у папці 'strategies'.")
        return
    if not backtester:
        st.error("Помилка: Backtester не ініціалізовано. Перевірте налаштування API.")
        # Можливо, варто додати кнопку для спроби переініціалізації або посилання на налаштування
        return
    if not config_manager:
        st.error("Помилка: ConfigManager не ініціалізовано.")
        return

    # --- Вибір параметрів бектесту ---
    col1, col2 = st.columns(2)
    with col1:
        strategy_names = list(available_strategies.keys())
        # Перевірка чи список не порожній перед викликом selectbox
        if not strategy_names:
             st.error("Список доступних стратегій порожній.")
             return # Немає сенсу продовжувати без стратегій
        selected_test_strategy_name = st.selectbox("Стратегія", strategy_names, key="backtest_strategy_select")
        test_symbol = st.text_input("Символ", "BTCUSDT", key="backtest_symbol").upper()
        # Індекс 5 ('1h') може бути недійсним, якщо список змінено; перевіряємо
        intervals = ['1m', '3m', '5m', '15m', '30m', '1h', '2h', '4h', '6h', '8h', '12h', '1d', '3d', '1w']
        default_interval_index = 5 if len(intervals) > 5 else 0
        test_interval = st.selectbox("Таймфрейм", intervals, index=default_interval_index, key="backtest_interval")

    with col2:
        initial_capital = st.number_input("Капітал (USDT)", min_value=1.0, value=10000.0, step=100.0, key="backtest_capital")
        position_size_pct = st.number_input("Позиція (% Кап.)", min_value=0.1, max_value=100.0, value=10.0, step=0.1, format="%.1f", key="backtest_pos_size")
        commission_pct = st.number_input("Комісія (%)", min_value=0.0, max_value=5.0, value=0.075, step=0.001, format="%.3f", key="backtest_commission", help="Відсоток комісії за одну операцію (вхід або вихід)")
        tp_pct = st.number_input("Take Profit (%)", min_value=0.0, value=1.0, step=0.01, format="%.2f", key="backtest_tp", help="0.0 - вимкнено")
        sl_pct = st.number_input("Stop Loss (%)", min_value=0.0, value=0.5, step=0.01, format="%.2f", key="backtest_sl", help="0.0 - вимкнено")

    # --- Параметри обраної стратегії ---
    st.subheader(f"Параметри для '{selected_test_strategy_name}' (для цього бектесту)")
    strategy_params_for_test = {}
    # Перевіряємо чи обрана стратегія існує в словнику
    if selected_test_strategy_name and selected_test_strategy_name in available_strategies:
        strategy_class = available_strategies[selected_test_strategy_name]
        saved_config = config_manager.get_strategy_config(selected_test_strategy_name)
        # --- Використовуємо покращену логіку отримання параметрів та типів ---
        default_params_info = {}
        # Перевіряємо чи клас стратегії існує і має потрібний метод
        if strategy_class and hasattr(strategy_class, 'get_default_params_with_types'):
             try:
                 default_params_info = strategy_class.get_default_params_with_types()
             except Exception as e:
                 logger.error(f"Помилка при отриманні параметрів для {selected_test_strategy_name}: {e}")
                 st.warning(f"Не вдалося отримати параметри для {selected_test_strategy_name}.")

        if not default_params_info:
            st.caption("Немає параметрів для налаштування у цій стратегії.")
        else:
            num_columns = len(default_params_info) if len(default_params_info) <= 4 else 4
            cols_params = st.columns(num_columns)
            col_idx = 0
            for name, info in default_params_info.items():
                # Перевіряємо чи info є словником
                if not isinstance(info, dict):
                    logger.warning(f"Некоректний формат параметру '{name}' для стратегії '{selected_test_strategy_name}'. Очікувався словник.")
                    continue # Пропускаємо цей параметр

                with cols_params[col_idx % num_columns]:
                    param_type = info.get('type', str)
                    default_value = info.get('default')
                    # Отримуємо значення з конфігурації, якщо воно є, інакше беремо дефолтне
                    # Потрібно конвертувати значення з конфігурації до потрібного типу!
                    saved_raw_value = saved_config.get(name)
                    current_value = default_value # Починаємо з дефолтного

                    if saved_raw_value is not None:
                        try:
                           # Спроба конвертації збереженого значення
                           if param_type is bool:
                               current_value = bool(saved_raw_value)
                           elif param_type is int:
                               current_value = int(saved_raw_value)
                           elif param_type is float:
                               current_value = float(saved_raw_value)
                           elif param_type is Decimal:
                               current_value = Decimal(str(saved_raw_value))
                           elif param_type is str:
                               current_value = str(saved_raw_value)
                           else: # Якщо тип невідомий, використовуємо збережене як є
                              current_value = saved_raw_value
                        except (ValueError, TypeError) as conv_err:
                           logger.warning(f"Не вдалося конвертувати збережене значення '{saved_raw_value}' для параметра '{name}' до типу {param_type}. Використовується значення за замовчуванням. Помилка: {conv_err}")
                           current_value = default_value # Повертаємось до дефолтного у разі помилки конвертації
                    elif default_value is None:
                        # Якщо немає збереженого і немає дефолтного, ставимо щось безпечне
                        if param_type is bool: current_value = False
                        elif param_type is int: current_value = 0
                        elif param_type is float: current_value = 0.0
                        elif param_type is Decimal: current_value = Decimal(0)
                        elif param_type is str: current_value = ""
                        logger.warning(f"Не знайдено збереженого або дефолтного значення для параметра '{name}'. Встановлено базове значення для типу {param_type}.")


                    input_key = f"backtest_param_{selected_test_strategy_name}_{name}"
                    label = f"{name}"
                    help_text = f"Тип: {param_type.__name__}. За замовч.: {default_value}"
                    input_value = None # Ініціалізуємо

                    # UI елементи (як у settings_tab, але з поточним значенням з бектест-форми)
                    try:
                        if param_type is bool:
                             input_value = st.checkbox(label, value=bool(current_value), key=input_key, help=help_text)
                        elif param_type is float:
                             is_perc = 'percent' in name.lower() or 'rate' in name.lower()
                             # Використовуємо try-except для безпечної конвертації
                             try: current_float = float(current_value)
                             except (ValueError, TypeError): current_float = 0.0

                             if is_perc:
                                 disp_val = current_float * 100
                                 step = 0.01
                                 fmt = "%.2f"
                                 label_ui = f"{label} (%)"
                             else:
                                 disp_val = current_float
                                 # Динамічний крок та формат на основі значення? Можливо занадто складно.
                                 step = 1e-6 # Малий крок для загальних float
                                 fmt = "%.6f"
                                 label_ui = label
                             input_disp = st.number_input(label_ui, value=disp_val, step=step, format=fmt, key=input_key, help=help_text)
                             if is_perc:
                                 input_value = input_disp / 100.0
                             else:
                                 input_value = input_disp
                        elif param_type is int:
                             try: current_int = int(current_value)
                             except (ValueError, TypeError): current_int = 0
                             input_value = st.number_input(label, value=current_int, step=1, key=input_key, help=help_text)
                        elif param_type is str:
                             input_value = st.text_input(label, value=str(current_value), key=input_key, help=help_text)
                        elif param_type is Decimal:
                              try: current_dec_float = float(current_value)
                              except (ValueError, TypeError): current_dec_float = 0.0
                              input_disp_float = st.number_input(label, value=current_dec_float, step=1e-8, format="%.8f", key=input_key, help=help_text)
                              input_value = Decimal(str(input_disp_float)) # Конвертуємо з float вводу
                        else:
                             st.text(f"{label}: {current_value} (Тип {param_type} не редагується)")
                             input_value = current_value # Зберігаємо поточне значення, яке не редагується
                    except Exception as ui_err:
                        logger.error(f"Помилка UI для параметра {name}: {ui_err}")
                        st.error(f"Помилка відображення '{name}'")
                        input_value = default_value # Повертаємось до дефолту при помилці UI

                    # Додаємо значення до словника
                    strategy_params_for_test[name] = input_value
                col_idx += 1

    # --- Вибір періоду ---
    st.markdown("**Період тестування**")
    col_date1, col_date2 = st.columns(2)

    # Використовуємо try-except на випадок проблем з datetime
    try:
        # Тепер timezone має бути доступний
        default_end_date = datetime.now(timezone.utc).date()
        # timedelta також імпортовано
        default_start_date = default_end_date - timedelta(days=90)
    except Exception as date_err:
        logger.error(f"Помилка при визначенні дат за замовчуванням: {date_err}")
        # Встановлюємо безпечні значення у разі помилки
        default_end_date = date.today()
        default_start_date = default_end_date - timedelta(days=90)

    with col_date1:
        start_date = st.date_input("Початкова дата", default_start_date, key="backtest_start_date", help="Включно")
    with col_date2:
        end_date = st.date_input("Кінцева дата", default_end_date, key="backtest_end_date", help="Включно")

    run_disabled = False
    if start_date > end_date:
         run_disabled = True
         st.error("Помилка: Початкова дата має бути раніше або дорівнювати кінцевій даті.")
    elif start_date == end_date:
         # Можливо, попередити, що тестування за 1 день може бути неінформативним?
         st.info("Тестування проводиться за один повний день.")


    # --- Запуск бектесту ---
    if st.button("🚀 Запустити бек-тестування", key="run_backtest_button", disabled=run_disabled):
        if selected_test_strategy_name and selected_test_strategy_name in available_strategies:
            strategy_class = available_strategies[selected_test_strategy_name]
            # Перевірка наявності Backtester API (вже є вище, але дублюємо для безпеки)
            if not backtester or not backtester.binance_api:
                 st.error("Помилка: Backtester або його Binance API не ініціалізовано. Перевірте ключі MainNet в .env та перезапустіть.")
                 return # Зупиняємо виконання тут

            with st.spinner(f"Запуск бектесту: {selected_test_strategy_name} на {test_symbol}... Завантаження даних..."):
                try:
                    # Конвертуємо дати в рядки UTC
                    # Додаємо час, щоб охопити весь день
                    start_datetime_utc = datetime.combine(start_date, datetime.min.time(), tzinfo=timezone.utc)
                    # Для кінцевої дати беремо кінець дня
                    end_datetime_utc = datetime.combine(end_date, datetime.max.time(), tzinfo=timezone.utc)

                    start_date_str = start_datetime_utc.strftime('%Y-%m-%d %H:%M:%S')
                    # Кінцеву дату треба коректно обробити, щоб Binance API включив останній день
                    # Наприклад, додати один день і взяти початок дня, або використовувати мілісекунди
                    # Простіший варіант - передавати дати як є, а Backtester нехай сам їх обробляє
                    # Або передавати datetime об'єкти
                    end_date_str = end_datetime_utc.strftime('%Y-%m-%d %H:%M:%S') # Кінець дня включно

                    logger.info(f"Запуск бектесту: Стратегія='{selected_test_strategy_name}', Символ='{test_symbol}', Інтервал='{test_interval}', Період='{start_date_str}' - '{end_date_str}', Капітал={initial_capital}, Поз={position_size_pct}%, Ком={commission_pct}%, TP={tp_pct}%, SL={sl_pct}%")
                    logger.info(f"Параметри стратегії для тесту: {strategy_params_for_test}")


                    # --- ВИКЛИК З ПАРАМЕТРАМИ СТРАТЕГІЇ ---
                    results = backtester.run_backtest(
                        symbol=test_symbol,
                        interval=test_interval,
                        strategy_class=strategy_class,
                        strategy_params=strategy_params_for_test, # <--- Передаємо параметри
                        start_date_str=start_date_str, # Передаємо рядок UTC
                        end_date_str=end_date_str,     # Передаємо рядок UTC
                        initial_capital=float(initial_capital), # Явна конвертація
                        position_size_pct=(float(position_size_pct)/100.0), # Конвертуємо % в частку
                        commission_pct=(float(commission_pct)/100.0),    # Конвертуємо % в частку
                        tp_pct=float(tp_pct) if float(tp_pct) > 0 else None, # None якщо 0
                        sl_pct=float(sl_pct) if float(sl_pct) > 0 else None  # None якщо 0
                    )

                    if results:
                        st.session_state['backtest_results'] = results
                        logger.info("Бек-тест успішно завершено.")
                        st.success("Бек-тестування завершено!")
                        # Можна одразу розгорнути результати
                        # st.experimental_rerun() # Може бути корисним, щоб оновити стан UI
                    else:
                        st.session_state['backtest_results'] = None
                        # Перевіряємо, чи є повідомлення про помилку в backtester
                        error_msg = getattr(backtester, 'last_error_message', "Бек-тест не повернув результатів. Перевірте логи.")
                        st.error(error_msg)
                        logger.error(f"Бек-тест не повернув результатів. Остання помилка бектестера: {error_msg}")

                except ImportError as imp_err:
                     st.session_state['backtest_results'] = None
                     logger.error(f"Помилка імпорту під час бектесту: {imp_err}", exc_info=True)
                     st.error(f"Помилка імпорту: {imp_err}. Перевірте залежності стратегії.")
                except FileNotFoundError as fnf_err:
                     st.session_state['backtest_results'] = None
                     logger.error(f"Помилка завантаження даних для бектесту: {fnf_err}", exc_info=True)
                     st.error(f"Помилка завантаження даних: {fnf_err}. Перевірте наявність даних або підключення до API.")
                except Exception as e:
                    st.session_state['backtest_results'] = None
                    logger.error(f"Загальна помилка під час бектесту: {e}", exc_info=True)
                    st.error(f"Помилка під час бектесту: {e}")
        else:
            st.warning("Оберіть стратегію зі списку.")

    st.divider()
    # --- Відображення результатів ---
    if 'backtest_results' in session_state and session_state['backtest_results']:
        results = session_state['backtest_results']
        params = results.get('parameters', {})
        metrics = results.get('metrics', {})
        # Перевірка наявності ключів перед доступом
        strategy_name_display = params.get('strategy', 'N/Д')
        symbol_display = params.get('symbol', 'N/Д')
        st.subheader(f"Результати: `{strategy_name_display}` ({symbol_display})")

        start_date_display = params.get('start_date', 'N/Д')
        end_date_display = params.get('end_date', 'N/Д')
        interval_display = params.get('interval', 'N/Д')
        initial_capital_display = results.get('initial_capital', 0)
        position_size_pct_display = params.get('position_size_pct', 0) * 100
        commission_pct_display = params.get('commission_pct', 0) * 100
        tp_pct_display = params.get('tp_pct', 0.0)
        sl_pct_display = params.get('sl_pct', 0.0)

        st.caption(
            f"Період: {start_date_display} - {end_date_display} | "
            f"Інтервал: {interval_display} | "
            f"Капітал: {initial_capital_display:.2f} | "
            f"Поз.: {position_size_pct_display:.1f}% | "
            f"Ком.: {commission_pct_display:.3f}% | "
            f"TP/SL: {tp_pct_display:.1f}%/{sl_pct_display:.1f}%"
        )
        with st.expander("Параметри стратегії, що використовувались у тесті"):
             strategy_params_display = params.get('strategy_params', {})
             # Конвертуємо Decimal в float для JSON серіалізації
             serializable_params = {k: float(v) if isinstance(v, Decimal) else v for k, v in strategy_params_display.items()}
             st.json(json.dumps(serializable_params, indent=2))

        st.markdown("---")

        # Відображення метрик з перевіркою наявності
        final_balance = results.get('final_balance', 0)
        total_profit = results.get('total_profit', 0)
        profit_percentage = results.get('profit_percentage', 0)
        total_trades = metrics.get('total_trades', 0)
        pf_metric = metrics.get('profit_factor') # Не ставимо 0 за замовчуванням, щоб відрізнити відсутність від 0

        col1, col2, col3, col4 = st.columns(4)
        col1.metric("Кінц. Баланс", f"{final_balance:.2f}")
        col2.metric("Total PnL", f"{total_profit:.2f}", f"{profit_percentage:.2f}%")
        col3.metric("Угод", total_trades)

        if pf_metric is None:
             col4.metric("Profit Factor", "N/A")
        elif pf_metric == float('inf'):
             col4.metric("Profit Factor", "∞")
        elif isinstance(pf_metric, (int, float)):
             col4.metric("Profit Factor", f"{pf_metric:.2f}")
        else:
             col4.metric("Profit Factor", f"{pf_metric}") # Якщо не число

        win_rate = metrics.get('win_rate')
        sharpe_ratio = metrics.get('sharpe_ratio')
        max_drawdown_pct = metrics.get('max_drawdown_pct')
        avg_trade_pnl = metrics.get('average_profit_per_trade')

        col5, col6, col7, col8 = st.columns(4)

        col5.metric("Win Rate", f"{win_rate:.2f}%" if isinstance(win_rate, (int, float)) else "N/A")
        col6.metric("Sharpe", f"{sharpe_ratio:.2f}" if isinstance(sharpe_ratio, (int, float)) else "N/A")
        col7.metric("Max Drawdown", f"{max_drawdown_pct:.2f}%" if isinstance(max_drawdown_pct, (int, float)) else "N/A")
        col8.metric("Avg Trade PnL", f"{avg_trade_pnl:.4f}" if isinstance(avg_trade_pnl, (int, float)) else "N/A")

        st.subheader("Графік Балансу")
        balance_history = results.get('balance_history')
        if isinstance(balance_history, pd.DataFrame) and not balance_history.empty:
            # Переконуємось, що індекс - це datetime для коректного відображення
            if not pd.api.types.is_datetime64_any_dtype(balance_history.index):
                 try:
                     balance_history.index = pd.to_datetime(balance_history.index)
                 except Exception as e:
                     logger.error(f"Не вдалося конвертувати індекс історії балансу в datetime: {e}")
                     st.warning("Не вдалося відобразити графік балансу через проблему з датами.")
                     balance_history = None # Не відображати графік

            if balance_history is not None:
                 st.line_chart(balance_history)
        elif isinstance(balance_history, list) and balance_history: # Якщо це список словників
             try:
                 df_balance = pd.DataFrame(balance_history)
                 if 'timestamp' in df_balance.columns and 'balance' in df_balance.columns:
                     df_balance['timestamp'] = pd.to_datetime(df_balance['timestamp'])
                     df_balance = df_balance.set_index('timestamp')
                     st.line_chart(df_balance[['balance']])
                 else:
                     st.warning("Історія балансу має неправильний формат.")
             except Exception as e:
                 logger.error(f"Помилка обробки списку історії балансу: {e}")
                 st.warning("Не вдалося відобразити графік балансу.")
        else:
            st.warning("Дані історії балансу відсутні або порожні.")


        with st.expander("Деталі та Список Угод"):
            st.subheader("Детальні Метрики")
            # Використовуємо json.dumps для коректного відображення infinity та NaN
            # Функція для обробки несеріалізованих значень
            def default_serializer(obj):
                if isinstance(obj, (Decimal)):
                    return float(obj)
                if obj == float('inf'):
                    return "Infinity"
                if obj == float('-inf'):
                    return "-Infinity"
                if isinstance(obj, float) and math.isnan(obj):
                    return "NaN"
                # Можна додати інші типи за потреби
                try:
                     # Спроба стандартної серіалізації
                     return json.JSONEncoder().encode(obj)
                except TypeError:
                    # Якщо стандартна не спрацювала, повертаємо рядок
                    return str(obj)

            try:
                 st.json(json.dumps(metrics, indent=2, default=default_serializer))
            except Exception as json_err:
                logger.error(f"Помилка серіалізації метрик в JSON: {json_err}")
                st.text(str(metrics)) # Відображаємо як текст, якщо JSON не вдалося

            st.subheader("Список Угод")
            trades = results.get('trades', []) # Очікуємо список словників
            if trades and isinstance(trades, list) and isinstance(trades[0], dict):
                 try:
                     df_trades = pd.DataFrame(trades)
                     # Опціонально конвертуємо колонки з датами/часом
                     for col in ['entry_time', 'exit_time']:
                          if col in df_trades.columns:
                              df_trades[col] = pd.to_datetime(df_trades[col], errors='coerce')
                     # Опціонально форматуємо float
                     for col in ['entry_price', 'exit_price', 'pnl', 'pnl_pct']:
                          if col in df_trades.columns:
                              # Перевіряємо, чи колонка числова перед форматуванням
                              if pd.api.types.is_numeric_dtype(df_trades[col]):
                                   df_trades[col] = df_trades[col].map('{:.4f}'.format) # Приклад форматування
                              else:
                                   # Якщо не числова, пробуємо конвертувати, ігноруючи помилки
                                   df_trades[col] = pd.to_numeric(df_trades[col], errors='coerce').map('{:.4f}'.format)


                     st.dataframe(df_trades, use_container_width=True)
                 except Exception as df_err:
                     logger.error(f"Помилка створення DataFrame для угод: {df_err}")
                     st.warning("Не вдалося відобразити список угод у вигляді таблиці.")
                     st.text(str(trades)) # Показуємо як текст
            elif trades: # Якщо trades не порожній, але не список словників
                 st.warning("Список угод має неочікуваний формат.")
                 st.text(str(trades))
            else:
                 st.info("Угоди під час цього бектесту відсутні.")
    elif 'backtest_results' in session_state and session_state['backtest_results'] is None:
         # Якщо бектест був запущений, але не повернув результатів (можливо, помилка)
         # Повідомлення про помилку вже мало бути виведено вище
         st.info("Результати бектестування відсутні.")
    # else:
         # Бектест ще не запускався
         # st.info("Налаштуйте параметри та запустіть бектестування.")
