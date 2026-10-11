# Windows x64 构建与发布

本仓库的发布目标是 `WilliamK7/scribe2srt`。正式 ZIP 仅包含本次构建的 EXE、
README、无密钥配置示例、第三方声明及许可证目录，根目录可直接运行。
`sample/` 和上游历史保留在源码仓库，个人 `settings.json` 不进入提交或发布包。

## 本地构建

使用 Windows、Python 3.11 x64 和一个独立虚拟环境。普通源码安装仍使用
`requirements.txt`，发布环境使用包含传递依赖的 `requirements-build.txt`。
不要从 Python 3.14 环境导出发布依赖。

```powershell
python -m venv build/release-venv
./scripts/build_windows.ps1 -Python ./build/release-venv/Scripts/python.exe
```

脚本检查每个原生命令的退出码，失败立即停止。构建使用独立目录，避免混入旧 EXE。
本地默认包名使用 HEAD 的短 SHA；需要正式版本名时显式传入 `-ArtifactId v0.4.0`。
本地同名包仅在新的验证全部成功后替换。正式附件必须来自成功的标签工作流。

需要分步调试或使用兼容的替换 Qt 库时，命令为：

```powershell
python -m pip install -r requirements-build.txt
python -m pip check
$env:QT_QPA_PLATFORM = 'offscreen'
python -m unittest discover -s tests -t . -p 'test_*.py' -v
python -m PyInstaller --noconfirm --onefile --windowed --noconsole --name Scribe2SRT --collect-data opencc --additional-hooks-dir scripts/hooks --specpath build app.py
python scripts/package_windows.py --executable dist/Scribe2SRT.exe --artifact-id v0.4.0
```

每条命令成功后再执行下一条。`QT_QPA_PLATFORM=offscreen` 仅用于测试，不写入产品配置。
使用替换 Qt/PySide 库重新构建时，在安装步骤换成自己的兼容构建，并直接运行
PyInstaller；官方发布的打包脚本会核对固定版本，不能用于变更依赖后的非官方包。

`--collect-data opencc` 收集本地字典。Qt GUI 钩子保持普通桌面组件并排除本程序
不使用的 Qt Virtual Keyboard 和 PDF 图片插件。打包检查实际 EXE 的 OpenCC
数据、`qwindows.dll`、HTTPS 证书和配置排除情况，不以构建成功代替运行验证。

## 验证 ZIP

`package_windows.py` 明确暂存 EXE、README、`settings.example.json` 和第三方声明，
从固定依赖环境复制原始 LICENSE/NOTICE。Qt 官方源文件的副本及 SHA-256 记录在
`third_party/`，修改 Qt 版本后运行 `python scripts/update_license_sources.py`，
审查生成的文件再提交。每次构建还记录实际 Python、OpenSSL、zlib 和 wheel 版本。

ZIP 生成后立即解压，并验证：

- 源码与打包版的真实 Windows Shell 快捷方式添加、更新、移除。
- 单文件、多文件、中文与空格路径，以及从其他工作目录启动时的程序目录配置。
- 关闭翻译时原有字幕排版保持有效，输入 JSON 不变。
- 解压后的 EXE 旁没有配置或密钥，中文标记为 `zho`，使用默认设置完成繁转简。
- 默认中文测试的 PATH 无 Python、FFmpeg 或 ffprobe；工作目录中的无效配置被忽略。
- 成功后没有任务缓存，应用自动退出。

快捷方式检查通过隔离目录运行，不改动当前用户的 SendTo 目录。单独运行：

```powershell
python tests/send_to_windows_smoke.py --executable dist/Scribe2SRT.exe --send-to-directory build/smoke-sendto
```

所有应用子进程设置 60 秒上限。超时清理只针对测试启动的 PID 及其子进程。
ZIP 通过解压运行验证后才生成 `<包名>.zip.sha256`，内容采用 `SHA256  文件名` 格式。
产物位于 `dist/releases/`，不会通配打包项目根目录或整个 `dist/`。

## GitHub 前置条件

GitHub CLI 使用官方 Windows amd64 便携版，下载 ZIP 后核对同版本 checksums 文件。
本机 CLI 登录与 ChatGPT GitHub 连接器登录互相独立：

```powershell
gh auth login --hostname github.com --git-protocol https --web --scopes workflow
gh auth status --hostname github.com
gh api user --jq .login
```

