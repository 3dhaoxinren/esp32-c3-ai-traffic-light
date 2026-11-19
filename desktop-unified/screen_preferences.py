"""OLED preferences and completion selection; no message content sent to Nano."""
import json
import os
from pathlib import Path
from core import DeviceError

DEFAULT_SCREEN = {'rotation': 0, 'mode': 'AUTO', 'done_text': '已完成'}
SCREEN_MODES = {'AUTO', 'RAIN', 'DONE', 'STATUS', 'OFF'}
SCREEN_STATES = {'IDLE', 'RUNNING', 'WAITING', 'ERROR', 'OFFLINE', 'DONE'}

def validate_screen(data):
    if not isinstance(data, dict) or set(data) not in ({'rotation', 'mode'}, {'rotation','mode','done_text'}):
        raise DeviceError('屏幕设置需要方向和显示模式')
    if type(data['rotation']) is not int or data['rotation'] not in (0,90,180,270):
        raise DeviceError('屏幕方向不支持')
    if not isinstance(data['mode'],str) or data['mode'] not in SCREEN_MODES:
        raise DeviceError('屏幕模式不支持')
    if 'done_text' in data:
        validate_text(data['done_text'])
        render_notice(data['done_text'],data['rotation'])
    return dict(data)

def validate_text(text):
    if not isinstance(text,str) or not text.strip() or len(text)>32 or any(ord(c)<32 and c!='\n' for c in text):
        raise DeviceError('请输入 1–32 个字符，可使用中英文和换行')
    return text

def render_notice(text,rotation):
    from PIL import Image,ImageDraw,ImageFont
    validate_text(text)
    if type(rotation) is not int or rotation not in (0,90,180,270):raise DeviceError('屏幕方向不支持')
    w,h=(64,128) if rotation in (90,270) else (128,64)
    font=ImageFont.truetype(str(Path(os.environ.get('WINDIR','C:/Windows'))/'Fonts/msyh.ttc'),12)
    rows=[]
    for part in text.split('\n'):
        row=''
        for c in part:
            if row and font.getlength(row+c)>w-8:rows.append(row);row=''
            row+=c
        rows.append(row)
    if len(rows)*14>h-6:raise DeviceError('当前方向放不下这些文字，请减少文字或换行')
    image=Image.new('1',(w,h));draw=ImageDraw.Draw(image)
    for i,row in enumerate(rows):
        draw.text(((w-font.getlength(row))/2,(h-len(rows)*14)//2+i*14),row,font=font,fill=1,anchor='lt')
    if rotation:image=image.rotate(-rotation,expand=True)
    return bytes(sum((1<<b) if image.getpixel((x,p*8+b)) else 0 for b in range(8)) for p in range(8) for x in range(128))

def screen_target(snapshot):
    target = snapshot['target'].upper()
    if target == 'IDLE' and not snapshot.get('preview'):
        if any(r.get('completed') and not r['stale'] and r['state']=='idle' for r in snapshot['sessions']):
            return 'DONE'
    return target

class ScreenPreferences:
    def __init__(self,path=None):
        self.path=Path(path) if path else Path(os.environ.get('LOCALAPPDATA',str(Path.home())))/'HLK-M30/screen-settings.json'
        self.values=dict(DEFAULT_SCREEN)
        self.warning=''
        try:self.values={**DEFAULT_SCREEN,**validate_screen(json.loads(self.path.read_text(encoding='utf-8')))}
        except FileNotFoundError:pass
        except (OSError,ValueError,TypeError,DeviceError):self.warning='屏幕配置无法读取，已使用横屏自动跟随。'
    def snapshot(self):return dict(self.values)
    def save(self,data):
        value={**DEFAULT_SCREEN,**validate_screen(data)}
        try:
            self.path.parent.mkdir(parents=True,exist_ok=True)
            temp=self.path.with_suffix('.tmp')
            temp.write_text(json.dumps(value),encoding='utf-8');temp.replace(self.path)
        except OSError as error:raise DeviceError('无法保存屏幕设置') from error
        self.values=value;self.warning=''
