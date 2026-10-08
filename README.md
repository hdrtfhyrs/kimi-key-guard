# Kimi Key 守卫 2.0.2

用于 Windows + Chrome 的本地 Kimi Code key 守卫。持续发现名称为 **“5小时”** 的新 API key，自动保存每枚精确 ID，并为每枚 key 启动独立的本机进程。一枚 key 失败时，其他 key 的观察和计时继续。

按真实发出时间计五小时；在额度可以可靠归属给这一笔时，累计用满 100% 也会触发撤销。删除前先刷新真实控制台，原始 PNG 保存到本机并校验成功，再删除精确 ID，最后取完整列表确认。另有 **“立即撤销此key（先截图）”** 按钮，方便使用者主动撤销并验证整条流程。

## 下载与安装

在 [Releases](https://github.com/hdrtfhyrs/kimi-key-guard/releases) 下载 `kimi-key-guard-2.0.2-windows.zip`。包里只有程序与说明，不包含开发者的账号、API key、买家资料或运行截图。它不依赖 Codex 或 Node，也不会新建 Windows 开机、登录或计划任务。

1. 准备 Windows 10/11、Chrome 120 以上，以及 [Python 3.10 或更新版本](https://www.python.org/downloads/windows/)。Python 安装时启用 launcher 或加入 PATH；已经装好则直接继续。
2. 解压到固定文件夹，建议放在 D 盘，例如 `D:\KimiKeyGuard\app`。后续不要随意移动该文件夹。
3. 双击 `install.cmd`。它会识别 Python、生成本机配置、注册当前用户的 Chrome 本机桥，并按需启动后台，不需要管理员权限。
4. 打开 `chrome://extensions`，开启“开发者模式”，点“加载已解压的扩展程序”，选择 **包含 `manifest.json` 的文件夹**。如果下载的是 GitHub 的 Source code ZIP，请选择解压后的仓库根文件夹。
5. 打开并登录 [Kimi Code 控制台](https://www.kimi.com/code/console)，从这个页面打开“Kimi Key 守卫”。确认顶部版本为 2.0.2、Chrome 桥已连接、监督和各 key 的心跳在更新。

有 D 盘时，默认运行数据位于 `D:\KimiKeyGuard\data`；没有 D 盘时位于 `%LOCALAPPDATA%\KimiKeyGuard\data`。程序文件和运行数据分开，更新程序时保留原配置及数据。截图在数据目录的 `screenshots\精确ID\证据ID\` 下。

## 自动计时与用量

程序只发现名为“5小时”的有效 key。发现时立即登记并观察，创建时间、首次发现时间和实际发出时间分别记录；未知的发出时间不会用创建时间代替。

首次安装默认暂停自动删除。在“补充交付记录”选择对应 ID，填真实发出时间和发出时五小时频限的已用百分比，再点“保存这枚key的交付记录”。记录填写后，由实际发出时刻计算五小时。需要自动到期撤销时，再点“允许自动删除”。暂停期间仍会发现和观察新 ID。

Kimi 账号额度由登录设备和 key 共享；当前接口没有每枚 key 的独立百分比。仅在本笔独占账号且额度记录连续时按观测累计提前撤销。多个 key 重叠或历史窗口缺失时保留额度归属未知，已知发出时间的五小时守卫继续。周用量显示 100% 本身不是这个守卫的五小时累计触发条件。[Kimi 官方权益说明](https://www.kimi.com/help/kimi-code/benefits)

## 主动撤销与删除功能验证

在准备撤销的 key 卡片中点红色 **“立即撤销此key（先截图）”**，核对确认框里的名称和 API ID，再确认。**这会真实删除所选 key，让它失效。** 自动删除暂停时仍可明确确认手动撤销，无需把额度改成 100% 或修改发出时间。

执行顺序为：刷新真实页面 → 原始 PNG 存盘并校验 → 再次核本 ID 的任务和确认 → 精确删除 → 完整列表确认目标不存在 → 保存删除结果。删除前截图、存档或授权失败会停止；错误和正在处理的步骤显示在面板上。成功后自动展开历史单，显示“手动撤销完成”、截图路径和结果记录路径。

“取消当前操作并暂停删除”或本 ID 暂停会取消尚未执行的请求；已发送的平台删除无法撤回。每次手动请求有效期为 90 秒，失败或超时后需要重新确认。真实交付字段不会因手动撤销而被改写。

## 常见情况

“找不到本机消息宿主”或“访问被禁止”：关闭守卫弹窗，重新运行 `install.cmd`，再从 Kimi 页面打开。移动过程序目录时，需要重新加载扩展并重新安装本机桥。如果自动生成的扩展 ID 与 Chrome 卡片不同，可以将卡片 ID 明确传给安装脚本。

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\setup.ps1 -ExtensionId "Chrome卡片上的32位ID"
```

“未找到 Python”：先安装 Python，或明确指定路径；可同时指定运行数据目录。

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\setup.ps1 -PythonPath "D:\Python\python.exe" -DataRoot "D:\KimiKeyGuard\data"
```

关闭过后台时，可双击 `start-guard.cmd` 按需恢复。Chrome、已登录的 Kimi 控制台和电脑需保持运行；网页退出登录或不可用时，平台动作会等待恢复。截图使用真实活动标签页，取证期间会短暂切换和刷新这个页面。

## 当前交付依据

原本机版本 2.0.1 已取得真实 Chrome 桥心跳、自然新 ID 自动登记和独立进程运行依据。2.0.2 加入了手动先截图撤销入口；发布包已移除本机固定路径和历史租期，并进行源码解析与确定性打包。打包发布期间没有自动试删，没有冒称已取得本版本完整删除回执。另一台电脑安装以及真实截图、撤销结果，须以那台电脑的实际心跳、PNG 和完整列表回执为准。

本仓库提供 Kimi 本地守卫，不包含闲鱼订单、聊天收发或自动交付服务。它使用已登录浏览器完成平台操作，依赖 Kimi 当前控制台与接口结构。

本机桥注册方式沿 [Chrome Native messaging 文档](https://developer.chrome.com/docs/extensions/develop/concepts/native-messaging)。程序使用 Python 标准库，不安装额外 Python 软件包。
