"""
统一路径解析 —— 让「源码运行」与「PyInstaller 打包运行」共用同一套资源定位规则。

## 要解决的问题

项目原先到处写相对路径（`"config/config_default.yaml"`、`"nametag/xx.png"`），
它们被解释为「相对于进程当前工作目录（CWD）」。而 CWD 并不可靠：

- 从资源管理器双击 exe → CWD = exe 所在目录；若 exe 在 `dist/` 而资源在项目根，就找不到；
- 用快捷方式 / 计划任务 / 被其它程序拉起 → CWD 可能是 `C:\\Windows\\System32`；
- PyInstaller `--onefile` → exe 落在 `dist/`，与项目资源目录天然分离。

因此统一改为「基于应用根目录（APP_ROOT）解析」，与 CWD 解耦。

## 应用根目录的定义

| 运行方式 | APP_ROOT |
|---|---|
| 打包后（`sys.frozen`） | 可执行文件所在目录（资源随 exe 一起分发） |
| 源码运行 | 仓库根目录（`src/` 的上一级） |

## 用法

```python
from src.utils.paths import resource_path, ensure_dir

cfg  = load_yaml("config/config_default.yaml")   # 内部已自动转绝对路径
os.makedirs(ensure_dir("screenshot"), exist_ok=True)
```

约定：底层 IO 函数（`load_yaml` / `save_yaml` / `load_image`）内部已调用
`resource_path`，业务代码继续写相对路径即可；只有 `cv2.imwrite`、`glob.glob`
这类直接调用的地方需要显式包一层。
"""
import os
import sys

# 是否运行在 PyInstaller 打好的包里
IS_FROZEN = bool(getattr(sys, "frozen", False))


def _detect_app_root():
    if IS_FROZEN:
        # 打包后：以 exe 所在目录为根，资源（config/ nametag/ monster/ ... ）随 exe 分发
        return os.path.dirname(os.path.abspath(sys.executable))
    # 源码运行：本文件位于 <root>/src/utils/paths.py，上溯三层即仓库根
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


APP_ROOT = _detect_app_root()


def resource_path(path):
    """
    把项目内的相对路径解析为绝对路径。

    - 已是绝对路径：原样返回（兼容命令行传绝对路径的场景）
    - 相对路径：相对 APP_ROOT 解析（正反斜杠都接受）
    - None / 空串：原样返回，交由调用方处理
    """
    if path is None:
        return path
    path = str(path)
    if not path:
        return path
    if os.path.isabs(path):
        return os.path.normpath(path)
    return os.path.normpath(os.path.join(APP_ROOT, path.replace("\\", "/").lstrip("/")))


def ensure_dir(path):
    """确保目录存在，返回其绝对路径"""
    abs_path = resource_path(path)
    os.makedirs(abs_path, exist_ok=True)
    return abs_path


def ensure_parent(path):
    """确保文件的父目录存在，返回该文件的绝对路径"""
    abs_path = resource_path(path)
    parent = os.path.dirname(abs_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    return abs_path


def describe():
    """一行描述当前运行位置，用于启动日志排查"""
    mode = "打包运行" if IS_FROZEN else "源码运行"
    return f"应用根目录: {APP_ROOT}（{mode}）"
