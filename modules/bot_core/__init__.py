# modules/bot_core/__init__.py

# BLOCK 1: Expose package classes
# Робимо основні класи доступними напряму з пакету bot_core
# Наприклад, замість from modules.bot_core.core import BotCore
# можна буде писати from modules.bot_core import BotCore

from .core import BotCore
from .core import BotCoreError
from .state_manager import StateManager
from .state_manager import StateManagerError

# Можна також визначити __all__ для явної вказівки публічного API
__all__ = [
    'BotCore',
    'BotCoreError',
    'StateManager',
    'StateManagerError'
]
# END BLOCK 1
