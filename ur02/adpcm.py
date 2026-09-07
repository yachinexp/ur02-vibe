# -*- coding: utf-8 -*-
"""IMA/DVI 4-bit ADPCM 解码器 (ATVV: 高 nibble 先出, 8kHz)
移植自已验证的 C# Adpcm.cs (真机实测可听懂人声)。
状态跨 notify 包保持。
"""

STEP_TABLE = [
    7, 8, 9, 10, 11, 12, 13, 14, 16, 17, 19, 21, 23, 25, 28, 31, 34, 37, 41, 45,
    50, 55, 60, 66, 73, 80, 88, 97, 107, 118, 130, 143, 157, 173, 190, 209, 230,
    253, 279, 307, 337, 371, 408, 449, 494, 544, 598, 658, 724, 796, 876, 963,
    1060, 1166, 1282, 1411, 1552, 1707, 1878, 2066, 2272, 2499, 2749, 3024, 3327,
    3660, 4026, 4428, 4871, 5358, 5894, 6484, 7132, 7845, 8630, 9493, 10442,
    11487, 12635, 13899, 15289, 16818, 18500, 20350, 22385, 24623, 27086, 29794,
    32767,
]
INDEX_TABLE = [-1, -1, -1, -1, 2, 4, 6, 8]

_CLAMP = (2 ** 15) - 1


class ImaAdpcmDecoder:
    def __init__(self):
        self.predictor = 0
        self.step_index = 0

    def reset(self, predictor=0, step_index=0):
        self.predictor = max(-32768, min(32767, predictor))
        self.step_index = max(0, min(88, step_index))

    def decode_nibble(self, nib):
        step = STEP_TABLE[self.step_index]
        diff = step >> 3
        if nib & 1:
            diff += step >> 2
        if nib & 2:
            diff += step >> 1
        if nib & 4:
            diff += step
        if nib & 8:
            self.predictor -= diff
        else:
            self.predictor += diff
        self.predictor = max(-32768, min(_CLAMP, self.predictor))
        self.step_index = max(0, min(88, self.step_index + INDEX_TABLE[nib & 7]))
        return self.predictor

    def decode(self, data):
        """解码一批字节 -> 16bit PCM 样本 list (每字节 2 样本, 高 nibble 先)"""
        out = [0] * (len(data) * 2)
        i = 0
        for b in data:
            out[i] = self.decode_nibble(b >> 4)
            i += 1
            out[i] = self.decode_nibble(b & 0x0F)
            i += 1
        return out

    def decode_with_state(self, data, predictor, step_index):
        """用外部给的状态(每帧帧头自带的)重启解码 -> 16bit PCM 样本 list。

        2026-09-05 实测: 固件每帧 6 字节头部 [3:5]=预测值 [5]=步长索引, 写在调用
        编码器之前 → 帧头 = 本帧编码"前"的状态。每帧用它重启, 而不是把上一帧算
        完的状态接力下去(否则每帧末尾 1~3 个未发采样点的误差逐帧累积, 步长档位被
        带飞, 预测值撞 ±32767 → 削顶 + 满刻度低频漂移, 听感"沙沙/噼啪")。
        返回的是本帧解出的 244 个采样点 (int16 list)。
        """
        self.reset(predictor, step_index)
        return self.decode(data)
