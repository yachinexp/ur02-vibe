# UR02 Vibe

UR02 蓝牙遥控器按键映射 + 麦克风桥接工具（Windows 桌面端）。

## 功能概述

- 蓝牙遥控器（UR02）按键映射 / 重映射
- 麦克风音频桥接与音频输出处理
- 配套 Windows 中文图形界面

> 具体功能与实现以源码为准。

## 目录结构

| 路径 | 说明 |
|------|------|
| `ur02/` | 主程序包（核心源码） |
| `assets/` | 资源文件（如遥控器图标 `remote.png`） |
| `profiles/` | 按键映射配置文件（`.set`） |
| `UR02-*.spec` | PyInstaller 打包配置 |

## 环境要求

- Windows 10 / 11
- Python 3.12

## 授权

[MIT License](LICENSE) © 2026 yachinexp
