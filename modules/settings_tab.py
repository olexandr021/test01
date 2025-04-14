# modules/settings_tab.py
import streamlit as st
from modules.config_manager import ConfigManager
import inspect
from modules.logger import logger
from decimal import Decimal # Для коректної роботи з типами
import math # Для порівняння float
from modules.bot_core import BotCore # Для отримання статусу

# Додаємо bot_core для отримання інформації про режим
def show_settings_tab(session_state, config_manager: ConfigManager, available_strategies: dict, bot_core: BotCore | None):
    """Відображає вкладку 'Налаштування'."""
    st.header("⚙️ Налаштування")

    st.warning("⚠️ **Увага:** Зміна деяких налаштувань (Режим роботи, Символ, Стратегія) потребує **перезапуску ядра бота** для застосування.")

    # --- Загальні налаштування бота ---
    st.subheader("Загальні Налаштування Бота")
    bot_config = config_manager.get_bot_config()
    edited_bot_config = bot_config.copy() # Редагуємо копію

    # --- Вибір Режиму ---
    current_core_mode = 'TestNet' # Default
    if bot_core:
        current_core_mode = bot_core.active_mode
    else:
        current_core_mode = bot_config.get('active_mode','TestNet')
    st.caption(f"Поточний режим ядра: {current_core_mode}")
    modes = ['TestNet', 'Реальний']
    default_mode_index = 0
    if current_core_mode in modes:
         default_mode_index = modes.index(current_core_mode)
    selected_mode = st.selectbox(
        "Режим Роботи для Наступного Запуску",
        modes,
        index=default_mode_index,
        key="setting_active_mode"
    )
    edited_bot_config['active_mode'] = selected_mode

    # --- Вибір Символу ---
    current_symbol = 'BTCUSDT' # Default
    if bot_core:
         current_symbol = bot_core.symbol
    else:
         current_symbol = bot_config.get('trading_symbol','BTCUSDT')
    edited_bot_config['trading_symbol'] = st.text_input(
        "Торговий Символ для Наступного Запуску",
        value=current_symbol,
        key="setting_symbol").upper()

    # --- Вибір Стратегії ---
    current_strategy = None # Default
    if bot_core:
         current_strategy = bot_core.selected_strategy_name
    else:
         current_strategy = bot_config.get('selected_strategy')
    strategy_names = [""] + list(available_strategies.keys()) # Додаємо порожній варіант
    default_strategy_index = 0
    if current_strategy and current_strategy in strategy_names:
        default_strategy_index = strategy_names.index(current_strategy)
    selected_strategy = st.selectbox(
        "Стратегія для Наступного Запуску",
        strategy_names,
        index=default_strategy_index,
        key="setting_strategy",
        format_func=lambda x: x if x else "Не обрано" # Показуємо "Не обрано" для ""
    )
    if selected_strategy:
         edited_bot_config['selected_strategy'] = selected_strategy
    else:
         edited_bot_config['selected_strategy'] = None


    # --- Налаштування Торгівлі ---
    st.markdown("**Параметри Торгівлі**")
    edited_bot_config['position_size_percent'] = st.number_input("Розмір позиції (% капіталу)", value=float(bot_config.get('position_size_percent',10.0)), min_value=0.1, max_value=100.0, step=0.1, format="%.1f", key="setting_pos_size")
    edited_bot_config['take_profit_percent'] = st.number_input("Тейк Профіт (%)", value=float(bot_config.get('take_profit_percent',1.0)), min_value=0.01, step=0.01, format="%.2f", key="setting_tp")
    edited_bot_config['stop_loss_percent'] = st.number_input("Стоп Лос (%)", value=float(bot_config.get('stop_loss_percent',0.5)), min_value=0.01, step=0.01, format="%.2f", key="setting_sl")
    edited_bot_config['commission_taker'] = st.number_input("Комісія Taker (%)", value=float(bot_config.get('commission_taker',0.075)), min_value=0.0, max_value=1.0, step=0.001, format="%.3f", key="setting_comm_taker")

    # --- Налаштування Telegram ---
    st.markdown("**Інтеграція з Telegram**")
    edited_bot_config['telegram_enabled'] = st.checkbox("Увімкнути сповіщення Telegram", value=bot_config.get('telegram_enabled', False), key="settings_telegram_enabled")
    if edited_bot_config['telegram_enabled']:
        edited_bot_config['telegram_token'] = st.text_input("Токен Telegram бота", value=bot_config.get('telegram_token',''), type="password", key="settings_telegram_token")
        edited_bot_config['telegram_chat_id'] = st.text_input("ID чату Telegram", value=bot_config.get('telegram_chat_id',''), key="settings_telegram_chat_id")


    # --- Кнопка збереження загальних налаштувань ---
    st.divider()
    if st.button("💾 Зберегти Загальні Налаштування", key="save_bot_settings"):
        try:
            if edited_bot_config != bot_config:
                 config_manager.set_bot_config(edited_bot_config)
                 st.success("Загальні налаштування збережено! Перезапустіть ядро бота для застосування змін режиму/символу/стратегії.")
                 logger.info(f"Збережено загальні налаштування: {edited_bot_config}")
                 # Оновити конфіг в BotCore? Або він сам перезавантажить при старті?
                 # Можна додати команду RELOAD_SETTINGS, якщо треба оновити "на льоту"
                 if bot_core:
                      bot_core.command_queue.put({'type':'RELOAD_SETTINGS'})
            else:
                 st.info("Змін у загальних налаштуваннях не виявлено.")
        except Exception as e:
             st.error(f"Помилка збереження: {e}")
             logger.error(f"Помилка збереження bot_config: {e}")

    st.divider()
    # --- Налаштування обраної стратегії ---
    st.subheader("Налаштування Параметрів Стратегій")
    # Додаємо вибір стратегії для налаштування
    strategy_to_configure = st.selectbox(
        "Обрати стратегію для редагування параметрів:",
        [""] + list(available_strategies.keys()), # Додаємо порожній варіант
        key="settings_strategy_selector",
        format_func=lambda x: x if x else "--- Оберіть Стратегію ---"
    )

    if strategy_to_configure:
        strategy_class = available_strategies[strategy_to_configure]
        saved_strategy_config = config_manager.get_strategy_config(strategy_to_configure)
        # Створюємо копію для зберігання оновлених значень
        updated_params = {}
        st.markdown(f"**Параметри для `{strategy_to_configure}`:**")

        try:
            # Використовуємо метод класу для отримання дефолтних параметрів і типів
            default_params_info = strategy_class.get_default_params_with_types()

            if not default_params_info:
                 st.caption("Ця стратегія не має параметрів для налаштування.")
            else:
                # Створюємо поля вводу
                for name, info in default_params_info.items():
                    param_type = info.get('type', str)
                    current_value = saved_strategy_config.get(name, info.get('default'))
                    input_key = f"settings_param_{strategy_to_configure}_{name}"
                    label = f"{name}"
                    help_text = f"За замовч.: {info.get('default','Н/Д')}"
                    input_value = None # Ініціалізуємо змінну

                    # UI елементи (як у model-21)
                    if param_type is bool:
                        input_value = st.checkbox(label, value=bool(current_value), key=input_key, help=help_text)
                    elif param_type is float:
                        is_perc = 'percent' in name.lower() or 'rate' in name.lower()
                        if is_perc:
                             disp_val = float(current_value) * 100
                             step = 0.01
                             fmt = "%.2f"
                             label_ui = f"{label} (%)"
                        else:
                             disp_val = float(current_value)
                             step = 1e-6
                             fmt = "%.6f"
                             label_ui = label
                        input_disp = st.number_input(label_ui, value=disp_val, step=step, format=fmt, key=input_key, help=help_text)
                        if is_perc:
                             input_value = input_disp / 100
                        else:
                             input_value = input_disp
                    elif param_type is int:
                        input_value = st.number_input(label, value=int(current_value), step=1, key=input_key, help=help_text)
                    elif param_type is str:
                        input_value = st.text_input(label, value=str(current_value), key=input_key, help=help_text)
                    elif param_type is Decimal:
                        # Використовуємо float для st.number_input, але зберігаємо як Decimal
                        input_disp_float = st.number_input(label, value=float(current_value), step=1e-8, format="%.8f", key=input_key, help=help_text)
                        input_value = Decimal(str(input_disp_float)) # Додано Decimal
                    else:
                        st.text(f"{label}: {current_value} (Тип {param_type} не ред.)")
                        input_value = current_value # Зберігаємо поточне значення

                    # Додаємо перевірку, чи значення було створено перед додаванням до словника
                    if input_value is not None:
                        updated_params[name] = input_value

                # Кнопка збереження параметрів стратегії
                if st.button(f"💾 Зберегти для '{strategy_to_configure}'", key=f"save_{strategy_to_configure}"):
                    try:
                        # TODO: Зробити валідацію типів перед збереженням?
                        config_manager.set_strategy_config(strategy_to_configure, updated_params)
                        st.success(f"Налаштування '{strategy_to_configure}' збережено!")
                        logger.info(f"Збережено параметри {strategy_to_configure}: {updated_params}")
                        # Повідомити BotCore? Можливо, через RELOAD_SETTINGS
                        if bot_core:
                             bot_core.command_queue.put({'type':'RELOAD_SETTINGS'})
                    except Exception as e:
                         st.error(f"Помилка збереження: {e}")
                         logger.error(f"Помилка збереження strat_config {strategy_to_configure}: {e}")

        except Exception as e:
             st.error(f"Помилка параметрів {strategy_to_configure}: {e}")
             logger.error(f"Помилка інтроспекції {strategy_to_configure}: {e}", exc_info=True)

    else:
        st.info("Оберіть стратегію вище для редагування її специфічних параметрів.")
