"""Installation-specific paths stay local and outside the shared source."""
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONFIG = HERE / 'guard-settings.json'
if not CONFIG.is_file():
    raise RuntimeError('请先运行 install.cmd 安装本机桥，再从Kimi控制台打开守卫')
settings = json.loads(CONFIG.read_text(encoding='utf-8-sig'))
DATA_ROOT = Path(settings['dataRoot']).expanduser().resolve()
if DATA_ROOT == HERE or HERE in DATA_ROOT.parents:
    raise RuntimeError('运行数据必须放在扩展目录之外，请重新运行安装并设置DataRoot')
