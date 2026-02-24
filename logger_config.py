"""
Logger configuration for Negotiation Chatbot.
Writes logs to negotiation_chatbot.log in the project directory.
"""
import logging
import os

LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "negotiation_chatbot.txt")

# Create formatter
_formatter = logging.Formatter(
    "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

# File handler - append to txt log file
_file_handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
_file_handler.setLevel(logging.DEBUG)
_file_handler.setFormatter(_formatter)


def get_logger(name: str) -> logging.Logger:
    """Get a logger that writes to negotiation_chatbot.log."""
    logger = logging.getLogger(name)
    if not logger.handlers:
        logger.setLevel(logging.DEBUG)
        logger.addHandler(_file_handler)
    return logger
