# uv 迁移与本地验收（2026-10-12）

已实施 `PLAN.md` 的仓库迁移与本地验证范围。源码运行、Windows 构建和 CI
统一使用 uv 0.13.0、标准 CPython 3.15.0 x64 及 `uv.lock`；保留脚本式 `app.py`
入口和 pip 安装方式。以下记录覆盖提交前的本地验收；远程工作流结果以对应提交的
GitHub Actions 为准。

## 配置与许可证

- `pyproject.toml` 声明 `>=3.15,<3.16`，`.python-version` 固定 3.15.0；
  uv 精确版本及禁止预发布版本的策略也在项目配置中。
- 默认环境包含 13 个运行依赖包；显式加入 `build` 组后，Windows 环境包含
  20 个包。首次升级解析选择的稳定版本与此前固定版本一致，Qt 保持 6.12.0。
- 两份 requirements 由 `scripts/export_requirements.py` 自动导出，保留平台条件；
  `--check` 与本地构建、CI 门禁共用相同导出方式。
- 发布校验读取 `.python-version`，要求精确补丁版本、正式版、标准 CPython 及
  Windows x64。构建脚本支持 `-Uv` 和 `-ArtifactId`。
- 当前解释器实际使用 OpenSSL 3.5.9、Expat 2.8.5 和 zlib-ng 2.2.4；
  zlib 报告的兼容版本是 `1.3.1.zlib-ng`。新增 Python 3.15.0 和 zlib-ng
  的官方许可证及 SHA-256，保留此前版本的材料。
- uv 解释器的原始 Python 许可证位于 `sys.base_prefix/LICENSE.txt`，已成功复制到 ZIP。
  Qt 的 201 份原始许可证和归属材料保持不变，均通过哈希检查。

## 验证结果

| 环境或检查 | 结果 |
|---|---|
| 默认 `uv sync --locked` 运行环境 | 发现 127 项测试，116 项通过；11 项构建许可证解析测试按需跳过 |
| `uv sync --locked --group build` 构建环境 | 127 项离线测试全部通过，无跳过 |
| 独立 CPython 3.15.0 pip 环境 | `pip install -r requirements.txt` 和 `pip check` 成功；116 项运行测试通过，11 项构建测试按需跳过 |
| 两份 requirements 与锁文件 | 导出一致；分别人为修改临时副本时，检查均拒绝通过 |
| 发布解释器限制 | 错误补丁版本 3.15.1、ARM64 均被拒绝 |
| PowerShell 构建脚本及 CI Python 安装命令 | 语法检查通过；actionlint 工作流检查通过 |
| Windows EXE、ZIP 与 SHA-256 | 完整构建、解压验证和独立哈希核对通过 |

新增许可证测试覆盖条件依赖、整行及行尾注释、包名规范化、缺包、版本不符、
PEP 440 等价版本、非精确依赖拒绝、重复冲突、缺许可证及原始许可证复制。
原生库测试另行覆盖 zlib-ng 实际版本与兼容版本的区分。

完整构建调用了 `scripts/build_windows.ps1 -ArtifactId v0.4.0-uv-review`。
EXE 检查确认包含 Qt Windows 插件、OpenCC 字典、HTTPS 证书和 `python315.dll`，
排除个人 `settings.json`、Qt Virtual Keyboard 及 PDF 图片插件。
ZIP 包含 254 个文件、20 个适用于 Windows 的包版本记录及 4 套匹配的原生库许可证。

已有 SendTo 烟雾测试在隔离目录中通过：

- 源码与打包版快捷方式的添加、更新和移除。
- 单文件、多文件、中文与空格路径，以及从其他工作目录启动。
- 输入 JSON 保持不变，字幕输出与自动退出符合预期。
- ZIP 解压后的 EXE 在无密钥、无 Python/FFmpeg/ffprobe 的 PATH 下，
  使用默认配置完成中文繁转简；忽略工作目录中的无效配置，没有遗留任务缓存。

## 本地产物与复现

产物：`dist/releases/Scribe2SRT-v0.4.0-uv-review-windows-x86_64.zip`。
同目录提供 `.zip.sha256`，本次 ZIP 的 SHA-256 为：

```text
2bd50701ffd91bd3c44f9c0c16fb09934c9093bab68cecd99a1e5fc1781b27ba
```

本轮工具运行将缓存和 uv 解释器放在仓库内，以适应当前工作区写入权限：

```powershell
$env:UV_CACHE_DIR = Join-Path $PWD 'build/uv-cache'
$env:UV_PYTHON_INSTALL_DIR = Join-Path $PWD 'build/uv-python'
./scripts/build_windows.ps1 -ArtifactId v0.4.0-uv-review
```

日志位于 `build/uv-migration-build.log`、`build/uv-migration-runtime-tests.log`、
`build/uv-migration-pip-install.log` 和 `build/uv-migration-pip-tests.log`。
独立 pip 验证环境、旧虚拟环境备份、旧工作树、历史构建、下载缓存及源码字节码
已在验收后清理。清理记录位于 `build/cleanup-20261012.json`。
当前使用的 uv 解释器、必要工具、最新 ZIP 和上述验收日志保留。
验收后项目 `.venv` 恢复为默认的 13 个运行依赖包；迁移后需重新添加“发送到”快捷方式。

GitHub Actions 已更新为固定版本的 setup-uv 和本地同一构建脚本，保留原有触发、
产物命名及标签发布规则。main 推送会运行远程 CI；真实 ElevenLabs/Qwen 服务及
翻译质量仍不属于离线验收范围。
