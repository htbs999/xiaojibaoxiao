import os
import sys

# 备份目录（虽然备份功能已移除，但保留以备将来使用）
BACKUP_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "backups")

def resource_path(relative_path):
    """获取资源的绝对路径（兼容打包和非打包环境）"""
    try:
        base_path = sys._MEIPASS
    except AttributeError:
        base_path = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base_path, relative_path)