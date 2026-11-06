# OpenCode Project Manager

一个 Windows 离线小工具：**移除 OpenCode 中的项目、聊天和关联桌面记录，保留项目文件夹及里面的全部文件。** 以后可以重新选择这个目录，添加回 OpenCode。

非官方工具。当前为实验版，仅支持已验证的 **OpenCode 2.2.5 数据库结构 / Windows x64**；不匹配的结构会拒绝修改。

## 下载和使用

**[下载 ProjectRecordManager.exe](https://github.com/chchaoo/opencode-project-manager/releases/latest/download/ProjectRecordManager.exe)** · [所有版本](https://github.com/chchaoo/opencode-project-manager/releases)

无需安装 Python，无需解压，下载 EXE 后直接双击运行。

1. 选择要移除的项目。默认只显示 **Other projects**，取消勾选可查看已添加项目。
2. 完全退出 OpenCode，包括 CLI 和后台服务。
3. 点击 **完整移除项目及聊天…**，核对目录、会话数量和影响范围。
4. 输入 **移除** 确认。工具会先备份，再执行清理。

**聊天记录会被删除；重新添加文件夹不会自动恢复旧聊天。** 同一项目 ID 下的会话、克隆目录和工作树登记会一起移除，原目录及文件保留。操作期间不要启动 OpenCode。

## 能清理什么，不能清理什么？

| 能清理 | 保留 / 不处理 |
|---|---|
| 项目登记、权限、目录和工作树登记 | 原文件夹及代码、文档、`.git`、`.opencode` 等文件 |
| 项目聊天、消息、工具调用、任务和事件记录 | 其他项目的数据、共享附件、账户和模型设置 |
| 相关本地项目列表、标签页、布局及可识别草稿 | 备份、历史日志、内部文件缓存/快照、云端分享 |

这是项目管理工具，不是隐私擦除工具。global 项目不能移除；发现未知结构、无法确定归属的记录或跨项目依赖时，会拒绝操作。OpenCode 自动创建的默认项目可能再次出现。

## 备份与撤销

备份在 `%LOCALAPPDATA%\ProjectRecordManager\backups`，可能包含聊天和凭据，请勿上传或分享。

点击 **撤销某次删除…**，选择该次备份的 `undo.json`。仅当两个数据库均未发生后续变化时，才允许恢复快照；已有新聊天或设置时会拒绝覆盖。旧版空项目备份仍可按原规则恢复。

仅支持默认路径的本地数据库，不支持远程服务器、WSL 或自定义数据路径。EXE 未签名，尚未完成多机兼容验证。详细说明见 [使用说明](使用说明.md)。反馈问题时请提供版本和错误文字，**不要上传数据库、备份或聊天内容**。

## 从源码构建

需要 Windows、Python 3.12（含 tkinter）。运行时仅使用标准库，打包使用 PyInstaller。

```powershell
git clone https://github.com/chchaoo/opencode-project-manager.git
cd opencode-project-manager
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements-build.txt
.\build.ps1
```

输出：`dist/ProjectRecordManager.exe`。构建脚本会先运行测试；也可单独执行：

```powershell
python -m unittest -v test_project_manager.py test_full_cleanup.py
```

测试使用临时合成数据库，覆盖聊天清理、共享数据保护、文件保留、双库回滚、备份和撤销，不删除真实 OpenCode 项目。GitHub Actions 可自动构建 EXE。

采用 [MIT 许可证](LICENSE)，与 OpenCode 官方无关联。
