# 迁移至 uv，并升级 Python 与依赖

## 目标与默认选择

统一源码运行、测试、Windows 打包和 CI 的环境管理，同时保留 pip 安装入口。`pyproject.toml` 与 `uv.lock` 成为依赖来源，两份 requirements 文件改为自动导出。

采用标准 CPython 3.15.0 x64，由 uv 管理；项目声明 `>=3.15,<3.16`，提交 `.python-version` 固定补丁版本。3.15.0 已正式发布；不维持旧 Python 兼容性。[Python 官方版本](https://www.python.org/downloads/)

## 项目与依赖配置

- 保留当前脚本式应用结构，设置 `tool.uv.package = false`，继续以 `app.py` 为入口。
- 运行依赖包括 PySide6、requests、requests-toolbelt 和 OpenCC；单独建立 `build` 依赖组，包含 PyInstaller、pefile 和许可证解析所需的 packaging。
- 首次解析选择最新稳定依赖，以选中版本作为直接依赖下限，完整版本和哈希写入 `uv.lock`。已经是最新版的包无需强行变更；不采用预发布版本。
- 固定 uv 0.13.0，使本地、CI 和导出工具一致。默认环境仅安装运行依赖，打包时显式加入 `build` 组。
- 保留两份 requirements：普通文件导出运行依赖，build 文件导出运行及构建依赖。统一使用 `uv export --locked --no-emit-project --no-hashes --no-annotate`，保留平台条件和生成说明，禁止手工维护。[uv 锁定与导出机制](https://docs.astral.sh/uv/concepts/projects/sync/)

## 构建、许可证与使用入口

- 源码安装和启动改为 `uv sync --locked`、`uv run --locked python app.py`；测试继续使用现有 unittest。
- Windows 构建脚本使用 `uv sync --locked --group build`，随后从同一环境执行依赖检查、测试、PyInstaller 和 ZIP 验证。移除 `-Python` 参数，保留 `-ArtifactId`，可用 `-Uv` 指定 uv 可执行文件。
- 发布环境校验从 `.python-version` 读取精确版本，并继续要求 Windows x64，替换写死的 Python 3.11 判断。
- 许可证收集使用 `packaging.Requirement` 解析 build 导出文件，处理注释、名称规范化和环境条件，仅校验当前平台适用的精确版本。缺包、版本不符、许可证缺失仍使构建失败。
- 为新 Python 及实际内置库补齐官方许可证材料与哈希；检查 uv 管理的 Python 许可证位置。Qt 版本发生变化时同步更新其材料与声明。
- README 和发布说明更新为 uv 流程，保留 pip 安装命令，并注明迁移虚拟环境后需重新添加“发送到”快捷方式。

## CI 与验收

- GitHub Actions 使用固定版本的 `setup-uv`，读取项目固定的 Python 版本，启用 uv 缓存；安装和执行均使用锁定依赖。
- 本地构建与 CI 都检查两份 requirements 与锁文件导出结果一致；不同步即失败。保留现有触发条件、产物命名及标签发布规则。
- 在新环境运行全部离线测试；新增许可证解析测试，覆盖条件依赖、注释、包名规范化、缺包和版本不符。
- 在独立环境验证 pip 导出的运行依赖可安装、通过依赖检查并运行离线测试。
- 完整构建 Windows EXE，验证 Qt 插件、OpenCC 字典、证书、许可证、ZIP 和 SHA-256；运行已有源码及打包版 SendTo 烟雾测试，覆盖中文与空格路径及无 Python/FFmpeg 的默认繁转简。

实施阶段范围为仓库迁移与本地验证。实施与本地验收已完成：构建环境 127 项离线测试全部通过，独立 pip 环境和 Windows EXE/ZIP、SendTo 烟雾测试通过。实际使用的 zlib-ng 已补齐官方许可证材料。详细结果见 [uv 迁移验收记录](docs/uv-migration-validation-20261012.md)。

后续按用户追加指令清理临时生成文件、提交并推送 main，不创建标签或发布 Release。
