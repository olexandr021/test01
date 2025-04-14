# strategies/base_strategy.py
# Фінальна перевірена версія
from abc import ABC, abstractmethod
from modules.logger import logger
from modules.config_manager import ConfigManager # Імпортуємо тип для type hinting
import inspect # Для інтроспекції параметрів
from decimal import Decimal # Для можливої обробки параметрів
import math # Для порівняння float
import pandas as pd # Для типізації в calculate_signals

class BaseStrategy(ABC):
    """Абстрактний базовий клас для всіх торгових стратегій."""

    required_klines_length: int = 100 # Мінімальна кількість свічок (можна перевизначити)

    def __init__(self, symbol: str, config_manager: ConfigManager, config: dict | None = None):
        """
        Ініціалізація базової стратегії.

        Args:
            symbol (str): Торговий символ (напр., 'BTCUSDT').
            config_manager (ConfigManager): Екземпляр менеджера конфігурації.
            config (dict, optional): Специфічна конфігурація для цієї стратегії,
                                     передана ззовні (напр., для бектесту).
        """
        self.symbol = symbol
        self.config_manager = config_manager

        # Спочатку беремо переданий конфіг, якщо він є
        loaded_config = config if config is not None else self._load_config()
        # Об'єднуємо з дефолтними параметрами класу
        default_p = self.get_default_params()
        self.config = {**default_p, **loaded_config} # Передані/з файлу значення перезаписують дефолтні

        self.last_signal = None

        logger.debug(f"Стратегія '{self.__class__.__name__}' ({self.symbol}) ініціалізована з конфігом: {self.config}")


    @abstractmethod
    def calculate_signals(self, data: pd.DataFrame) -> str | None:
        """
        Розраховує індикатори та генерує торговий сигнал ('BUY', 'SELL', 'CLOSE' або None).
        Має бути реалізовано у дочірніх класах.

        Args:
            data (pd.DataFrame): DataFrame з історією Klines (очікується 'close' типу Decimal).

        Returns:
            str or None: Торговий сигнал або None.
        """
        pass

    # --- Допоміжні методи ---

    def _load_config(self) -> dict:
        """Завантажує конфігурацію для цього класу стратегії."""
        strategy_name = self.__class__.__name__
        config_data = self.config_manager.get_strategy_config(strategy_name)
        logger.debug(f"Завантажено конфіг для {strategy_name}: {config_data}")
        return config_data

    def _save_config(self):
        """Зберігає поточну конфігурацію цієї стратегії."""
        strategy_name = self.__class__.__name__
        try:
            self.config_manager.set_strategy_config(strategy_name, self.config)
            logger.info(f"Збережено конфіг для {strategy_name}: {self.config}")
        except Exception as e:
            logger.error(f"Помилка збереження конфігу {strategy_name}: {e}")

    @classmethod
    def get_default_params(cls) -> dict:
        """Повертає словник параметрів за замовчуванням."""
        params = {}
        if hasattr(cls, 'default_params') and isinstance(cls.default_params, dict):
            params = cls.default_params.copy()
        else:
            try:
                signature = inspect.signature(cls.__init__)
                for name, param in signature.parameters.items():
                    if name not in ['self', 'symbol', 'config_manager', 'config'] and param.default != inspect.Parameter.empty:
                        params[name] = param.default
            except Exception as e:
                 logger.error(f"Помилка інтроспекції {cls.__name__}: {e}")
        return params

    @classmethod
    def get_default_params_with_types(cls) -> dict:
        """Повертає словник параметрів за замовчуванням та їх типів."""
        params_info = {}
        defaults = cls.get_default_params()
        try:
            # Визначаємо типи на основі значень за замовчуванням
            for name, value in defaults.items():
                params_info[name] = {'default': value, 'type': type(value)}

            # Уточнюємо типи через інтроспекцію анотацій __init__
            signature = inspect.signature(cls.__init__)
            for name, param in signature.parameters.items():
                is_valid_param = name in params_info and param.annotation != inspect.Parameter.empty
                if is_valid_param and isinstance(param.annotation, type):
                    params_info[name]['type'] = param.annotation

        except Exception as e:
            logger.error(f"Помилка отримання типів параметрів для {cls.__name__}: {e}")
            # Fallback: Якщо інтроспекція анотацій не вдалась, використовуємо типи значень
            if not params_info and defaults:
                for name, value in defaults.items():
                    params_info[name] = {'default': value, 'type': type(value)}
        return params_info


    def update_config_parameters(self, params: dict):
        """Оновлює та зберігає параметри конфігурації стратегії."""
        config_changed = False
        logger.debug(f"Оновлення параметрів {self.__class__.__name__} з: {params}")
        default_params_info = self.get_default_params_with_types()
        for key, new_value in params.items():
            if key in default_params_info:
                current_value = self.config.get(key, default_params_info[key]['default'])
                expected_type = default_params_info[key]['type']
                converted_value = None
                try:
                    if expected_type is bool and not isinstance(new_value, bool):
                        converted_value = str(new_value).lower() in ['true','1','t','y','yes','on']
                    elif expected_type is Decimal and not isinstance(new_value, Decimal):
                        converted_value = Decimal(str(new_value))
                    elif expected_type is int and not isinstance(new_value, int):
                        converted_value = int(float(new_value))
                    elif expected_type is float and not isinstance(new_value, float):
                        converted_value = float(new_value)
                    elif expected_type is str and not isinstance(new_value, str):
                        converted_value = str(new_value)
                    else:
                         # Тип збігається або не базовий
                         converted_value = new_value

                    # Порівняння
                    needs_update = False
                    if key not in self.config:
                        needs_update = True
                    elif isinstance(current_value, Decimal) or isinstance(converted_value, Decimal):
                        needs_update = Decimal(str(current_value)) != Decimal(str(converted_value))
                    elif isinstance(current_value, float) or isinstance(converted_value, float):
                        needs_update = not math.isclose(float(current_value), float(converted_value), rel_tol=1e-9)
                    else:
                        needs_update = current_value != converted_value

                    if needs_update:
                        self.config[key] = converted_value
                        logger.debug(f"Параметр '{key}' оновлено: {converted_value}")
                        config_changed = True

                except (ValueError,TypeError) as e:
                    logger.warning(f"Помилка конвертації '{key}' ({new_value}) до {expected_type}: {e}")
            else:
                 logger.warning(f"Спроба оновити невідомий параметр '{key}'")

        if config_changed:
            self._save_config()
        else:
            logger.debug(f"Змін у конфігу {self.__class__.__name__} не було.")
