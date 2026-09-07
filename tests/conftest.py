import os

# Must be set before bot.py / llm_client.py are imported anywhere in the test session.
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123456:TEST-TOKEN-abcdefghijklmnopqrst")
os.environ.setdefault("OPENROUTER_API_KEY", "test-key")
os.environ.setdefault("BOT_PROMPT_FILE", "does-not-exist.txt")
os.environ.setdefault("BOT_PROMPT", "test persona")
