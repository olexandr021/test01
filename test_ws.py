# test_ws.py
# Простий скрипт для тестування WebSocket з'єднання з Binance TestNet

import websocket
import threading
import time
import logging
import sys # Для виводу в stderr

# Налаштування логування (дуже схоже на logger.py, але виводимо все в консоль)
log_format = '%(asctime)s - %(levelname)-8s - [%(name)s:%(threadName)s] - %(message)s'
formatter = logging.Formatter(log_format)
# Налаштовуємо обробник для виводу в консоль (stderr)
stream_handler = logging.StreamHandler(sys.stderr)
stream_handler.setFormatter(formatter)
stream_handler.setLevel(logging.DEBUG) # Показуємо все
# Налаштовуємо логер бібліотеки websocket-client
ws_logger = logging.getLogger("websocket")
ws_logger.setLevel(logging.DEBUG) # Встановлюємо рівень DEBUG
ws_logger.addHandler(stream_handler) # Додаємо наш обробник
ws_logger.propagate = False # Не передаємо вище
# Вмикаємо трейсинг бібліотеки
websocket.enableTrace(True)

# Логер для нашого скрипта
script_logger = logging.getLogger("WSTestScript")
script_logger.setLevel(logging.DEBUG)
script_logger.addHandler(stream_handler)
script_logger.propagate = False

# --- Callback функції ---
def on_message(ws, message):
    script_logger.info(f"Отримано повідомлення: {message[:200]}...")

def on_error(ws, error):
    script_logger.error(f"Помилка WebSocket: {error}", exc_info=True)

def on_close(ws, close_status_code, close_msg):
    script_logger.warning(f"З'єднання WebSocket закрито. Код: {close_status_code}, Повідомлення: '{close_msg}'")

def on_open(ws):
    script_logger.info(">>> З'єднання WebSocket ВІДКРИТО! <<<")
    # Підписуємось на потік після відкриття
    subscription_message = {
      "method": "SUBSCRIBE",
      "params": [
        "btcusdt@kline_1m" # Тільки один потік для простоти
      ],
      "id": 1
    }
    import json
    ws.send(json.dumps(subscription_message))
    script_logger.info(f"Надіслано підписку: {subscription_message}")

# --- Основна логіка ---
if __name__ == "__main__":
    # URL для Binance TestNet (простий kline потік)
    # test_url = "wss://testnet.binance.vision/ws/btcusdt@kline_1m"
    # URL для комбінованого потоку (як у боті, але без listen key)
    test_url = "wss://testnet.binance.vision/stream?streams=btcusdt@kline_1m"

    script_logger.info(f"Спроба підключення до: {test_url}")

    # Створюємо WebSocketApp
    ws_app = websocket.WebSocketApp(
        test_url,
        on_open=on_open,
        on_message=on_message,
        on_error=on_error,
        on_close=on_close
    )

    # Запускаємо в окремому потоці, щоб можна було зупинити по Ctrl+C
    ws_thread = threading.Thread(target=ws_app.run_forever, daemon=True, name="TestWSThread")
    ws_thread.start()
    script_logger.info("Цикл WebSocket запущено в фоновому потоці. Натисніть Ctrl+C для виходу.")

    try:
        # Тримаємо основний потік живим
        while ws_thread.is_alive():
            time.sleep(1)
    except KeyboardInterrupt:
        script_logger.info("Отримано Ctrl+C. Закриття з'єднання...")
        ws_app.close() # Закриваємо з'єднання
        ws_thread.join(timeout=5) # Чекаємо на завершення потоку
        script_logger.info("Скрипт завершено.")
    except Exception as main_e:
         script_logger.error(f"Неперехоплена помилка в основному потоці: {main_e}", exc_info=True)
         ws_app.close()
         ws_thread.join(timeout=5)