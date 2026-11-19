"""Voice synthesis is opt-in per request. Only neural profiles need the Internet."""
import asyncio
import subprocess
import sys

import device

VOICES={
    'xiaoxiao':('zh-CN-XiaoxiaoNeural','晓晓 · 自然女声'),
    'xiaoyi':('zh-CN-XiaoyiNeural','晓伊 · 轻快女声'),
    'yunxi':('zh-CN-YunxiNeural','云希 · 年轻男声'),
    'yunyang':('zh-CN-YunyangNeural','云扬 · 沉稳男声'),
}

async def neural_mp3(text,voice,rate):
    try:import edge_tts
    except ImportError as exc:raise RuntimeError('在线语音组件未安装，请安装新版客户端依赖。') from exc
    pcm=bytearray()
    stream=edge_tts.Communicate(text,voice=voice,rate=f'{rate:+d}%',connect_timeout=8,receive_timeout=12)
    async for part in stream.stream():
        if part['type']=='audio':
            pcm.extend(part['data'])
            if len(pcm)>4*1024*1024:raise RuntimeError('在线语音超过允许长度。')
    return bytes(pcm)

def synthesize(text,profile='local',rate=0):
    if not isinstance(text,str) or not text.strip() or len(text)>500:
        raise ValueError('请输入 1–500 字播报文本。')
    if not isinstance(profile,str) or profile not in ('local',*VOICES):
        raise ValueError('请选择有效的语音包。')
    if type(rate) is not int or not -25<=rate<=25:raise ValueError('语速须在 -25% 到 +25% 之间。')
    if profile=='local':
        try:return device.speech(text,rate=rate),'Windows 本机语音'
        except subprocess.TimeoutExpired as exc:raise RuntimeError('本机语音合成超时，请缩短文字后重试。') from exc
    voice,label=VOICES[profile]
    try:import miniaudio
    except ImportError as exc:raise RuntimeError('在线语音解码组件未安装，请安装新版客户端依赖。') from exc
    try:
        mp3=asyncio.run(asyncio.wait_for(neural_mp3(text,voice,rate),timeout=30))
    except RuntimeError:
        raise
    except Exception as exc:
        raise RuntimeError('在线语音暂不可用。请检查网络、换个音色，或切回 Windows 离线语音。') from exc
    if not mp3:raise RuntimeError('在线语音没有返回音频，请更换音色或稍后再试。')
    try:
        audio=miniaudio.decode(mp3,output_format=miniaudio.SampleFormat.SIGNED16,nchannels=1,sample_rate=device.RATE)
    except (ValueError,miniaudio.DecodeError) as exc:
        raise RuntimeError('在线语音没有返回可解码的音频。') from exc
    if sys.byteorder!='little':audio.samples.byteswap()
    data=audio.samples.tobytes()
    if not 0<len(data)<=device.MAX_SOURCE:raise ValueError('生成内容过长，请拆成多次。')
    return data,label
