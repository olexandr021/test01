# modules/logger.py
import logging
import os
import traceback # Імпортуємо traceback на початку для чистоти
from logging.handlers import TimedRotatingFileHandler

LOG_DIR = 'logs'
DEFAULT_LOG_FILE = os.path.join(LOG_DIR, 'bot.log')

def setup_logger(log_file=DEFAULT_LOG_FILE, level=logging.DEBUG, name="BotLogger"):
    """
    Налаштовує логер для запису у файл та виводу в консоль.
    Використовує TimedRotatingFileHandler для ротації логів.
    """
    try:
        log_dir = os.path.dirname(log_file)
        if log_dir and not os.path.exists(log_dir):
            os.makedirs(log_dir)
            print(f"Створено директорію для логів: {log_dir}") # print, бо логер ще не готовий

        logger_instance = logging.getLogger(name)

        # Перевірка, чи логер вже налаштований
        if logger_instance.hasHandlers():
            logger_instance.setLevel(level) # Просто оновлюємо рівень, якщо потрібно
            # print(f"Логер '{name}' вже налаштовано.") # Можна прибрати повторне повідомлення
            return logger_instance

        logger_instance.setLevel(level)
        logger_instance.propagate = False # Не передавати повідомлення батьківським логерам

        log_format = '%(asctime)s - %(levelname)-8s - [%(name)s:%(threadName)s:%(lineno)d] - %(message)s'
        formatter = logging.Formatter(log_format)

        # --- Обробник для файлу з ротацією ---
        # Новий файл щоночі, зберігати останні 14 днів
        file_handler = TimedRotatingFileHandler(log_file, when="midnight", interval=1, backupCount=14, encoding='utf-8')
        file_handler.setFormatter(formatter)
        file_handler.setLevel(logging.DEBUG) # Записуємо все у файл
        logger_instance.addHandler(file_handler)

        # --- Обробник для консолі ---
        stream_handler = logging.StreamHandler()
        stream_handler.setFormatter(formatter)
        stream_handler.setLevel(logging.INFO) # Виводимо INFO та вище в консоль
        logger_instance.addHandler(stream_handler)

        print(f"Логер '{logger_instance.name}' налаштовано. Рівень: {logging.getLevelName(level)}. Файл: {log_file}")
        return logger_instance

    except Exception as e:
        print(f"ПОМИЛКА НАЛАШТУВАННЯ ЛОГЕРА: {e}")
        # import traceback # Перенесено на початок файлу
        traceback.print_exc()
        logging.basicConfig(level=logging.WARNING) # Налаштовуємо базовий логер
        return logging.getLogger("fallback_logger")

# --- Створюємо основний логер ---
# Зверніть увагу: якщо цей файл імпортується декілька разів, логер буде створено один раз
# завдяки механізму logging.getLogger()
logger = setup_logger(name="BotCoreLogger")

# Приклад використання
if __name__ == '__main__':
    logger.debug("Тест DEBUG логера.")
    logger.info("Тест INFO логера.")
    logger.warning("Тест WARNING логера.")
    logger.error("Тест ERROR логера.")
    logger.critical("Тест CRITICAL логера.")
    try:
        x = 1 / 0
    except ZeroDivisionError:
        logger.error("Тест винятку", exc_info=True)
