# -*- coding: utf-8 -*-
"""UR02 BLE 语音引擎 —— 原生 WinRT 实现 (同已验证的 C# Ur02VoiceEngine.cs)

为什么不用 bleak:
  bleak 3.x 在 Windows 上要求目标设备正在广播, 而 UR02 一旦被电脑连上就不再广播,
  结果 BleakClient 直接抛 BleakDeviceNotFoundError。WinRT 的
  BluetoothLEDevice.FromBluetoothAddressAsync 可以直接拿到已连接设备(实测 OK)。

链路:
  FromBluetoothAddressAsync(0xA4C138A9354E)
    -> 服务 ab5e0001 (ATVV)
    -> TX ab5e0002(写命令) / AUDIO ab5e0003(notify 音频) / CTRL ab5e0004(notify 控制)

语音键时序 (2026-09-05 实测, 关键):
  按下语音键 -> 遥控器同时发 HID usage 0x00CF 和 BLE CTRL 08 MIC_BUTTON
             -> 遥控器蓝灯【瞬时】闪灭, 不会等待
             -> 主机必须在几十毫秒内回 0C 00 (实测 +10ms -> +20ms 收到 AUDIO_START)
             -> 遥控器回 04 00 02 00 (codec 0x02 = 16kHz), 音频包涌来
  再按      -> 0D 00 关麦
  能力协商 0A 01 00 00 03 03 实测【非必需】, 直接发 0C 00 就能开麦
"""
import asyncio
import threading

from .consts import (ATVV_AUDIO, ATVV_CTRL, ATVV_SERVICE, ATVV_TX,
                     BATT_LEVEL, BATT_SERVICE,
                     CAPS_QUERY, MIC_CLOSE, MIC_OPEN, REMOTE_ADDR)

try:
    from winrt.windows.devices.bluetooth import BluetoothLEDevice, BluetoothCacheMode
    from winrt.windows.devices.bluetooth.genericattributeprofile import (
        GattClientCharacteristicConfigurationDescriptorValue as CCCD,
    )
    from winrt.windows.storage.streams import DataReader, DataWriter
    _WINRT = True
except Exception as _e:                                   # pragma: no cover
    _WINRT = False
    _WINRT_ERR = _e


