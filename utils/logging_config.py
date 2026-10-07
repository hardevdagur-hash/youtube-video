import logging
import sys
from config.settings import settings
from infrastructure.log_redaction import RedactingFormatter, install_secret_redaction, quiet_http_loggers

def setup_logging() -> None:
    """Sets up standard, centralized logging for the application.
    
    Logs will be outputted to the console (sys.stdout) and written to 'logs/app.log'.
    The log level is governed by settings.log_level.
    """
    log_format = "%(asctime)s [%(levelname)s] %(name)s:%(filename)s:%(lineno)d - %(message)s"
    date_format = "%Y-%m-%d %H:%M:%S"

    # Never let credentials (e.g. YouTube ?key=...) reach console or file output
    install_secret_redaction()
    quiet_http_loggers()

    # Root logger
    root_logger = logging.getLogger()
    # Remove existing handlers to avoid duplicates
    for handler in root_logger.handlers[:]:
        root_logger.removeHandler(handler)
        
    root_logger.setLevel(settings.log_level)
    
    # 1. Console Handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(RedactingFormatter(log_format, date_format))
    root_logger.addHandler(console_handler)
    
    # 2. File Handler
    log_file_path = settings.logs_dir / "app.log"
    try:
        file_handler = logging.FileHandler(log_file_path, encoding="utf-8")
        file_handler.setFormatter(RedactingFormatter(log_format, date_format))
        root_logger.addHandler(file_handler)
    except IOError as e:
        # Fallback if log directory or file is not writeable
        logging.critical(f"Failed to initialize file logger at {log_file_path}: {e}")
