# modules/history_tab.py
import streamlit as st
import pandas as pd
from modules.bot_core import BotCore
from modules.logger import logger
from decimal import Decimal
from datetime import datetime

def show_history_tab(bot_core: BotCore | None):
    """Відображає вкладку з історією угод та статистикою поточної сесії."""
    st.header("💾 Історія та Статистика")

    if bot_core is None:
        st.error("Помилка: Ядро бота не ініціалізовано.")
        return

    # --- Розділ Історії Угод ---
    st.subheader("Історія виконаних угод")
    trade_history = bot_core.get_trade_history()

    if trade_history:
        try:
            df_history = pd.DataFrame(trade_history)
            # --- Форматування ---
            if 'time' in df_history.columns:
                 df_history['time'] = pd.to_datetime(df_history['time']).dt.strftime('%Y-%m-%d %H:%M:%S')
            num_cols = ['price','last_fill_price','last_fill_qty','total_filled_qty','quantity','commission','pnl'] # Додаємо quantity
            for col in num_cols:
                if col in df_history.columns:
                    # Застосовуємо форматування тільки якщо значення не None і може бути конвертоване
                    def format_decimal(x):
                        if x is not None and isinstance(x, (Decimal, str, float, int)):
                             try:
                                 # Додано перевірку на NaN перед конвертацією в Decimal
                                 if isinstance(x, float) and pd.isna(x):
                                     return 'N/A'
                                 return f"{Decimal(str(x)):.8f}"
                             except Exception:
                                 return 'Error' # Або інше значення для помилки форматування
                        return 'N/A'
                    df_history[col] = df_history[col].apply(format_decimal)

            col_map = {'time':'Час','symbol':'Символ','side':'Сторона','type':'Тип','status':'Статус','order_id':'ID(Bin)','client_order_id':'ID(Bot)','price':'Ціна(Avg)','total_filled_qty':'К-сть','pnl':'PnL','commission':'Ком.','commission_asset':'Ком. Актив','purpose':'Призначення'}
            cols_show = [k for k,v in col_map.items() if k in df_history.columns]
            df_display = df_history[cols_show].rename(columns=col_map)
            if 'Час' in df_display.columns:
                # Спробуємо конвертувати 'Час' назад в datetime для коректного сортування
                try:
                    df_display['Час_dt'] = pd.to_datetime(df_history['time']) # Використовуємо оригінальний стовпець
                    df_display = df_display.sort_values(by='Час_dt', ascending=False).drop(columns=['Час_dt'])
                except Exception as sort_e:
                     logger.warning(f"Не вдалося сортувати історію за часом: {sort_e}")
                     # Якщо не вдалося, спробуємо сортувати як рядки (може бути некоректно)
                     df_display = df_display.sort_values(by='Час', ascending=False)

            st.dataframe(df_display.reset_index(drop=True), use_container_width=True, height=400)
            # --- Завантаження CSV ---
            @st.cache_data
            def convert_df(df):
                 return df.to_csv(index=False).encode('utf-8')

            csv_filename = f"history_{bot_core.symbol}_{datetime.now():%Y%m%d_%H%M}.csv"
            st.download_button(
                label="Завантажити CSV",
                data=convert_df(df_display),
                file_name=csv_filename,
                mime="text/csv",
                key='download-csv'
            )
        except Exception as e:
            st.error(f"Помилка відображення історії: {e}")
            logger.error(f"Помилка DataFrame історії: {e}", exc_info=True)
    else:
        st.info("Історія угод порожня.")

    st.divider()
    # --- Статистика Сесії ---
    st.subheader("Статистика поточної сесії")
    session_stats = bot_core.get_session_stats() # Отримуємо float значення
    if session_stats and session_stats.get('total_trades', 0) > 0: # Показуємо, тільки якщо були угоди
        col1, col2, col3 = st.columns(3)
        col1.metric("Тривалість", session_stats.get('session_duration','N/A'))
        col2.metric("Угод", session_stats.get('total_trades',0))
        col3.metric("Win Rate", f"{session_stats.get('win_rate',0.0):.2f}%")

        col4, col5, col6 = st.columns(3)
        col4.metric("Total PnL", f"{session_stats.get('total_pnl',0.0):.4f} {bot_core.quote_asset}")
        col5.metric("Avg Win", f"{session_stats.get('avg_profit',0.0):.4f}")
        col6.metric("Avg Loss", f"{session_stats.get('avg_loss',0.0):.4f}")

        pf = session_stats.get('profit_factor', 0.0)
        if pf == float('inf'):
             pf_str = "∞"
        else:
             pf_str = f"{pf:.2f}"
        st.metric("Profit Factor", pf_str)
        # TODO: Додати кнопку скидання статистики (потрібна команда для BotCore)
    else:
        st.info("Статистика за поточну сесію відсутня.")