必须确认 `WilliamK7`。不要把令牌发到聊天或写入 CI。凭据仅能在沙箱外使用时，
通过正常授权调用相同 CLI，不读取或复制凭据文件。CLI 登录配置和系统凭据存储
需要用户目录写入权限。

查询目标仓库；不存在时执行：

```powershell
gh repo fork cylind/scribe2srt --clone=false
```

CLI 2.102.0 在显式指定仓库时不接受 `--remote=false`；上述命令不克隆仓库，
本地远程另行配置。Fork 创建后检查公开属性、上游来源、写入权限和 `main`；
异步创建最多等待 5 分钟。同名无关仓库不覆盖或删除。

本仓库提交身份使用 `WilliamK7` 和 `82304956+WilliamK7@users.noreply.github.com`。
当前上游 `origin` 改为 `upstream`，个人 Fork 使用 `origin`；重复执行先核实 URL。
Git 推送使用 CLI 凭据助手，保证 gh.exe 位于当前 PATH：

```powershell
git -c credential.helper= -c 'credential.helper=!gh auth git-credential' push --dry-run origin main
```

所有 gh 工作流、标签、Release 操作显式指定 `--repo WilliamK7/scribe2srt`，
API 操作使用相同路径，防止 Fork 默认上游选择导致操作错误。

## 上线顺序

1. 完成本地测试、重新构建、ZIP 验证和暂存审查后提交。保留已暂存的配置退出
   版本控制操作及磁盘上的配置。明确暂存新模块、测试、fixtures 和构建依赖；
   不提交虚拟环境、探测工具、缓存、EXE 或个人配置。
2. Fetch 个人 Fork，整合远程领先提交后重新验证；不强推。
3. 推送 `main` 并记录实际 SHA，检查 Fork Actions 权限和工作流启用状态。
4. 首次推送没有触发任务时，启用工作流并手动运行：
   `gh workflow run release.yml --repo WilliamK7/scribe2srt --ref main`。
   按 SHA 查找并等待对应任务成功，不能使用无关的最近一次绿色任务。
5. 检查本地、个人远程标签和 Release 的 `v0.4.0` 是否占用。仅在空闲时，于已通过
   Windows CI 的提交创建附注标签，并只推送该标签。
6. 标签任务重新构建、验证并发布正式 Release。仅上传该次成功构建的 ZIP 和校验文件。
7. 下载公开附件，核对 SHA-256、ZIP 根目录和许可证，再运行离线中文烟雾测试。

工作流行为：

| 事件 | 验证、打包、Actions artifact | 正式 Release |
|---|---|---|
| main 推送 | 是，短 SHA 包名 | 否 |
| 面向 main 的 PR | 是，事件提交包 | 否 |
| workflow_dispatch | 是，所选分支/标签 | 否 |
| 本 Fork v* 标签推送 | 是，标签对应提交 | 是 |

构建固定 `windows-2022`、Python 3.11 x64、30 分钟上限，默认 `contents: read`。
发布任务在构建成功后运行，限制 push、v* 标签和本 Fork，单独授予 `contents: write`。
Action 主版本保留 checkout v4、setup-python v5、artifact v4、action-gh-release v2。

## 失败与重试

- 登录未完成或账号不符：继续本地工作，暂停远程操作。保存凭据失败通过正常授权解决。
- Fork 403/404/422：检查权限、账号限制、同名冲突和异步状态，不删除或重建仓库。
- 工作流文件推送被拒绝：检查 `workflow` scope，不重复执行相同失败命令。
- 推送没有任务：检查 Actions 启用状态、事件过滤和默认分支工作流，再手动触发。
- 构建/烟雾失败：读取对应步骤日志，修复后在新提交上重跑；不删测试或使用旧 EXE。
- 临时网络或运行器失败：重跑同一提交对应任务。
- 发布上传 403：检查发布任务权限和 Actions 策略，不向 CI 添加个人 API Key。
- 仅发布失败：重跑该次失败发布任务，复用同一次成功构建的 artifact。
- 标签已公开后需要修改源码：保持旧标签，使用下一个未占用的 `v0.4.x`。

真实 ElevenLabs 和 Qwen 请求不进入自动门禁。离线验证不能证明实际服务可用性、
组批编号保留率、翻译语义对齐或翻译质量；Release 说明必须保留此验证边界。
