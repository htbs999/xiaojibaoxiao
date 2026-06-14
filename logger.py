"""
日志系统 - 仅输出到控制台（适配云托管环境）
"""
import logging
import sys

def get_logger(name: str, level=logging.INFO) -> logging.Logger:
    """获取模块专属 logger，输出到 stdout"""
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger
    logger.setLevel(level)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter(
        "[%(asctime)s] [%(name)s] %(levelname)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    ))
    logger.addHandler(handler)
    return logger