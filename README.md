# 冒险岛怀旧服国服 · 自动练级工具

基于计算机视觉的冒险岛怀旧服（国服）自动练级脚本。不读取游戏内存，只截取游戏窗口画面、识别怪物与角色位置，再模拟键盘操作控制角色。

> 📦 **不想装 Python？** 直接下载免安装版（Windows 64 位，解压即用）：
> **[⬇ MapleStoryAutoLevelUp-CN v1.0](../../releases/latest)**
>
> 免安装包是 PyInstaller 打包的 `--onedir` 目录，**必须整个文件夹解压后使用**，不要只拿 exe。

> 本项目是 [MapleStoryAutoLevelUp](https://github.com/kenyu910645/MapleStoryAutoLevelUp)（台服 Artale 版，MIT）的**国服怀旧服 fork**。
> 国服客户端是 Unity 重制版，与台服差异较大，本 fork 做了针对性改造，详见下文「与上游的差异」。

## 两种使用方式

| 方式 | 适合谁 | 怎么做 |
|---|---|---|
| **免安装包** | 只想用，不想碰代码 | 去 [Releases](../../releases/latest) 下载 zip，解压后双击 `冒险岛自动练级.exe` |
| **源码运行** | 想改代码 / 自己打包 | 见下文「环境要求」与「开发者：源码运行」 |

> 仓库本身**只放源码**，不放 exe —— PyInstaller 的成品有 250MB，超过 GitHub 单文件 100MB 硬限，且二进制进 git 历史会让仓库迅速膨胀。成品一律走 Release 附件。

## 与上游的差异

国服客户端与台服 Artale 不是同一套东西，以下改造不是可选优化，而是必须：

| 项 | 台服上游 | 国服本 fork |
|---|---|---|
| 客户端 | 原版 2D 客户端 | **Unity 重制版** |
| 角色定位 | 可用组队血条 | 组队血条**不可用**，改用**角色名标签**定位 |
| 符文系统 | 有 | **无**，相关逻辑已删 |
| 创角掷骰 | 有 `AutoDiceRoller` | **无**，工具已删除 |
| 地图定位 | 全屏截图方案 | 小地图方案，旧实现已整批删除 |
| 反作弊 | — | 客户端带**反挂机（AntiMacro）验证**与三层反作弊 |

## 环境要求

- Windows 10 / 11（**不支持虚拟机**，也不支持 macOS）
- Python 3.12
- 依赖见 `requirements.txt`：

```bash
pip install -r requirements.txt
```

其中 `interception-python` 用于内核级按键模拟，需要管理员权限运行；`windows-capture` 用于抓帧。

## 快速开始

1. 启动国服怀旧服客户端，切换到**窗口模式**
2. 打开游戏左上角的**小地图**（角色定位依赖它）
3. 把角色走到要挂机的地图
4. 双击 `启动界面.bat`，在主界面「配置方案」里调好按键与地图
5. 按 `Start` 或 `F1` 开始

界面没反应时双击 `诊断启动.bat`，它用带控制台的 Python 启动，报错会留在窗口里。

> **首次使用需要自己做模板**：`monster/`（怪物模板）、`minimaps/`（地图与路线）、`nametag/`（角色名标签）三个目录出厂是空的。上游台服的模板与国服画面不通用，必须用本项目的工具从国服真机截图重做。界面上的 F2（标定名字）/ F3（截怪）/ F4（录路线）就是干这个的。

### 启动脚本说明

| 文件 | 用途 |
|---|---|
| `启动界面.bat` | 正常启动界面（无控制台窗口） |
| `诊断启动.bat` | 带控制台启动，用于看报错 |
| `停止界面.bat` | 关闭界面 |

三个 bat 都不写死 Python 路径，按以下顺序自动查找：环境变量 `MAPLEBOT_PY` → PATH 里的 `pythonw.exe` / `python.exe`。想固定用某个虚拟环境：

```bat
setx MAPLEBOT_PY "D:\你的venv\Scripts\pythonw.exe"
```

## 开发者：源码运行

```bash
python -m src.main          # 带界面（推荐）
```

不带界面：

```bash
python -m src.engine.MapleStoryAutoLevelUp
python -m src.engine.MapleStoryAutoLevelUp --cfg my_config   # 指定配置
python -m src.engine.MapleStoryAutoLevelUp --disable_viz     # 关掉调试视窗
python -m src.engine.MapleStoryAutoLevelUp --record          # 录制调试视窗
```

快捷键：`F1` 暂停/继续，`F2` 截图（存 `screenshot/`），`F12` 退出。

## 打包分发

| 脚本 | 产物 | 说明 |
|---|---|---|
| `打包_用户版.bat` | `dist\冒险岛自动练级\` | **只含出厂默认配置**，可直接发给别人 |
| `打包_个人版.bat` | `dist\冒险岛自动练级-个人版\` | 含你自己的配置/名字模板/地图路线 + tools 源码，**仅供自用，不要外发** |

产物是 `--onedir` 目录，**整个文件夹拷走才能用**（只拷 exe 会因为找不到资源目录而失败，原因见 `src/utils/paths.py`）。

## 配置体系

- `config/config_default.yaml` —— 全部默认值与注释说明（**基准**，一般不改）
- `config/config_custom.yaml` —— 用户配置的出厂回退值
- `config/config_data.yaml` —— 地图 / 怪物登记表（录完路线、截完怪会自动写入）
- 主界面「配置方案」另存的自定义方案（如 `config/你的方案名.yaml`）—— **个人数据，不要提交到仓库**，`.gitignore` 已按规则挡住

配置的加载语义是**深合并**：`config_default.yaml` 铺底，你的方案覆盖改过的键。所以方案文件里只写你改过的项就够了。

## 工具箱

| 工具 | 命令 | 用途 |
|---|---|---|
| 路线录制 | `python -m tools.routeRecorder --new_map <地图目录名>` | 录制巡逻路线与回正线 |
| 怪物模板 | `python tools/mob_maker.py` | 从 maplestory.io 下载怪物图（注意：**上游数据，国服不一定通用**） |
| 名字标定 | `python -m tools.calibrate_nametag` | 标定角色名标签模板 |
| 模板查重 | `python -m tools.mob_template_qa` | 检查怪物模板是否重复 |
| 诊断包 | `python -m tools.diagnose` | 收集环境与日志用于排查 |

路线录制的操作键位：

| 键 | 功能 |
|---|---|
| `F1` | 暂停 / 继续记录 |
| `F2` | 截图（存 `screenshot/`） |
| `F3` | 保存当前路线并开始新的一段 |
| `F4` | 把当前扫描的地图存为 `map.png` |

录完的原始路线通常不够好，需要用画图工具微调；`tools/homeRouteDrawer.py` 提供了界面化的回正线绘制。

## 自检脚本

`tools/verify_*.py` 是一批离线自检，不开游戏也能验证核心逻辑：

```bash
python -m tools.verify_load_config        # 配置加载链路（异常不静默）
python -m tools.verify_navigation         # 导航与路线
python -m tools.verify_home_route         # 回正线
python -m tools.verify_recorder_action    # 录制器动作读取
```

## 许可与致谢

- 本项目基于 [kenyu910645/MapleStoryAutoLevelUp](https://github.com/kenyu910645/MapleStoryAutoLevelUp)（MIT License，Copyright (c) 2025 Ken Yu）修改而来，遵循同一 MIT 协议。
- 原项目的 Discord 群与赞助链接属于上游作者，本 fork 不继承。
- 上游 README 中的台服数据与说明已不适用于国服，请以本文件为准。

## 免责声明

本项目仅供**个人娱乐与技术学习**使用，不得用于商业用途。使用自动化脚本可能违反游戏服务条款并导致账号被封禁，**风险自负**。作者不对任何账号处罚或数据损失负责。
