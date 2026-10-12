# Windows x64 构建与发布

本仓库的发布目标是 `WilliamK7/scribe2srt`。正式 ZIP 仅包含本次构建的 EXE、
README、无密钥配置示例、第三方声明及许可证目录，根目录可直接运行。
`sample/` 和上游历史保留在源码仓库，个人 `settings.json` 不进入提交或发布包。

## 本地构建

使用 Windows x64 和 uv 0.13.0。`pyproject.toml` 与 `uv.lock` 是依赖来源，
`.python-version` 固定标准 CPython 3.15.0；uv 管理解释器和项目 `.venv`。
普通源码环境通过 `uv sync --locked` 仅安装运行依赖；发布环境显式加入 `build` 组。

```powershell
uv --version
./scripts/build_windows.ps1
```

脚本检查每个原生命令的退出码，失败立即停止。构建使用独立目录，避免混入旧 EXE。
可用 `-Uv C:\tools\uv.exe` 指定 uv。`-Python` 参数已移除。
已有环境不符合版本时 uv 会重建它；迁移前关闭程序，迁移后重新添加“发送到”快捷方式。
本地默认包名使用 HEAD 的短 SHA；需要正式版本名时显式传入 `-ArtifactId v0.4.0`。
本地同名包仅在新的验证全部成功后替换。正式附件必须来自成功的标签工作流。

需要分步调试或使用兼容的替换 Qt 库时，命令为：

```powershell
uv sync --locked --group build
uv run --locked --no-sync --group build python scripts/release_environment.py
uv run --locked --no-sync --group build python scripts/export_requirements.py --check
$releasePython = uv run --locked --no-sync --group build python -c "import sys; print(sys.executable)"
uv pip check --python $releasePython
$env:QT_QPA_PLATFORM = 'offscreen'
uv run --locked --no-sync --group build python -m unittest discover -s tests -t . -p 'test_*.py' -v
uv run --locked --no-sync --group build python -m PyInstaller --noconfirm --onefile --windowed --noconsole --name Scribe2SRT --collect-data opencc --additional-hooks-dir scripts/hooks --specpath build app.py
uv run --locked --no-sync --group build python scripts/package_windows.py --executable dist/Scribe2SRT.exe --artifact-id v0.4.0
```

每条命令成功后再执行下一条。`QT_QPA_PLATFORM=offscreen` 仅用于测试，不写入产品配置。
使用替换 Qt/PySide 库重新构建时，在安装步骤换成自己的兼容构建，并直接运行
PyInstaller；官方发布的打包脚本会核对固定版本，不能用于变更依赖后的非官方包。

两份 requirements 为 pip 兼容入口：`requirements.txt` 仅含运行依赖，
`requirements-build.txt` 包含运行和构建依赖。导出使用
`uv export --locked --no-emit-project --no-hashes --no-annotate`，脚本统一生成说明并保留平台条件。
禁止手工编辑；本地构建和 CI 均比较两份文件与锁文件的导出结果，不同步即停止。

升级依赖时运行 `uv lock --upgrade`，核对稳定版本并更新直接依赖下限，随后运行：

```powershell
uv run --locked python scripts/export_requirements.py
uv run --locked python scripts/export_requirements.py --check
```

保留 pip 安装方式，使用独立的标准 CPython 3.15.0 虚拟环境：

```powershell
python -m venv build/pip-venv
./build/pip-venv/Scripts/python.exe -m pip install -r requirements.txt
./build/pip-venv/Scripts/python.exe -m pip check
$env:QT_QPA_PLATFORM = 'offscreen'
./build/pip-venv/Scripts/python.exe -m unittest discover -s tests -t . -p 'test_*.py' -v
./build/pip-venv/Scripts/python.exe app.py
```

该入口仅安装运行依赖，构建许可证解析测试会提示需要 `build` 组；正式构建必须运行完整测试。

`--collect-data opencc` 收集本地字典。Qt GUI 钩子保持普通桌面组件并排除本程序
不使用的 Qt Virtual Keyboard 和 PDF 图片插件。打包检查实际 EXE 的 OpenCC
数据、`qwindows.dll`、HTTPS 证书和配置排除情况，不以构建成功代替运行验证。

## 验证 ZIP

`package_windows.py` 明确暂存 EXE、README、`settings.example.json` 和第三方声明，
从固定依赖环境复制原始 LICENSE/NOTICE。使用 `packaging.Requirement` 解析导出的注释、
规范化包名和环境条件，仅检查当前平台适用的精确版本；缺包、版本不同或缺许可证均失败。
Qt、Python、OpenSSL、zlib/zlib-ng、Expat 官方材料
及 SHA-256 记录在 `third_party/`；正常打包只校验和复制本地材料，不联网下载许可证。
每次构建记录实际运行时和 wheel 版本，缺少精确匹配的材料或哈希不符会停止打包。

更换 Python 或其内置库版本后，在对应发布环境中显式更新材料：

```powershell
uv run --locked --group build python scripts/update_license_sources.py --native-only
```

也可用已有构建的版本清单更新另一套运行时材料：
`uv run --locked --group build python scripts/update_license_sources.py --native-only --runtime-manifest path/to/licenses/dependency-versions.json`。
更新会保留其他版本的材料。修改 Qt 版本后调整脚本中的 Qt 来源版本并运行
`uv run --locked --group build python scripts/update_license_sources.py`，同时更新 Qt 和当前运行时材料。
uv 管理的解释器在 `sys.base_prefix/LICENSE.txt` 提供原始 Python 许可证，打包时直接复制。
当前 Windows CPython 3.15.0 使用 zlib-ng 2.2.4，另行记录实际 fork 版本及官方 LICENSE.md，
同时保留 zlib 兼容版本。审查来源、版本及文件差异后提交 `third_party/`。
CI 读取 `.python-version`，补丁版本不会自动漂移；更新该文件或 uv 的解释器构建来源后，
应在新环境刷新并核对原生库材料。普通打包不会自动联网补齐或退回使用其他版本。

ZIP 生成后立即解压，并验证：

- 源码与打包版的真实 Windows Shell 快捷方式添加、更新、移除。
- 单文件、多文件、中文与空格路径，以及从其他工作目录启动时的程序目录配置。
- 快捷方式在英文 Windows 环境下使用 Unicode 接口保存；测试覆盖中文、emoji 和引号路径。
- 关闭翻译时原有字幕排版保持有效，输入 JSON 不变。
- 解压后的 EXE 旁没有配置或密钥，中文标记为 `zho`，使用默认设置完成繁转简。
- 默认中文测试的 PATH 无 Python、FFmpeg 或 ffprobe；工作目录中的无效配置被忽略。
- 成功后没有任务缓存，应用自动退出。

快捷方式检查通过隔离目录运行，不改动当前用户的 SendTo 目录。单独运行：

```powershell
uv run --locked --group build python tests/send_to_windows_smoke.py --executable build/<本次构建目录>/dist/Scribe2SRT.exe --send-to-directory build/smoke-sendto
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

构建固定 `windows-2022`、uv 0.13.0、`.python-version` 中的 CPython x64 和 30 分钟上限，
默认 `contents: read`。`setup-uv` 固定到 v9.0.0 的提交 SHA，启用 uv 缓存，
缓存键包含 `pyproject.toml`、`uv.lock` 和 `.python-version`；CI 调用与本地相同的构建脚本。
发布任务在构建成功后运行，限制 push、v* 标签和本 Fork，单独授予 `contents: write`。
其他 Action 保留 checkout v4、artifact v4、action-gh-release v2。

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
