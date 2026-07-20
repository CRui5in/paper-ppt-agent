# Changelog

本项目所有重要变更都会记录在此文件中。

格式基于 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [1.0.2] - 2026-07-20

🛠️ Codex Agent 运行时兼容性补丁。

### 🔧 修复

- 将 Codex Python SDK 更新到 OpenAI 官方 commit `bf3c1972b7d045c0a3a48dff91f381070f8f69e1`。
- 将配套 `openai-codex-cli-bin` 从 `0.131.0a4` 更新至 `0.144.4`，修复 `gpt-5.6-sol` 因旧 Codex CLI 无法运行而被误报为 `svg_output/` 为空的问题。
- 将 Codex Agent 预检、持久会话和模板导入迁移到新版 `CodexConfig`、`Sandbox.full_access` 与 `turn(sandbox=...)` API，修复升级后因旧 SDK 类型已移除而导致的生成请求 `400 Bad Request`。
- 同步 `pyproject.toml` 与 `uv.lock`，确保 `uv sync --locked` 安装新版 SDK/CLI。

### 📝 维护

- 移除独立版本说明文件，统一以 `CHANGELOG.md` 作为发布记录来源。

## [1.0.1] - 2026-07-01

🛠️ 维护性补丁。

### 🔧 修复

- 同步 `uv.lock` 中项目版本号，修复 `uv sync --locked` 因版本不一致而失败的问题。

## [1.0.0] - 2026-06-23

🎉 第一个正式版本。上传论文 PDF / TeX 源码（或链接、文本），由多智能体协作自动生成可编辑的 PowerPoint。

### ✨ 核心能力

- **多智能体流水线**：Strategist → Executor → Critic 三阶段协作，内容提炼与版式生成一体化
- **Agent 生成模式**：工作台支持 Claude Code / Codex 本机 Agent 运行时生成演示文稿
- **静态 + 视觉 QA**：自动检测文字溢出、元素重叠、低对比度等问题并触发修复
- **反馈迭代**：指定单页或全量重生成，支持结构调整（增删插排），自动版本快照
- **实时可观测**：Agent 日志流、Token 用量聚合、Critic 逐页详情面板
- **多语言**：支持中英双语及自定义语言输出
- **多模型**：OpenAI / Anthropic / Gemini / DeepSeek 及自定义兼容接口
- **模板导入**：支持 PPTX 直接导入为五页模板，也支持基于 Claude Code / Codex 的 Agent 模式自动分析、模板化与预览
- **PPT 编辑器**：内置基于 PPTist 的可视化编辑器，支持结果页和模板导入页中直接调整页面、备注、字体与导出
- **Deep Research**：外部研究增强（arXiv / Semantic Scholar / Web），相关性自动过滤

### 🚀 本次发布包含的功能

#### 2026 年 6 月

- 📚 **多源数据支持** — 支持论文、链接和文本资料共同作为生成来源
- 🧠 **论文解析增强** — 增强论文解析、章节规划和内容筛选能力
- 🖼️ **图文匹配优化** — 提高正文图表与页面图片的匹配准确性
- 🧭 **汇报结构优化** — 收敛目录和章节划分，优化汇报结构
- 🛠️ **生成稳定性提升** — 提升 Provider 与 Agent 生成稳定性
- 🌐 **状态与预览改进** — 改进 SVG 修复、预览解析和生成状态反馈
- 🧩 **模板导入 Codex 支持**
- 🚀 **交互式启动器**

#### 2026 年 5 月

- 👁️ **视觉 QA（实验性）** — 调用多模态大模型将幻灯片渲染为图像进行布局与对比度审查
- 🖥️ **实时 SVG 预览 + 日志面板 + Critic 详情视图** — 生成过程中实时查看幻灯片、Agent 日志与审查详情
- 🎨 **模板系统与自定义字体** — 预设行业风格模板，支持自定义标题/正文字体配置
- 🧩 **模板导入** — 支持 PPTX 直接导入、五页模板映射，以及基于 Claude Code 的 Agent 模式自动分析和模板化
- 🤖 **Agent 生成模式** — 工作台接入 Claude Code / Codex 生成演示文稿
- 📝 **PPT 编辑器** — 在生成结果与模板导入流程中接入可视化 PPT 编辑器，支持页面编辑、备注、保存、重新导出
- 🔬 **Deep Research 工作流** — 外部研究增强（arXiv / Semantic Scholar / Web）+ 相关性过滤
- 🎨 **前端 UI 重构** — 重构工作台、结果页与模板导入等前端体验，升级 SVG-to-PPTX 转换器

#### 2026 年 4 月

- 🔒 **静态 Critic 增强** — 新增装饰线遮挡检测、低对比度文本检测，修复多行文字宽度估算误报
- 📁 **版本历史管理** — 每次反馈迭代自动归档快照，支持版本对比与回溯
- 🔎 **Token 日志筛选** — 按模型、阶段、页码、任务筛选 LLM 调用记录，支持点击展开详情
- ⏹️ **生成取消** — 支持在流水线运行中取消当前任务
- 🤖 **多智能体流水线** — Strategist → Executor → Critic 三阶段协作，支持 SVG 自动修复与反馈迭代

### ⚙️ 环境要求

| 依赖 | 版本 |
|:-----|:-----|
| 🐍 Python | 3.11+ |
| 📦 [uv](https://docs.astral.sh/uv/) | latest |
| 🟢 Node.js | 18+ |

至少一种模型提供商的 API Key：OpenAI / Anthropic / Gemini / DeepSeek 或自定义 BaseURL 兼容接口。

### ⚠️ 已知限制

- 需要自备至少一家模型提供商的 API Key（OpenAI / Anthropic / Gemini / DeepSeek 或自定义接口）
- Agent 生成模式需要在本机安装并配置 Claude Code 或 Codex
- 视觉 QA 当前为实验性功能，依赖多模态大模型
- 当前以源码方式运行，未提供预编译可执行文件

### 🙏 参考项目

- [PPTAgent](https://github.com/icip-cas/PPTAgent) — 流程设计与 Agent 架构参考
- [ppt-master](https://github.com/hugohe3/ppt-master) — 部分工程实现参考
- [PPTist](https://github.com/pipipi-pikachu/PPTist) — PPT 编辑器能力参考与集成基础