class AtvvEngine:
    """后台 asyncio 线程跑 WinRT GATT。

    回调:
      on_audio_adpcm(bytes)  原始 ADPCM 包
      on_ctrl(bytes)         控制通道原始字节 (上层解析 opcode/codec)
      on_status(str)         状态文本
      on_battery(int)        遥控器电量百分比 0~100 (标准 Battery Service 0x180F/0x2A19,
                             NOTIFY 推送; 注意: 这是 WinRT 线程池线程, UI 侧要自己 marshal)
    """

    def __init__(self, on_audio_adpcm, on_ctrl, on_status, on_battery=None):
        self.on_audio = on_audio_adpcm
        self.on_ctrl = on_ctrl
        self.on_status = on_status
        self.on_battery = on_battery or (lambda pct: None)
        self._loop = None
        self._thread = None
        self._running = False
        self._dev = None
        self._ch_tx = None
        self._ch_audio = None
        self._ch_ctrl = None
        self._ch_batt = None             # 标准电量特征 0x2A19
        self._batt_tok = None            # 电量 NOTIFY 的订阅句柄(断线要摘掉, 否则重复叠加)
        self._subscribed = False
        self.connected = False
        self.mic_open = False
        self._fail_count = 0          # 连续断连次数(重连退避用)

    # ---------- 线程/loop 管理 ----------
    def start(self):
        if self._running:
            return
        if not _WINRT:
            self.on_status("ble:WinRT 不可用(%s)" % _WINRT_ERR)
            return
        self._running = True
        self._thread = threading.Thread(target=self._thread_main, daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False
        if self._loop:
            try:
                asyncio.run_coroutine_threadsafe(self._shutdown(), self._loop).result(timeout=5)
            except Exception:
                pass

    def _thread_main(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._run_loop())
        except Exception:
            pass
        finally:
            try:
                self._loop.close()
            except Exception:
                pass

    # ---------- 主循环: 连接-订阅-保活 ----------
    async def _run_loop(self):
        while self._running:
            try:
                self.on_status("ble:连接中...")
                # ⚠️ 2026-09-07 关键修复: 这个 WinRT 调用在遥控器不可达时既不返回也不报错,
                # 会永久挂起 —— 有重连循环却永远连不上的真凶。实测可达时只要 27~35ms。
                try:
                    dev = await asyncio.wait_for(
                        BluetoothLEDevice.from_bluetooth_address_async(REMOTE_ADDR),
                        timeout=10.0)
                except asyncio.TimeoutError:
                    raise RuntimeError("连接超时10秒(遥控器不可达/正在重启), 换个时间再试")
                if dev is None:
                    raise RuntimeError("返回 null(已取消配对?请到 Windows 蓝牙设置重新添加 Ugoos Remote)")
                self._dev = dev
                self.connected = True
                self.on_status("ble:已连接 %s" % (dev.name or "UR02"))

                # 2026-09-07: 刷机后遥控器重启, Windows 会缓存旧 GATT 服务表(OTA 模式/旧固件),
                # 导致枚举不到 ATVV 服务或订阅后马上断。强制 UNCACHED 从设备重新读。
                res = None
                try:
                    res = await asyncio.wait_for(
                        dev.get_gatt_services_with_cache_mode_async(BluetoothCacheMode.UNCACHED),
                        timeout=15.0)
                except asyncio.TimeoutError:
                    self.on_status("ble:服务枚举(UNCACHED)超时, 回退缓存枚举")
                except Exception:
                    res = None
                if res is None:
                    try:
                        res = await dev.get_gatt_services_async()
                    except Exception as e:
                        raise RuntimeError("服务枚举失败 %s" % _short(e))
                # 枚举到 0 个服务 = 拿到的是 Windows 缓存的假设备对象, 实际没连上
                if len(res.services) == 0:
                    raise RuntimeError("枚举到0个服务=没真正连上(遥控器休眠/已被电视盒子抢走)")

                def _pick(services):
                    a = b = None
                    for s in services:
                        u = str(s.uuid).lower()
                        if u == ATVV_SERVICE:
                            a = s
                        elif u == BATT_SERVICE:
                            b = s
                    return a, b

                svc, batt_svc = _pick(res.services)
                if svc is None:
                    # UNCACHED 漏了 -> 回退缓存枚举再找一次
                    try:
                        res2 = await dev.get_gatt_services_async()
                        svc2, batt2 = _pick(res2.services)
                        svc = svc or svc2
                        batt_svc = batt_svc or batt2
                    except Exception:
                        pass
                if svc is None:
                    raise RuntimeError("未找到 ATVV 服务(遥控器不在语音模式?)")

                # ⚠️ 2026-09-07 修复"连上就断/反复重连连不上": UNCACHED 是强制现场去设备
                # 读特征表, 设备一忙(刚连上/正在处理按键)就会漏读几个 -> 三个特征凑不齐 ->
                # 判"特征不全" -> 断开重连 -> 再漏读, 无限循环(v1.27 用缓存枚举时从不出这毛病)。
                # 对策: UNCACHED 优先(防刷机后的假缓存表), 凑不齐就回退缓存枚举取并集。
                chs = {}
                try:
                    cres = await asyncio.wait_for(
                        svc.get_characteristics_with_cache_mode_async(BluetoothCacheMode.UNCACHED),
                        timeout=10.0)
                    chs = {str(c.uuid).lower(): c for c in cres.characteristics}
                except Exception as e:
                    self.on_status("ble:特征枚举(UNCACHED)失败 %s, 回退缓存枚举" % _short(e))
                if not all(k in chs for k in (ATVV_TX, ATVV_AUDIO, ATVV_CTRL)):
                    try:
                        cres2 = await svc.get_characteristics_async()
                        for c in cres2.characteristics:
                            chs.setdefault(str(c.uuid).lower(), c)
                    except Exception:
                        pass
                self._ch_tx = chs.get(ATVV_TX)
                self._ch_audio = chs.get(ATVV_AUDIO)
                self._ch_ctrl = chs.get(ATVV_CTRL)
                if not (self._ch_tx and self._ch_audio and self._ch_ctrl):
                    self.on_status("ble:ATVV 服务内实际特征: %s"
                                   % (", ".join(sorted(chs)) or "(空)"))
                    raise RuntimeError("ATVV 特征不全")

                for name, c, h in (("ctrl", self._ch_ctrl, self._on_ctrl_evt),
                                   ("audio", self._ch_audio, self._on_audio_evt)):
                    st = await c.write_client_characteristic_configuration_descriptor_async(CCCD.NOTIFY)
                    if int(st) != 0:
                        self.on_status("ble:%s 订阅返回 %s" % (name, st))
                    c.add_value_changed(h)
                self._subscribed = True
                self.on_status("ble:订阅完成(语音桥接就绪)")
                self._fail_count = 0   # 连上了, 重置断连计数

                # 电量(可选): 没有这个服务也要能正常用语音, 所以整段包 try
                if batt_svc is not None:
                    try:
                        try:
                            bcres = await batt_svc.get_characteristics_with_cache_mode_async(BluetoothCacheMode.UNCACHED)
                        except Exception:
                            bcres = await batt_svc.get_characteristics_async()
                        for c in bcres.characteristics:
                            if str(c.uuid).lower() == BATT_LEVEL:
                                self._ch_batt = c
                                break
                        if self._ch_batt is not None:
                            r = await self._ch_batt.read_value_async()
                            b = _read_ibuffer(r.value)
                            if b:
                                self.on_battery(int(b[0]))
                            await self._ch_batt.write_client_characteristic_configuration_descriptor_async(CCCD.NOTIFY)
                            self._batt_tok = self._ch_batt.add_value_changed(self._on_batt_evt)
                    except Exception as e:
                        self.on_status("ble:电量读取失败 %s" % _short(e))
                else:
                    self.on_status("ble:无电量服务(0x180F), 不显示电量")

                # 保活: 等停止或断开
                self._mic_keepalive = 0
                self._hb = 0
                while self._running:
                    await asyncio.sleep(1.0)
                    try:
                        if int(dev.connection_status) != 1:
                            await asyncio.sleep(0.3)   # 去抖: 避免瞬时状态抖动误判断连
                            if int(dev.connection_status) != 1:
                                # 2026-09-07: 实测遥控器重启后约 0.5 秒会自己连回来。
                                # 先等 1.5 秒再进重连流程, 免得在它恢复途中把连接又掐掉。
                                self.on_status("ble:连接中断, 等1.5秒看能否自动恢复")
                                await asyncio.sleep(1.5)
                                break
                    except Exception:
                        break
                    # 抗掐流保活(2026-09-07): 固件 20 秒掐流上限无法靠刷补丁去掉
                    # (Telink boot 校验会把改过的固件回退原厂), 所以从 PC 侧每 15 秒
                    # 重发一次开麦命令, 在遥控器计时器到点前归零 -> 等于永不掐流。
                    if self.mic_open:
                        self._mic_keepalive += 1
                        if self._mic_keepalive >= 15:
                            self._mic_keepalive = 0
                            try:
                                await self._write(MIC_OPEN)
                                self.on_status("ble:麦克风保活重发(抗 20s 掐流)")
                            except Exception:
                                pass
                    # 2026-09-07: GATT 心跳。旧版每秒只"看一眼"连接状态、从不主动跟遥控器
                    # 说话, 链路长期只有空包 -> 被某一侧判定超时掐断(用户反馈"一直在用也会
                    # 自己断")。每 25 秒读一次电量特征, 既是真实数据往来, 又顺带刷新电量显示。
                    self._hb += 1
                    if self._hb >= 25:
                        self._hb = 0
                        try:
                            if self._ch_batt is not None:
                                r = await asyncio.wait_for(
                                    self._ch_batt.read_value_async(), timeout=5.0)
                                b = _read_ibuffer(r.value)
                                if b:
                                    self.on_battery(int(b[0]))
                        except Exception:
                            pass
            except Exception as e:
                if self._running:
                    self.on_status("ble:%s, 3秒后重试" % _short(e))
            finally:
                await self._cleanup()
                if self._running:
                    self._fail_count += 1
                    # 指数退避: 3 -> 6 -> 12 -> 24s(封顶), 避免死循环狂连把日志刷爆
                    wait = min(3.0 * (2 ** min(self._fail_count - 1, 3)), 30.0)
                    if self._fail_count >= 4:
                        self.on_status("ble:反复断连(已%d次)→可能遥控器连了其它设备(TV/盒子), 请先断开再试" % self._fail_count)
                    else:
                        self.on_status("ble:已断开, %d秒后重连(第%d次)" % (int(wait), self._fail_count))
                    await asyncio.sleep(wait)

    async def _cleanup(self):
        if self.connected:
            self.connected = False
            self.mic_open = False
            self._subscribed = False
            self.on_status("ble:已断开")
        if self._ch_batt is not None:
            try:
                if self._batt_tok is not None:
                    self._ch_batt.remove_value_changed(self._batt_tok)
            except Exception:
                pass
            self._ch_batt = None
            self._batt_tok = None
        self._ch_tx = self._ch_audio = self._ch_ctrl = None
        d, self._dev = self._dev, None
        if d is not None:
            try:
                d.close()
            except Exception:
                pass

    async def _shutdown(self):
        if self.mic_open:
            await self._write(MIC_CLOSE)
            self.mic_open = False
        self._running = False
        await self._cleanup()

    # ---------- notify 回调 (WinRT 线程池) ----------
    def _on_audio_evt(self, sender, args):
        if not self._running:
            return
        try:
            b = _read_ibuffer(args.characteristic_value)
            if b:
                self.on_audio(b)
        except Exception:
            pass

    def _on_ctrl_evt(self, sender, args):
        try:
            b = _read_ibuffer(args.characteristic_value)
            if b:
                self.on_ctrl(b)
        except Exception:
            pass

    def _on_batt_evt(self, sender, args):
        """电量 NOTIFY (标准 0x2A19: 1 字节, 0~100; 255 = 未知)"""
        if not self._running:
            return
        try:
            b = _read_ibuffer(args.characteristic_value)
            if b:
                v = int(b[0])
                if 0 <= v <= 100:
                    self.on_battery(v)
        except Exception:
            pass

    # ---------- 公开命令 ----------
    def open_mic(self):
        if not (self._loop and self.connected and self._ch_tx):
            self.on_status("ble:未连接, 开麦失败")
            return False
        asyncio.run_coroutine_threadsafe(self._open_mic(), self._loop)
        return True

    def close_mic(self):
        if not (self._loop and self._ch_tx):
            return False
        asyncio.run_coroutine_threadsafe(self._close_mic(), self._loop)
        return True

    async def _open_mic(self):
        """开麦。

        2026-09-05 实测: 遥控器语音键是【瞬时】的 —— 按下后蓝灯立刻闪灭,
        主机必须在几十毫秒内回 0C 00 (实测 +10ms 发出 -> +20ms 收到 AUDIO_START)。
        所以这里【不做】能力协商、也不 sleep: 能力协商实测并非必需, 直接发 0C 00 即可。
        """
        try:
            await self._write(MIC_OPEN)
            self.mic_open = True
            self.on_status("ble:开麦命令已发送")
        except Exception as e:
            self.on_status("ble:开麦失败 %s" % _short(e))

    async def _close_mic(self):
        try:
            await self._write(MIC_CLOSE)
            self.mic_open = False
            self.on_status("ble:关麦命令已发送")
        except Exception as e:
            self.on_status("ble:关麦失败 %s" % _short(e))

    # ---------- 掐流自动重开 (v1.31) ----------
    def reopen_mic(self):
        """掐流后自动重开麦: 先 0D 关再 0C 开。

        2026-09-07 实测: 固件 20s 掐流 = 音频帧计数(562 帧)到点直接断流【不发任何命令】,
        推流中重复发 0C 00 被从机忽略(保活无效) —— 只有掐流后从机回到空闲态,
        此时 0D->0C 的状态机路径才有效。由 UI 看门狗检测到掐流时调用。
        """
        if not (self._loop and self.connected and self._ch_tx):
            self.on_status("ble:未连接, 自动重开失败")
            return False
        asyncio.run_coroutine_threadsafe(self._reopen_mic(), self._loop)
        return True

    async def _reopen_mic(self):
        try:
            await self._write(MIC_CLOSE)
            await asyncio.sleep(0.5)
            await self._write(MIC_OPEN)
            self.mic_open = True
            self.on_status("ble:开麦命令已发送(掐流自动重开)")
        except Exception as e:
            self.on_status("ble:自动重开失败 %s" % _short(e))
            self.mic_open = False

    async def _write(self, payload: bytes):
        w = DataWriter()
        w.write_bytes(payload)
        r = await self._ch_tx.write_value_with_result_async(w.detach_buffer())
        if int(r.status) != 0:
            raise RuntimeError("写入返回 %s" % r.status)
        return r


def _read_ibuffer(buf) -> bytes:
    r = DataReader.from_buffer(buf)
    n = r.unconsumed_buffer_length
    if not n:
        return b""
    out = bytearray(n)
    r.read_bytes(out)
    return bytes(out)


def _short(e) -> str:
    t = type(e).__name__
    msg = str(e)
    return t if not msg else "%s(%s)" % (t, msg[:60])
