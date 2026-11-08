import configparser
import logging
from pathlib import Path

from signalbot import (
    Config,
    DataMessageContext,
    DataMessageHandler,
    SendMessage,
    SignalBot,
    text_triggered,
)


class PingCommand(DataMessageHandler):
    @text_triggered("Ping")
    async def handle_data_message(self, context: DataMessageContext) -> None:
        await context.send(SendMessage(text="Pong"))


if __name__ == "__main__":
    config = configparser.ConfigParser()
    settings_path = Path(__file__).parent / "settings.ini"
    if not config.read(settings_path):
        raise SystemExit(f"Missing {settings_path} (copy settings.ini.example)")

    bot = SignalBot(
        Config(
            connection_mode=config["signal"]["connection_mode"],
            signal_service=config["signal"]["api_url"],
            phone_number=config["signal"]["phone_number"],
            logging_level=logging.INFO,
        )
    )
    bot.register(PingCommand())
    bot.start()