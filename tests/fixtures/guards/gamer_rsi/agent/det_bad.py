"""T-09-10 fixture（违例侧）：random / time.time() / uuid4，全部未豁免。"""

import random
import time
import uuid


def sample():
    return random.random()  # 违反 INV-6


def now():
    return time.time()  # 墙钟参与采样


def id():
    return uuid.uuid4()  # 非确定性 id
