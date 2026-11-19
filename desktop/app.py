"""AI indicator desktop controller. Run: python -m desktop.app."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import argparse, logging, os, sys, time
from PySide6.QtCore import QObject,QSettings,Qt,QTimer,Signal
from PySide6.QtGui import QFont,QFontDatabase
from PySide6.QtWidgets import (QApplication,QCheckBox,QComboBox,QFileDialog,QFrame,QGridLayout,
 QHBoxLayout,QLabel,QLineEdit,QMessageBox,QProgressBar,QPushButton,QSpinBox,QTextEdit,QVBoxLayout,QWidget)
try:
    from . import device as d
    from .editor import Editor
except ImportError:
    import device as d
    from editor import Editor

STYLE="""
QWidget#app{background:#101819;color:#f7f2e7;}
QFrame#card{background:#182427;border:1px solid #304347;border-radius:15px;}
QLabel{color:#f7f2e7;font-size:14px;}
QLabel[kind="title"]{font-size:36px;font-weight:700;}
QLabel[kind="section"]{font-size:20px;font-weight:700;}
QLabel[kind="hint"]{color:#adc0bc;font-size:13px;}
QLabel[kind="tag"]{color:#9ee8c0;font-size:14px;font-weight:700;}
QLabel[kind="lamp"]{font-size:31px;color:#617977;font-weight:700;}
QPushButton{background:#26383b;color:#f7f2e7;border:1px solid #456266;border-radius:8px;padding:9px 13px;min-height:23px;font-weight:600;}
QPushButton:hover{border-color:#9ee8c0;background:#304649;}
QPushButton:disabled{color:#81928f;background:#222d2f;border-color:#314246;}
QPushButton[kind="primary"]{background:#a9e8c4;color:#10241b;border-color:#a9e8c4;font-weight:700;}
QPushButton[kind="primary"]:hover{background:#c7f4d9;}
QPushButton[kind="primary"]:disabled{background:#2c5142;color:#8fa89f;border-color:#2c5142;}
QLineEdit,QComboBox,QSpinBox,QTextEdit{color:#f6f1e5;background:#101b1d;border:1px solid #3a5054;border-radius:7px;padding:7px 10px;selection-background-color:#4a9277;}
QLineEdit:focus,QComboBox:focus,QSpinBox:focus,QTextEdit:focus{border-color:#9ee8c0;}
QComboBox QAbstractItemView{background:#182729;color:#f6f1e5;selection-background-color:#326655;}
QCheckBox{color:#dae7de;spacing:8px;}
QProgressBar{background:#101b1d;border:1px solid #384f53;border-radius:4px;height:8px;}
QProgressBar::chunk{background:#a9e8c4;border-radius:3px;}
"""
def label(text,kind=None):
    m=QLabel(text); m.setTextFormat(Qt.TextFormat.PlainText); m.setWordWrap(True)
    if kind:m.setProperty('kind',kind)
    return m
def card(title,caption):
    f=QFrame(); f.setObjectName('card'); v=QVBoxLayout(f)
    v.setContentsMargins(21,18,21,19);v.setSpacing(11)
    v.addWidget(label(title,'section'));v.addWidget(label(caption,'hint'))
    return f,v

class Bus(QObject):
    done=Signal(str,object,object)
    progress=Signal(int)

class Window(QWidget):
    def __init__(self):
        super().__init__()
        self.setObjectName('app')
        self.setWindowTitle('AI 指示灯 · 红绿灯控制台')
        self.setMinimumSize(1050,800); self.resize(1210,855)
        self.cfg=QSettings('AILamp','ControlDesk')
        self.pool=ThreadPoolExecutor(max_workers=1);self.bus=Bus()
        self.bus.done.connect(self.finish);self.bus.progress.connect(self.set_progress)
        self.busy=False;self.actions=[];self.data=None;self.segments=[];self.data_source='';self.active_state=None;self.active_target=None
        root=QVBoxLayout(self); root.setContentsMargins(28,21,28,20); root.setSpacing(15)
        top=QHBoxLayout()
        tl=QVBoxLayout();tl.setSpacing(3);tl.addWidget(label('AI 指示灯','title'))
        tl.addWidget(label('红绿灯控制台   /   ESP32-C3 × MAX98357A','hint'));top.addLayout(tl,1)
        self.connected=label('○  未连接','tag');top.addWidget(self.connected);root.addLayout(top)
        main=QHBoxLayout();main.setSpacing(17);root.addLayout(main,1)
        left=QVBoxLayout(); left.setSpacing(13);main.addLayout(left,46)
        c,v=card('01  设备连接','先尝试 USB 直连；首次连接 Wi-Fi 也从 USB 配置。');left.addWidget(c)
        line=QHBoxLayout();self.mode=QComboBox();self.mode.addItems(['USB 直连','Wi-Fi 局域网'])
        self.usb=QComboBox();self.ip=QLineEdit();self.ip.setPlaceholderText('设备局域网 IPv4')
        self.ip.setText(self.cfg.value('wifi_ip','',type=str));self.refresh_ports()
        line.addWidget(self.mode);line.addWidget(self.usb,1);line.addWidget(self.ip,1)
        self.scan=self.button('扫描',self.refresh_ports);line.addWidget(self.scan)
        v.addLayout(line)
        b=QHBoxLayout();b.addWidget(self.button('连接检查',self.check,True));b.addWidget(self.button('通过 USB 配 Wi-Fi',self.ask_wifi))
        v.addLayout(b)
        self.device_status=label('尚未检查设备。','hint');v.addWidget(self.device_status)
        self.mode.currentIndexChanged.connect(self.mode_changed);self.mode_changed()
        c,v=card('02  灯态联动','手动试灯，或让设备显示当前任务状态。');left.addWidget(c,1)
        self.lamp=label('●   待选择','lamp');v.addWidget(self.lamp)
        grid=QGridLayout(); grid.setSpacing(8)
        for j,(title,cmd,ink) in enumerate([('绿灯 · 空闲','IDLE','#a9e8c4'),('黄灯 · 运行','RUNNING','#f2ce77'),('黄闪 · 等待','WAITING','#f2ce77'),('红闪 · 报错','ERROR','#fc9790'),('绿灯 · 完成','DONE','#a9e8c4'),('熄灯','OFFLINE','#a1b2ae')]):
            p=self.button(title,lambda checked=False,s=cmd,t=title,col=ink:self.state(s,t,col))
            grid.addWidget(p,j//2,j%2)
        v.addLayout(grid)
        self.keep=QCheckBox('保持状态（每 5 秒给设备续期）');self.keep.setChecked(False)
        self.keep.toggled.connect(self.keep_changed); v.addWidget(self.keep)
        v.addWidget(self.button('设置各状态的播报',self.state_tracks))
        v.addStretch()
        right=QVBoxLayout();right.setSpacing(13);main.addLayout(right,54)
        c,v=card('03  配置 & 自定义播报','输入播报文字，或者导入录音。音频直接存到灯里。');right.addWidget(c,1)
        row=QHBoxLayout(); self.slot=QSpinBox();self.slot.setRange(1,3000);self.slot.setValue(2)
        self.gain=QSpinBox();self.gain.setRange(0,9);self.gain.setValue(7)
        row.addWidget(label('音频编号','hint'));row.addWidget(self.slot,1)
        row.addSpacing(10);row.addWidget(label('音量','hint'));row.addWidget(self.gain,1)
        row.addWidget(self.button('应用音量',self.apply_gain));v.addLayout(row)
        self.role=label('02–33：顶部按键可用；01：任务完成播报。','hint');v.addWidget(self.role)
        self.speech=QTextEdit();self.speech.setPlaceholderText('比如：别睡了，表！任务完成了！')
        self.speech.setMinimumHeight(114);v.addWidget(self.speech,1)
        choices=QHBoxLayout();choices.addWidget(self.button('生成本地语音',self.generate));choices.addWidget(self.button('导入音频',self.import_audio))
        choices.addWidget(self.button('选 FFmpeg',self.find_ffmpeg));v.addLayout(choices)
        cuts=QHBoxLayout();cuts.addWidget(self.button('剪辑与分段',self.edit));cuts.addWidget(self.button('USB 读回已存音频',self.read_stored));v.addLayout(cuts)
        self.material=label('没有待发送音频。Windows 语音合成不需要上传文字。','hint');v.addWidget(self.material)
        self.preview=QCheckBox('推送后按所选音量试播');self.preview.setChecked(False);v.addWidget(self.preview)
        row=QHBoxLayout();self.push_btn=self.button('推送到指示灯',self.push_audio,True);row.addWidget(self.push_btn,1)
        row.addWidget(self.button('播放已存编号',self.play));row.addWidget(self.button('停止播报',self.stop))
        v.addLayout(row)
        bar=QProgressBar();bar.setRange(0,100);bar.setValue(0);bar.setTextVisible(False);bar.setFixedHeight(8);self.bar=bar;v.addWidget(bar)
        c,v=card('顶部按键语音','从 02 开始连续编号；只存了 02 就设为 1 条。');right.addWidget(c)
        row=QHBoxLayout();self.count=QSpinBox();self.count.setRange(0,32);self.count.setValue(3)
        row.addWidget(label('轮播范围','hint'));row.addWidget(self.count,1);row.addWidget(self.button('保存按键语音数',self.apply_count));v.addLayout(row)
        self.status=label('就绪 · 只扫描 C3 原生 USB，不打开 CH340 / Nano。','hint')
        root.addWidget(self.status)
        self.timer=QTimer(self);self.timer.setInterval(5000);self.timer.timeout.connect(self.refresh_state)
    def button(self,text,cb,primary=False):
        p=QPushButton(text);p.setCursor(Qt.CursorShape.PointingHandCursor)
        if primary:p.setProperty('kind','primary')
        p.clicked.connect(cb);self.actions.append(p);return p
    def refresh_ports(self):
        prior=self.usb.currentText();self.usb.clear();self.usb.addItems(d.ports())
        if prior:self.usb.setCurrentText(prior)
        if not self.usb.count():self.usb.addItem('未发现 C3')
    def keep_changed(self,on):
        if on:self.timer.start()
        else:self.timer.stop()
    def mode_changed(self):
        isusb=self.mode.currentIndex()==0
        self.usb.setVisible(isusb);self.ip.setVisible(not isusb);self.scan.setVisible(isusb)
        self.active_state=None;self.connected.setText('○  未连接')
    def target(self,usb=False):
        mode='USB' if usb or self.mode.currentIndex()==0 else 'WiFi'
        name=self.usb.currentText() if mode=='USB' else self.ip.text().strip()
        if mode=='USB':d.usb_address(name)
        return d.Target(mode,name)
    def task(self,key,message,func,callback=None,quiet=False):
        if self.busy:return False
        self.busy=True; self.last_callback=callback;self.quiet=quiet
        for w in self.actions:w.setEnabled(False)
        if not quiet:self.status.setText(message)
        self.set_progress(0)
        def run():
            try:self.bus.done.emit(key,func(),None)
            except Exception as ex:self.bus.done.emit(key,None,str(ex) or type(ex).__name__)
        self.pool.submit(run)
        return True
    def set_progress(self,n):self.bar.setValue(n)
    def finish(self,key,result,error):
        self.busy=False
        for w in self.actions:w.setEnabled(True)
        if error:
            self.status.setText('未完成 · '+error)
            self.set_progress(0)
            if key in ('check','hold'):self.connected.setText('○  连接未确认')
            if key=='hold':self.active_state=None
            return
        if self.last_callback:self.last_callback(result)
        elif not self.quiet:self.status.setText(str(result))
    def error(self,f):
        try:return f()
        except Exception as ex:self.status.setText('未完成 · '+str(ex));return None
    def check(self):
        t=self.error(self.target)
        if not t:return
        def done(r):
            self.connected.setText('●  '+('USB' if t.mode=='USB' else 'Wi-Fi')+' 已连接')
            g=r['pcm']; net=r['net'];lamp=r['lamp']; addr=net.get('ip','')
            if addr and addr!='0.0.0.0':
                self.ip.setText(addr);self.cfg.setValue('wifi_ip',addr)
            if g.get('gain','').isdigit():self.gain.setValue(int(g['gain']))
            self.device_status.setText(f"{t.address}   ·   功放 {'就绪' if g.get('i2s')=='1' else '未就绪'}   ·   语音分区 {'就绪' if g.get('fs')=='1' else '空白 / 未就绪'}")
            self.status.setText('设备已识别 · '+lamp.get('state',lamp.get('color','')))
        self.task('check','正在识别设备…',lambda:d.check_device(t),done)
    def ask_wifi(self):
        from PySide6.QtWidgets import QDialog,QDialogButtonBox,QFormLayout
        t=self.error(lambda:self.target(True))
        if not t:return
        dlg=QDialog(self);dlg.setWindowTitle('USB 配置设备 Wi-Fi'); f=QFormLayout(dlg)
        f.addRow(label('设备需要可用的 2.4 GHz Wi-Fi。密码只发送给通过 USB 连接的 C3。','hint'))
        ssid=QLineEdit();pw=QLineEdit();pw.setEchoMode(QLineEdit.EchoMode.Password)
        f.addRow('Wi-Fi 名称',ssid);f.addRow('Wi-Fi 密码',pw)
        bt=QDialogButtonBox(QDialogButtonBox.StandardButton.Ok|QDialogButtonBox.StandardButton.Cancel)
        bt.accepted.connect(dlg.accept);bt.rejected.connect(dlg.reject);f.addRow(bt)
        if dlg.exec()!=QDialog.DialogCode.Accepted:return
        a,b=ssid.text(),pw.text();pw.clear()
        def done(net):
            address=net.get('ip','')
            if net.get('state')=='ONLINE' and address!='0.0.0.0':
                self.ip.setText(address);self.cfg.setValue('wifi_ip',address);self.mode.setCurrentIndex(1)
                self.status.setText('设备已加入 Wi-Fi。请点「连接检查」确认局域网控制。')
            else:self.status.setText('设备已保存 Wi-Fi 配置，尚未取得 IP；稍后用 USB「连接检查」查看。')
        self.task('wifi','正在通过 USB 配置，等设备取得 IP…',
          lambda:d.with_link(t,lambda f:d.wifi_provision(f,a,b,self.bus.progress.emit)),done)
    def state(self,s,title,color):
        t=self.error(self.target)
        if not t:return
        def done(_):
            self.active_state=s;self.active_target=t;self.lamp.setText('●   '+title)
            self.lamp.setStyleSheet('color:'+color);self.status.setText('已发送灯态 · '+title)
            if self.keep.isChecked() and not self.timer.isActive():self.timer.start()
        self.task('state','正在设置灯态…',lambda:d.with_link(t,lambda f:d.okay(f,'LIGHT AI '+s,'OK AI ')),done)
    def refresh_state(self):
        s,t=self.active_state,self.active_target
        if not s or not t or not self.keep.isChecked():return
        self.task('hold','',lambda:d.with_link(t,lambda f:d.okay(f,'LIGHT AI '+s,'OK AI ')),quiet=True)
    def find_ffmpeg(self):
        name,_=QFileDialog.getOpenFileName(self,'选择 FFmpeg','','FFmpeg (ffmpeg.exe ffmpeg);;所有文件 (*)')
        if name:self.cfg.setValue('ffmpeg',name);self.status.setText('已设置 FFmpeg。')
    def loaded(self,r):
        self.data,self.data_source=r;self.segments=[]
        self.material.setText(f'{self.data_source}  ·  {len(self.data)/32000:.2f} 秒  ·  待推送')
        self.status.setText('音频准备完成：选择编号后推送。')
    def generate(self):
        text=self.speech.toPlainText().strip()
        self.task('tts','正在生成 Windows 本地语音…',lambda:(d.speech(text),'自定义播报'),self.loaded)
    def import_audio(self):
        name,_=QFileDialog.getOpenFileName(self,'导入录音','','音频 (*.wav *.mp3 *.m4a *.flac *.ogg);;所有文件 (*)')
        if name:
            ff=self.cfg.value('ffmpeg','',type=str)
            self.task('import','正在转换音频…',lambda:(d.load_source(name,ff or None),Path(name).name),self.loaded)
    def push_audio(self):
        t=self.error(self.target)
        if not t:return
        if not self.data:self.status.setText('请先生成或导入要推送的音频。');return
        slot,g,play,data=self.slot.value(),self.gain.value(),self.preview.isChecked(),self.data
        cuts=list(self.segments)
        if not cuts:
            try:d.valid(data)
            except ValueError:self.status.setText('音频超过单段上限，请先剪到每段 32 秒以内。');return
        numbers='、'.join(str(s.slot).zfill(4) for s in cuts) if cuts else str(slot).zfill(4)
        ask=QMessageBox.question(self,'推送音频','将写入编号 '+numbers+'。同编号的原语音会被替换。继续？')
        if ask!=QMessageBox.StandardButton.Yes:return
        operation=(lambda:d.push_segments(t,data,cuts,play,g,self.bus.progress.emit)) if cuts else (lambda:d.transfer(t,data,slot,play,g,self.bus.progress.emit))
        self.task('push','正在分块推送并检查数据…',operation)
    def edit(self):
        if not self.data:self.status.setText('请先生成、导入或读回一段音频。');return
        w=Editor(self,self.data,self.slot.value(),self.segments)
        if w.exec():
            self.segments=w.queue
            self.material.setText(f'{self.data_source}  ·  已剪 {len(w.queue)} 段  ·  等待推送')
            self.status.setText('分段已保存到当前编辑；未写入设备。')
    def read_stored(self):
        t=self.error(lambda:self.target(True))
        if not t:return
        n=self.slot.value()
        self.task('read','正在通过 USB 读回已存语音…',
          lambda:(d.stored_audio(t,n,self.bus.progress.emit),f'灯内语音 {n:04d}'),self.loaded)
    def state_tracks(self):
        t=self.error(self.target)
        if not t:return
        self.task('bindings','正在读取灯内的状态配置…',lambda:d.with_link(t,d.get_state_tracks),lambda rows:self.show_tracks(t,rows))
    def show_tracks(self,t,rows):
        from PySide6.QtWidgets import QDialog,QDialogButtonBox,QFormLayout
        dialog=QDialog(self);dialog.setWindowTitle('任务状态 → 播报编号'); dialog.resize(450,420)
        form=QFormLayout(dialog);form.setVerticalSpacing(12)
        form.addRow(label('状态变化时只播一次。填 0 则不播；其他编号需先推送。','hint'))
        fields={}
        for k,name in d.STATES:
            n=QSpinBox();n.setRange(0,3000);n.setValue(rows.get(k.lower(),0));fields[k]=n;form.addRow(name,n)
        box=QDialogButtonBox(QDialogButtonBox.StandardButton.Save|QDialogButtonBox.StandardButton.Cancel)
        box.accepted.connect(dialog.accept);box.rejected.connect(dialog.reject);form.addRow(box)
        if not dialog.exec():return
        updates={k:n.value() for k,n in fields.items() if n.value()!=rows.get(k.lower(),0)}
        def work(f):
            for k,n in updates.items():d.set_state_track(f,k,n)
            return '设备已保存不同状态的播报配置。'
        self.task('bind','正在保存状态播报配置…',lambda:d.with_link(t,work))
    def device_action(self,key,s,work):
        t=self.error(self.target)
        if t:self.task(key,'正在发送指令…',lambda:d.with_link(t,lambda f:(work(f),s)[1]))
    def play(self):
        n,g=self.slot.value(),self.gain.value()
        self.device_action('play',f'正在播报音频 {n:04d}。',lambda f:(d.set_gain(f,g),d.play_track(f,n)))
    def stop(self):
        self.device_action('stop','已停止播报。',lambda f:d.require(f,'PCM STOP','OK PCM stopped'))
    def apply_gain(self):
        n=self.gain.value();self.device_action('gain',f'音量已设为 {n} 档；重新开机回到 7 档。',lambda f:d.set_gain(f,n))
    def apply_count(self):
        n=self.count.value();self.device_action('count',f'按键语音数已保存为 {n}。',lambda f:d.set_button_count(f,n))
    def closeEvent(self,e):
        if self.busy:
            self.status.setText('设备操作尚未完成，请稍后再关闭。');e.ignore();return
        self.timer.stop();self.pool.shutdown(wait=False);e.accept()

def main():
    arg=argparse.ArgumentParser();arg.add_argument('--snapshot');opt=arg.parse_args()
    if opt.snapshot:os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
    app=QApplication([sys.argv[0]])
    font_root=Path(os.environ.get('WINDIR','C:/Windows'))/'Fonts'
    family='Microsoft YaHei UI'
    if opt.snapshot:
        for f in ('msyh.ttc','msyhbd.ttc','segoeui.ttf'):
            p=font_root/f
            if p.is_file():
                i=QFontDatabase.addApplicationFont(str(p))
                if f=='msyh.ttc' and i>=0:
                    fs=QFontDatabase.applicationFontFamilies(i)
                    if fs:family=fs[0]
    app.setFont(QFont(family,10));app.setStyleSheet(STYLE)
    w=Window();w.show()
    if opt.snapshot:
        app.processEvents()
        out=w.grab().save(opt.snapshot)
        w.close(); return 0 if out else 1
    return app.exec()
if __name__=='__main__':sys.exit(main())
