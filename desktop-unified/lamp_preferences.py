"""Validated, per-user lamp appearance; no credentials or auto-start settings."""
import json
import os
from pathlib import Path
import threading

from core import DeviceError

DEFAULT_PATTERNS = {
    "idle": {"color": "GREEN", "effect": "SOLID"},
    "running": {"color": "YELLOW", "effect": "BREATHE"},
    "waiting": {"color": "YELLOW", "effect": "SLOW"},
    "error": {"color": "RED", "effect": "FAST"},
    "offline": {"color": "OFF", "effect": "SOLID"},
}
COLORS = {"RED", "YELLOW", "GREEN", "OFF"}
EFFECTS = {"SOLID", "BREATHE", "SLOW", "FAST"}


def validate_pattern(value):
    if not isinstance(value, dict) or set(value) != {"color", "effect"}:
        raise DeviceError("灯效需要 color 和 effect")
    color, effect = value["color"], value["effect"]
    if not isinstance(color, str) or color not in COLORS or not isinstance(effect, str) or effect not in EFFECTS:
        raise DeviceError("不支持的灯色或灯效")
    if effect == "BREATHE" and color not in {"YELLOW", "GREEN"}:
        raise DeviceError("当前接线只有黄灯 D5、绿灯 D6 支持硬件呼吸；红灯可选常亮或闪烁")
    if color == "OFF" and effect != "SOLID":
        raise DeviceError("熄灯不能搭配呼吸或闪烁")
    return dict(color=color, effect=effect)


def validate_patterns(values):
    if not isinstance(values, dict) or set(values) != set(DEFAULT_PATTERNS):
        raise DeviceError("请提交完整的五种状态灯效")
    result = {key: validate_pattern(value) for key, value in values.items()}
    if result["offline"] != DEFAULT_PATTERNS["offline"]:
        raise DeviceError("离线或心跳失效固定熄灯")
    return result


class LampPreferences:
    def __init__(self, path=None):
        self.path = Path(path) if path else Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "HLK-M30" / "lamp-settings.json"
        self.lock = threading.RLock()
        self.patterns = {k: dict(v) for k, v in DEFAULT_PATTERNS.items()}
        self.warning = ""
        try:
            self.patterns = validate_patterns(json.loads(self.path.read_text(encoding="utf-8"))["patterns"])
        except FileNotFoundError:
            pass
        except (OSError, ValueError, KeyError, TypeError, DeviceError):
            self.warning = "灯色配置无法读取，已使用默认灯效；保存配置可修复。"

    def snapshot(self):
        with self.lock:
            return {k: dict(v) for k, v in self.patterns.items()}

    def save(self, values):
        patterns = validate_patterns(values)
        with self.lock:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                temporary = self.path.with_suffix(".tmp")
                temporary.write_text(json.dumps({"version": 1, "patterns": patterns}, indent=2), encoding="utf-8")
                temporary.replace(self.path)
            except OSError as error:
                raise DeviceError("无法保存灯色配置，请检查目录写入权限") from error
            self.patterns = patterns
            self.warning = ""
