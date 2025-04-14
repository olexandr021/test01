# strategies/moving_average_crossover.py
from strategies.base_strategy import BaseStrategy
import pandas as pd
from decimal import Decimal
from modules.logger import logger
from modules.config_manager import ConfigManager
import numpy as np # Для перевірки NaN

class MovingAverageCrossoverStrategy(BaseStrategy):
    """
    Стратегія на основі перетину двох ковзних середніх (SMA).
    """
    # Визначаємо параметри за замовчуванням
    default_params = {
        'fast_period': 12,
        'slow_period': 26,
    }
    # Мінімальна к-сть свічок (визначається в __init__)
    required_klines_length: int = 27

    def __init__(self, symbol: str, config_manager: ConfigManager, config: dict = None):
        """ Ініціалізація стратегії. """
        super().__init__(symbol, config_manager, config) # config об'єднається з default_params всередині BaseStrategy

        try:
            self.fast_period = int(self.config.get('fast_period')) # Беремо вже об'єднаний конфіг
            self.slow_period = int(self.config.get('slow_period'))
        except (ValueError, TypeError, KeyError) as e:
             logger.error(f"Помилка параметрів MA: {e}. Використання дефолтних.")
             self.fast_period = self.default_params['fast_period']
             self.slow_period = self.default_params['slow_period']

        # Встановлюємо потрібну к-сть свічок
        self.required_klines_length = self.slow_period + 1 # Потрібна попередня свічка для MA

        logger.info(f"MovingAverageCrossoverStrategy [{self.symbol}] ініціалізована. Fast:{self.fast_period}, Slow:{self.slow_period}, Need:{self.required_klines_length}")

        is_fast_invalid = self.fast_period <= 0
        is_slow_invalid = self.slow_period <= 0
        is_order_invalid = self.fast_period >= self.slow_period
        if is_fast_invalid or is_slow_invalid or is_order_invalid:
            logger.error(f"Некоректні періоди MA: Fast={self.fast_period}, Slow={self.slow_period}. Скинуто до дефолту.")
            self.fast_period = self.default_params['fast_period']
            self.slow_period = self.default_params['slow_period']
            self.required_klines_length = self.slow_period + 1

    def calculate_signals(self, data: pd.DataFrame) -> str | None:
        """ Розраховує індикатори та генерує торговий сигнал. """
        signal = None
        log_prefix = f"MA Crossover [{self.symbol}]"
        try:
            if not isinstance(data, pd.DataFrame):
                 logger.warning(f"{log_prefix}: Отримано не DataFrame.")
                 return None
            if 'close' not in data.columns:
                 logger.warning(f"{log_prefix}: Немає стовпця 'close'.")
                 return None
            if len(data) < self.required_klines_length:
                 logger.debug(f"{log_prefix}: Недостатньо даних ({len(data)}/{self.required_klines_length}).")
                 return None

            close_prices = data['close'].dropna() # Переконуємось, що працюємо з Decimal
            if len(close_prices) < self.required_klines_length:
                logger.debug(f"{log_prefix}: Недостатньо НЕ-NaN даних ({len(close_prices)}/{self.required_klines_length}).")
                return None

            # Розрахунок MA
            fast_ma = close_prices.rolling(window=self.fast_period, min_periods=self.fast_period).mean()
            slow_ma = close_prices.rolling(window=self.slow_period, min_periods=self.slow_period).mean()

            # Перевірка наявності достатньої кількості значень MA для аналізу
            has_enough_ma = len(fast_ma) >= 2 and len(slow_ma) >= 2
            has_nan_in_last_two = fast_ma.iloc[-2:].isnull().any() or slow_ma.iloc[-2:].isnull().any()
            if not has_enough_ma or has_nan_in_last_two:
                logger.debug(f"{log_prefix}: Недостатньо даних MA для аналізу.")
                return None

            # Останні два значення
            last_fast = fast_ma.iloc[-1]
            prev_fast = fast_ma.iloc[-2]
            last_slow = slow_ma.iloc[-1]
            prev_slow = slow_ma.iloc[-2]

            # Логіка сигналів
            # Перевіряємо, чи значення не є NaN перед порівнянням
            if pd.notna(prev_fast) and pd.notna(prev_slow) and pd.notna(last_fast) and pd.notna(last_slow):
                cross_up = prev_fast <= prev_slow and last_fast > last_slow
                cross_down = prev_fast >= prev_slow and last_fast < last_slow

                if cross_up:
                    signal = 'BUY'
                    logger.info(f"{log_prefix} Сигнал BUY @ {close_prices.iloc[-1]:.4f} (Fast[{self.fast_period}] > Slow[{self.slow_period}])")
                elif cross_down:
                    signal = 'CLOSE'
                    logger.info(f"{log_prefix} Сигнал CLOSE @ {close_prices.iloc[-1]:.4f} (Fast[{self.fast_period}] < Slow[{self.slow_period}])")
                # else: сигналу немає, залишається None
            else:
                 logger.warning(f"{log_prefix}: Знайдено NaN в останніх значеннях MA, сигнал не визначено.")
                 signal = None # Явно вказуємо None при NaN

            self.last_signal = signal
            return signal
        except Exception as e:
            logger.error(f"{log_prefix} Помилка calculate_signals: {e}", exc_info=True)
            return None
