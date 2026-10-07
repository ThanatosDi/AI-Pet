"""PyInstaller 的進入點。"""

import os
import sys
from pathlib import Path

# 無主控台（windowed）模式下 sys.stdout / sys.stderr 是 None，print 會直接崩潰。
# stdout 丟掉；stderr 寫到 ~/.ai-pet/ai-pet.log，出問題時可以查。
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w", encoding="utf-8")
if sys.stderr is None:
    try:
        log = Path(os.environ.get("AI_PET_HOME") or Path.home() / ".ai-pet") / "ai-pet.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        sys.stderr = open(log, "a", encoding="utf-8", buffering=1)
    except OSError:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")

from ai_pet.cli import main  # noqa: E402

main()
