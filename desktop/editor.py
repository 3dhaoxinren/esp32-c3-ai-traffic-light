"""Waveform editor for input or USB-read audio. All cuts are sample-relative."""
from array import array
from pathlib import Path
from tempfile import TemporaryDirectory
import sys
from PySide6.QtCore import Qt,Signal,QRectF
from PySide6.QtGui import QColor,QPainter,QPen
from PySide6.QtWidgets import (QComboBox,QDialog,QDoubleSpinBox,QGridLayout,QHBoxLayout,QLabel,
  QListWidget,QPushButton,QSpinBox,QVBoxLayout,QWidget)
try:
    from . import device as d
except ImportError:
    import device as d

class Timeline(QWidget):
    selection=Signal(float,float)
    def __init__(self,source):
        super().__init__()
        a=array('h');a.frombytes(source)
        if sys.byteorder!='little':a.byteswap()
        self.duration=len(a)/d.RATE
        step=max(1,len(a)//780)
        self.peaks=[(min(a[i:i+step]),max(a[i:i+step])) for i in range(0,len(a),step)]
        self.s0=0;self.s1=min(self.duration,32);self.anchor=None
        self.setMinimumHeight(152)
        self.setCursor(Qt.CursorShape.CrossCursor)
    def paintEvent(self,event):
        paint=QPainter(self);w=self.width();h=self.height()
        paint.fillRect(self.rect(),QColor('#101b1d'))
        paint.fillRect(QRectF(w*self.s0/self.duration,0,w*(self.s1-self.s0)/self.duration,h),QColor('#244b3d'))
        mid=h*.47
        paint.setPen(QPen(QColor('#40544f'),1));paint.drawLine(0,int(mid),w,int(mid))
        paint.setPen(QPen(QColor('#a9e8c4'),1.2))
        for i,(lo,hi) in enumerate(self.peaks):
            x=int((i+0.5)*w/len(self.peaks))
            paint.drawLine(x,int(mid-max(0,hi)/32768*(h*.44)),x,int(mid-min(0,lo)/32768*(h*.44)))
        paint.setPen(QColor('#bacac0'));paint.drawText(8,h-8,'0.00 秒')
        paint.drawText(w-108,h-8,f'{self.duration:.3f} 秒')
    def timepos(self,e):return self.duration*max(0,min(1.,e.position().x()/self.width()))
    def mousePressEvent(self,e):self.anchor=self.timepos(e)
    def mouseMoveEvent(self,e):
        if self.anchor is not None:
            a,b=sorted((self.anchor,self.timepos(e)));self.selection.emit(a,b)
    def mouseReleaseEvent(self,e):
        if self.anchor is not None:self.mouseMoveEvent(e);self.anchor=None
    def limits(self,a,b):
        self.s0=min(a,b);self.s1=max(a,b);self.update()

class Editor(QDialog):
    def __init__(self,parent,data,slot,segments=None):
        super().__init__(parent); self.source=data; self.queue=list(segments or ())
        self.temp=TemporaryDirectory();self.setWindowTitle('剪辑与分段');self.resize(830,610)
        layout=QVBoxLayout(self);layout.setContentsMargins(22,18,22,18)
        self.wave=Timeline(data)
        layout.addWidget(QLabel('拖动波形选择起点和终点，也可以精确输入时间。每段最多 32 秒。'))
        layout.addWidget(self.wave)
        grid=QGridLayout();layout.addLayout(grid)
        self.a=QDoubleSpinBox();self.b=QDoubleSpinBox()
        for j,w in enumerate((self.a,self.b)):
            w.setDecimals(3);w.setRange(0,self.wave.duration);w.setSingleStep(.1);w.setSuffix(' 秒')
            grid.addWidget(QLabel(('起点','终点')[j]),0,j*2);grid.addWidget(w,0,j*2+1)
            w.valueChanged.connect(self.changed)
        self.b.setValue(min(32,self.wave.duration));self.wave.selection.connect(self.selected)
        self.slot=QSpinBox();self.slot.setRange(1,3000);self.slot.setValue(slot)
        self.state=QComboBox();self.state.addItem('不绑定状态','')
        for k,t in d.STATES:self.state.addItem(t,k)
        grid.addWidget(QLabel('设备编号'),1,0);grid.addWidget(self.slot,1,1);grid.addWidget(QLabel('任务状态'),1,2);grid.addWidget(self.state,1,3)
        row=QHBoxLayout();layout.addLayout(row)
        for text,cb in [('试听选段',self.preview),('停止试听',self.stop),('加入分段',self.add_cut),('移除所选',self.remove)]:
            p=QPushButton(text);p.clicked.connect(cb);row.addWidget(p)
        self.list=QListWidget();layout.addWidget(self.list,1)
        self.note=QLabel('分段保存到同一音频源下；点「推送」时才写到设备。');self.note.setWordWrap(True);layout.addWidget(self.note)
        row=QHBoxLayout();layout.addLayout(row)
        cancel=QPushButton('取消');cancel.clicked.connect(self.reject);row.addWidget(cancel)
        save=QPushButton('完成剪辑');save.setProperty('kind','primary');save.clicked.connect(self.save);row.addWidget(save)
        self.finished.connect(self.finish_editor)
        self.update_queue()
    def selected(self,a,b):
        self.a.setValue(a);self.b.setValue(b);self.changed()
    def changed(self):self.wave.limits(self.a.value(),self.b.value())
    def current(self):
        end=len(self.source)//2
        a=min(end,round(self.a.value()*d.RATE));b=min(end,round(self.b.value()*d.RATE))
        if abs(b-end)<=8:b=end
        s=d.Segment(a,b,self.slot.value(),self.state.currentData())
        s.audio(self.source)
        return s
    def update_queue(self):
        self.list.clear()
        for s in self.queue:
            status=dict(d.STATES).get(s.state,'未绑定')
            self.list.addItem(f'{s.start/d.RATE:06.3f} – {s.end/d.RATE:06.3f} 秒    →    {s.slot:04d}    {status}')
    def add_cut(self):
        try:
            s=self.current()
            if any(e.slot==s.slot for e in self.queue):raise ValueError('这一编号已经在列表中。')
            if s.state and any(e.state==s.state for e in self.queue):raise ValueError('同批不能绑定两次同一个状态。')
            self.queue.append(s);self.update_queue()
            self.note.setText(f'已添加 {len(self.queue)} 段。')
            self.slot.setValue(min(3000,s.slot+1));self.state.setCurrentIndex(0)
        except ValueError as ex:self.note.setText(str(ex))
    def remove(self):
        i=self.list.currentRow()
        if i>=0:self.queue.pop(i);self.update_queue();self.note.setText(f'剩余 {len(self.queue)} 段。')
    def preview(self):
        try:
            import winsound
            self.stop()
            path=Path(self.temp.name)/'selection.wav'
            d.savewav(path,self.current().audio(self.source))
            winsound.PlaySound(str(path),winsound.SND_ASYNC|winsound.SND_FILENAME|winsound.SND_NODEFAULT)
            self.note.setText('正在用电脑扬声器试听这一段。')
        except Exception as ex:self.note.setText(str(ex))
    def stop(self):
        if sys.platform=='win32':
            import winsound;winsound.PlaySound(None,0)
    def finish_editor(self,_):
        self.stop()
        try:self.temp.cleanup()
        except OSError:pass
    def save(self):
        if not self.queue:self.add_cut()
        if self.queue:self.accept()
