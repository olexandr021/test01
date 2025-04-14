# modules/predictions_tab.py
import streamlit as st
from modules.logger import logger

# Приймає статус, але поки не використовує
def show_predictions_tab(bot_status: dict | None = None):
    """Відображає вкладку для прогнозів руху ціни."""
    st.header("🔮 Прогнози руху ціни")
    st.info("ℹ️ Функціонал прогнозування ціни та аналізу точності прогнозів знаходиться в розробці.")
    # --- Майбутня реалізація ---
