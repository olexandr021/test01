# modules/config_manager.py
import json
import os
import shutil
import time
import threading
from modules.logger import logger

class ConfigManagerError(Exception):
    """Спеціальний клас винятків для ConfigManager."""
    pass

class ConfigManager:
    """
    Клас для управління конфігурацією бота, що зберігається у JSON файлі.
    Потокобезпечний.
    """
    def __init__(self, config_file='config.json'):
        """
        Ініціалізує менеджер конфігурації.

        Args:
            config_file (str): Шлях до файлу конфігурації JSON.
        """
        self.config_file = config_file
        self.lock = threading.Lock()
        self.config = self._load_config()
        logger.info(f"Менеджер конфігурації ініціалізовано. Файл: {self.config_file}")

    def _load_config(self) -> dict:
        """
        Завантажує конфігурацію з файлу. Якщо файл не існує, створює його з конфігурацією за замовчуванням.
        Повертає конфігурацію у вигляді словника. Потокобезпечно.
        """
        with self.lock:
            if os.path.exists(self.config_file):
                try:
                    with open(self.config_file, 'r', encoding='utf-8') as f:
                        config_data = json.load(f)
                    logger.debug(f"Конфігурацію завантажено з {self.config_file}")
                    config_data.setdefault('bot', {})
                    config_data.setdefault('strategies', {})
                    return config_data
                except json.JSONDecodeError as e:
                    logger.error(f"Помилка JSON у файлі {self.config_file}: {e}. Створення файлу за замовчуванням.")
                    try:
                        backup_file = f"{self.config_file}.backup_{int(time.time())}"
                        shutil.copyfile(self.config_file, backup_file)
                        logger.info(f"Створено резервну копію: {backup_file}")
                    except Exception as backup_e:
                         logger.error(f"Не вдалося створити резервну копію: {backup_e}")
                    return self._create_default_config_internal()
                except Exception as e:
                    logger.error(f"Невідома помилка завантаження конфігурації з {self.config_file}: {e}", exc_info=True)
                    raise ConfigManagerError(f"Не вдалося завантажити конфігурацію: {e}")
            else:
                logger.warning(f"Файл конфігурації {self.config_file} не знайдено. Створення файлу за замовчуванням.")
                return self._create_default_config_internal()

    def _create_default_config_internal(self) -> dict:
        """Створює та повертає конфігурацію за замовчуванням (не потокобезпечний сам по собі)."""
        default_config = {
            'bot': {
                'active_mode': 'TestNet',
                'selected_strategy': None,
                'trading_symbol': 'BTCUSDT',
                'position_size_percent': 10.0,
                'commission_taker': 0.075,
                'commission_maker': 0.075,
                'take_profit_percent': 1.0,
                'stop_loss_percent': 0.5,
                'telegram_enabled': False,
                'telegram_token': '',
                'telegram_chat_id': '',
            },
            'strategies': {}
        }
        try:
            # Викликаємо внутрішній метод збереження, бо вже тримаємо блокування з _load_config
            self._save_config_internal(default_config)
            logger.info(f"Створено файл конфігурації за замовчуванням: {self.config_file}")
        except ConfigManagerError as e:
             logger.error(f"Не вдалося зберегти початкову конфігурацію: {e}")
        return default_config

    def _save_config_internal(self, config_to_save: dict):
        """Внутрішній метод збереження без блокування (викликається з _load або _save)."""
        temp_file = f"{self.config_file}.{os.getpid()}.tmp"
        try:
            with open(temp_file, 'w', encoding='utf-8') as f:
                json.dump(config_to_save, f, indent=4, ensure_ascii=False, sort_keys=True)
            shutil.move(temp_file, self.config_file)
            logger.debug(f"Конфігурацію успішно збережено у {self.config_file}")
        except Exception as e:
             logger.error(f"Помилка запису конфігурації у файл {self.config_file}: {e}", exc_info=True)
             if os.path.exists(temp_file):
                 try:
                      os.remove(temp_file)
                 except OSError: # <-- Виправлено тут
                      pass # <-- Виправлено тут
             raise ConfigManagerError(f"Не вдалося зберегти конфігурацію: {e}")

    def _save_config(self, config: dict | None = None):
        """
        Зберігає конфігураційний словник у JSON файл. Якщо config=None, зберігає self.config.
        Використовує безпечний метод запису. Потокобезпечно.
        """
        with self.lock:
            # Визначаємо, який конфіг зберігати
            config_to_save = None
            if config is None:
                config_to_save = self.config
            else:
                config_to_save = config
            # Викликаємо внутрішній метод збереження
            self._save_config_internal(config_to_save)
            # Оновлюємо внутрішній self.config, якщо зберігали переданий словник
            # Це важливо, щоб self.config завжди відповідав збереженому стану
            if config is not None:
                 self.config = config_to_save # Оновлюємо основний конфіг копією того, що зберегли

    def get_config(self) -> dict:
        """Повертає копію поточного конфігураційного словника. Потокобезпечно."""
        import copy
        with self.lock:
             return copy.deepcopy(self.config)

    def get_strategy_config(self, strategy_name: str) -> dict:
        """Повертає копію конфігурації для вказаної стратегії. Потокобезпечно."""
        import copy
        with self.lock:
            # Використовуємо setdefault, щоб гарантовано мати ключ 'strategies'
            strategies_dict = self.config.setdefault('strategies', {})
            strategy_cfg = strategies_dict.get(strategy_name, {})
            return copy.deepcopy(strategy_cfg)

    def set_strategy_config(self, strategy_name: str, config: dict):
        """Встановлює конфігурацію для стратегії та зберігає зміни. Потокобезпечно."""
        with self.lock:
            self.config.setdefault('strategies', {})[strategy_name] = config.copy() # Зберігаємо копію
        # Зберігаємо поза блокуванням, щоб уникнути подвійного блокування, якщо _save_config блокує
        self._save_config() # Збереже поточний self.config

    def get_bot_config(self) -> dict:
        """Повертає копію загальної конфігурації бота. Потокобезпечно."""
        import copy
        with self.lock:
            # Використовуємо setdefault, щоб гарантовано мати ключ 'bot'
            bot_cfg = self.config.setdefault('bot', {})
            return copy.deepcopy(bot_cfg)

    def set_bot_config(self, config: dict):
        """Встановлює загальну конфігурацію бота та зберігає зміни. Потокобезпечно."""
        with self.lock:
            self.config['bot'] = config.copy() # Зберігаємо копію
        # Зберігаємо поза блокуванням
        self._save_config()

    def reload_config(self):
        """Перезавантажує конфігурацію з файлу. Потокобезпечно."""
        logger.info("Перезавантаження конфігурації з файлу...")
        self.config = self._load_config()
