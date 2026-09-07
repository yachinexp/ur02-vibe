# -*- coding: utf-8 -*-
"""UR02 Vibe — Ugoos UR02 蓝牙遥控器 vibe coding 助手
版本: v1.2-20260905
模块: 按键映射(HID->SendInput) + 语音桥接(BLE ATVV -> ADPCM -> VB-CABLE)
"""

APP_NAME = "UR02 Vibe 助手"
APP_VERSION = "v1.32-20260907"  # v1.32: BLE 稳定性 —— ①连接/枚举全部包超时(WinRT 调用不可达时永久挂起=重连连不上的真凶) ②服务/特征 UNCACHED 漏读时回退缓存枚举(凑不齐三个 ATVV 特征就无限重连) ③每25秒读电量当 GATT 心跳(防链路空转被掐+顺带刷新电量) ④断连后先等1.5秒给自动恢复机会 | v1.31: 掐流自动重开 | v1.30: 单实例锁+退避
# ② 右上角遥控器电量显示(标准 Battery Service 0x180F/0x2A19, READ+NOTIFY, 实测 97%)
# ③ 语音桥接改成启动自动开启, 去掉开关(少一步操作)
# -------------------------------- v1.25 -------------------------------
# 补点量闭环 + 单帧封顶 80 样本(10ms) + 丢弃第1个测量窗口
# (v1.24 教训: 开流头两秒 BLE 热身, 窗口测到 60ms 间隔 -> 每帧补 236 个 = 一半是合成音,
#  用户听感"爆破音很重"; 而 v1.23 只补 12 点 + 断流渐变反而更干净 -> 超过 10ms 宁可留白)
# 丢包补偿(PLC): 实测遥控器是真8k采样+丢样本(内容基频不随组间隔变),
# 改为按实测组间隔用【基频周期复制】补满真实缺口(43.6ms时补105样本=13ms),
# 播放速率锁标称8k(音高才对), PLL 收窄到±6%只管抖动。
# 离线验证: 43.6ms 断流 16->0 次, 52.9ms 断流 27->0 次, 有声占比 21.2%->25.7%(源26.9%)

# 遥控器蓝牙 MAC (字节正序)
# !! 2026-09-05 实测修正: 文档里的 A4:C1:38:8A:39:5E 是笔误, 真实地址来自
#    BTHLE\DEV_A4C138A9354E (PnP InstanceId) 与注册表 BTHPORT\...\Devices\a4c138a9354e
#    且 WinRT FromBluetoothAddressAsync 实测: A4C138A9354E 成功(name=UR02), 8A395E 返回 null
REMOTE_MAC = "A4:C1:38:A9:35:4E"
REMOTE_ADDR = 0xA4C138A9354E          # WinRT API 用的 48 位整数
# HID 设备路径过滤串 (MAC 小写无冒号)
HID_FILTER = "a4c138a9354e"
# HID VID/PID (实测: VID=0x0508 PID=0x1980)
HID_VID = 0x0508
HID_PID = 0x1980

# ATVV GATT
ATVV_SERVICE = "ab5e0001-5a21-4f05-bc7d-af01f617b664"
ATVV_TX = "ab5e0002-5a21-4f05-bc7d-af01f617b664"
ATVV_AUDIO = "ab5e0003-5a21-4f05-bc7d-af01f617b664"
ATVV_CTRL = "ab5e0004-5a21-4f05-bc7d-af01f617b664"

# 标准 Battery Service —— 2026-09-06 实测枚举确认:
#   UR02 共 7 个 GATT 服务, 其中有 0x180F, 特征 0x2A19 属性 = READ|NOTIFY, 读回 61h = 97%
#   (另有 1800/180A/1812/1920/10203-.../ab5e0001)。支持 NOTIFY, 不用轮询。
BATT_SERVICE = "0000180f-0000-1000-8000-00805f9b34fb"
BATT_LEVEL = "00002a19-0000-1000-8000-00805f9b34fb"

CAPS_QUERY = bytes([0x0A, 0x01, 0x00, 0x00, 0x03, 0x03])
MIC_OPEN = bytes([0x0C, 0x00])
MIC_CLOSE = bytes([0x0D, 0x00])

# UR02 实测键码表 (col02, Consumer Page 16bit usage) —— 2026-09-05 抓包确认
USAGE_NAMES = {
    "0030": "电源",
    "0040": "菜单",
    "0041": "OK/确定",
    "0042": "上",
    "0043": "下",
    "0044": "左",
    "0045": "右",
    "00B5": "下一首",
    "00B6": "上一首",
    "00CD": "播放/暂停",
    "00CF": "语音",
    "00E2": "主页",
    "00E9": "音量+",
    "00EA": "音量-",
    "0196": "浏览器",
    "0221": "搜索",
    "0224": "返回",
}
VOICE_USAGE = "00CF"


def usage_name(code):
    c = (code or "").upper()
    return USAGE_NAMES.get(c, "0x" + c if c else "-")


# AUDIO_START(opcode 0x04) 第 3 字节 = codec: 1=8kHz 2=16kHz
# !! 2026-09-05 底噪排查实锤: UR02 推的是 8000Hz, 不是 16kHz!
#    铁证: 组间隔法 —— 每 128B 一组(6x20B+1x8B), 组平均间隔 32.02ms
#          -> 256 样本/32.02ms = 7995 样本/秒; 按 8k 解码时长 17.1s 与
#          实测推流窗口 17.9s 吻合(按 16k 只有 8.5s 对不上)。
#    之前"16kHz"结论(码率法 11.97s/191744 样本)被 8B 尾包+序号头包误算污染。
#    本机 AUDIO_START 实测是单字节 04(无 codec 字段) -> 无字段时默认 codec=1。
CODEC_RATES = {1: 8000, 2: 8000}
AUDIO_SAMPLE_RATE = 8000
