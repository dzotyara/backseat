"""`python -m backseat` still starts the Telegram bot: an alias of `python -m backseat.telegram`."""

from backseat.telegram.__main__ import main

if __name__ == "__main__":
    main()
