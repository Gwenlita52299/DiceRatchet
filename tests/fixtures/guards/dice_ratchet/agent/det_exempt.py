"""T-09-10 fixture（豁免侧）：同样用法但带 ``# guard: allow`` 白名单注释 → exempted=True。"""

import random  # guard: allow E_IO_GUARD_RANDOM reason=demo-fixture
import time
import uuid


def sample():
    return random.random()  # guard: allow E_IO_GUARD_RANDOM reason=demo-fixture


def now():
    return time.time()  # guard: allow E_IO_GUARD_RANDOM reason=demo-fixture


def id():
    return uuid.uuid4()  # guard: allow * reason=demo-fixture
