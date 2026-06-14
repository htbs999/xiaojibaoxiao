mport os
import sys

BACKUP_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "backups")

def resource_path(relative_path):
    try:
        base_path = sys._MEIPASS
    except AttributeError:
        base_path = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base_path, relative_path)