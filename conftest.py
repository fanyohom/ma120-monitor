"""让 tests/ 下的用例可以直接 import 仓库根目录的模块。

pytest 以 rootdir 方式运行时不一定把仓库根目录加入 sys.path，
这里显式插入，保证 `pytest`、`python -m pytest` 都能跑通。
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
