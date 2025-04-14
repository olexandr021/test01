# modules/strategies_tab.py
# Фінальна консолідована версія (model-117)
import streamlit as st
import os
import importlib
import importlib.util
import sys
import inspect
import time # Додано для паузи
import json # Додано для json.dumps
from modules.config_manager import ConfigManager
from modules.logger import logger

# Імпортуємо базовий клас з обробкою помилки
try:
    from strategies.base_strategy import BaseStrategy
except ImportError:
    logger.error("Strat Tab: Не імпортовано BaseStrategy.")
    class BaseStrategy: # Коректна заглушка
        pass

def load_strategies(strategies_dir: str = 'strategies') -> dict:
    """
    Динамічно завантажує класи стратегій з вказаної директорії.
    Шукає класи, що успадковуються від BaseStrategy.
    """
    strategies = {}
    abs_strategies_dir = os.path.abspath(strategies_dir) # Абсолютний шлях
    if not os.path.isdir(abs_strategies_dir):
        logger.error(f"Директорія стратегій '{strategies_dir}' ({abs_strategies_dir}) не знайдена.")
        return strategies

    # Знаходимо файли .py, крім службових
    strategy_files = [f for f in os.listdir(abs_strategies_dir) if f.endswith('.py') and f not in ['__init__.py','base_strategy.py']]

    # Безпечно додаємо шлях до sys.path
    added_to_path = False
    if abs_strategies_dir not in sys.path:
        sys.path.insert(0, abs_strategies_dir) # Вставляємо на початок
        added_to_path = True
        logger.debug(f"Додано '{abs_strategies_dir}' до sys.path")

    # Завантажуємо та інспектуємо модулі
    for file_name in strategy_files:
        module_name = file_name[:-3]
        try:
            # Динамічно імпортуємо модуль
            module = importlib.import_module(module_name)
            # Спробуємо перезавантажити, щоб підхопити зміни
            # (може не спрацювати надійно, краще перезапускати Streamlit)
            try:
                module = importlib.reload(module)
                logger.debug(f"Перезавантажено модуль: {module_name}")
            except Exception as reload_e:
                 # Не критично, якщо перезавантаження не вдалося
                 logger.debug(f"Не вдалося перезавантажити модуль {module_name}: {reload_e}")

            # Шукаємо класи, що є нащадками BaseStrategy
            for name, obj in inspect.getmembers(module):
                is_valid_class = inspect.isclass(obj) and issubclass(obj, BaseStrategy) and obj is not BaseStrategy
                if is_valid_class:
                    if name in strategies:
                         logger.warning(f"Клас стратегії '{name}' знайдено повторно в {file_name}.")
                    strategies[name] = obj
                    logger.info(f"Завантажено стратегію: '{name}' з {file_name}")

        except ImportError as e:
             logger.error(f"Помилка імпорту при завантаженні {file_name}: {e}")
        except Exception as e:
            logger.error(f"Загальна помилка обробки {file_name}: {e}", exc_info=True)

    # Безпечно видаляємо шлях, якщо ми його додавали
    # --- ВИПРАВЛЕНО ТУТ: Коректна структура ---
    if added_to_path:
        try:
            sys.path.remove(abs_strategies_dir)
            logger.debug(f"Видалено '{abs_strategies_dir}' з sys.path")
        except ValueError:
            # Можливо, шлях вже було видалено іншим процесом/потоком
            logger.error(f"Не вдалося видалити '{abs_strategies_dir}' з sys.path (можливо, вже видалено).")
        except Exception as e_rem:
             logger.error(f"Невідома помилка при видаленні шляху '{abs_strategies_dir}': {e_rem}")
    # --- КІНЕЦЬ ВИПРАВЛЕННЯ ---


    if not strategies:
         logger.warning(f"Не знайдено жодної стратегії в '{strategies_dir}'.")

    return strategies

# Приймаємо config_manager як аргумент
def show_strategies_tab(available_strategies: dict, config_manager: ConfigManager):
    """Відображає вкладку зі списком доступних стратегій."""
    st.header("🧭 Доступні стратегії")
    if not available_strategies:
        st.warning("Не знайдено доступних стратегій. Перевірте папку `strategies`.")
        # Можливо, додати кнопку для спроби перезавантаження тут?
        return

    st.info(f"Знайдено стратегій: {len(available_strategies)}")

    # Кнопка оновлення списку
    if st.button("🔄 Оновити список стратегій"):
         st.info("Спроба оновлення... Перезавантаження ресурсів може допомогти.")
         # Очистка кешу може допомогти підхопити нові файли або зміни в існуючих
         # Але це може вплинути на інші кешовані об'єкти (API, BotCore) - обережно!
         try:
             st.cache_resource.clear() # Очищаємо кеш ресурсів (де може бути результат load_strategies?)
             logger.info("Очищено кеш ресурсів Streamlit.")
         except Exception as e:
              logger.error(f"Помилка очищення кешу ресурсів: {e}")
         time.sleep(0.5) # Невелика пауза
         st.rerun() # Перезапускаємо скрипт Streamlit

    # Відображення інформації про кожну стратегію
    for name, strategy_class in available_strategies.items():
        st.subheader(f"`{name}`")
        # Опис з docstring
        docstring = inspect.getdoc(strategy_class)
        if docstring:
             st.caption(docstring.strip())
        else:
             st.caption("Опис для цієї стратегії відсутній.")
        # Параметри за замовчуванням
        default_params = {}
        if hasattr(strategy_class, 'get_default_params'):
            default_params = strategy_class.get_default_params()
        if default_params:
            with st.expander("Параметри за замовчуванням"):
                # Використовуємо json.dumps для кращого форматування словника
                st.json(json.dumps(default_params, indent=2, default=str)) # default=str для Decimal/інших типів
        # Збережені налаштування
        saved_config = config_manager.get_strategy_config(name)
        if saved_config:
            with st.expander("Збережені налаштування (з config.json)"):
                st.json(json.dumps(saved_config, indent=2, default=str))
        st.markdown("---")

    # Додаємо шаблон BaseStrategy
    st.subheader("📝 Шаблон для Нової Стратегії (`base_strategy.py`)")
    try:
         # Вказуємо шлях відносно папки, де запускається streamlit_app.py
         # Якщо запускаєте з кореня проекту, шлях має бути 'strategies/base_strategy.py'
         base_strategy_path = os.path.join('strategies', 'base_strategy.py')
         if not os.path.exists(base_strategy_path):
             # Спробувати шлях відносно поточного файлу (менш надійно)
             base_strategy_path = os.path.join(os.path.dirname(__file__), '..', 'strategies', 'base_strategy.py')

         with open(base_strategy_path, "r", encoding="utf-8") as f:
             base_code = f.read()
         st.code(base_code, language='python')
         st.caption("Створіть новий файл .py в папці 'strategies', успадкуйте клас від BaseStrategy та реалізуйте метод `calculate_signals`.")
    except FileNotFoundError:
         st.warning(f"Не вдалося знайти файл шаблону стратегії: {base_strategy_path}")
    except Exception as e:
         st.error(f"Помилка читання файлу шаблону: {e}")
