# Paper PPT Agent

<p align="center">
  <b>Upload a paper, AI generates your presentation</b>
</p>

<p align="center">
  <a href="https://github.com/CRui5in/paper-ppt-agent/blob/master/LICENSE"><img src="https://img.shields.io/badge/License-MIT-blue.svg" alt="MIT License"></a>
  <img src="https://img.shields.io/badge/Python-3.11+-3776AB?logo=python&logoColor=white" alt="Python">
  <img src="https://img.shields.io/badge/FastAPI-0.115+-009688?logo=fastapi&logoColor=white" alt="FastAPI">
  <img src="https://img.shields.io/badge/React-18+-61DAFB?logo=react&logoColor=black" alt="React">
  <img src="https://img.shields.io/badge/TypeScript-5+-3178C6?logo=typescript&logoColor=white" alt="TypeScript">
  <img src="https://img.shields.io/badge/uv-powered-DE5FE9?logo=astral&logoColor=white" alt="uv">
</p>

<p align="center">
  <a href="./README.md">中文</a> | English
</p>

---

A multi-agent pipeline for automatically generating editable PowerPoint presentations from academic papers. Upload a PDF or TeX source, and the AI handles content extraction, structural planning, layout design, and visual quality assurance.

![screenshot](./screenshot.png)

## Table of Contents

- [✨ Features](#-features)
- [📸 Demo](#-demo)
- [⚙️ Requirements](#️-requirements)
- [🚀 Quick Start](#-quick-start)
- [📋 Changelog](#-changelog)
- [🗺️ Roadmap](#️-roadmap)
- [🙏 Acknowledgements](#-acknowledgements)
- [📄 License](#-license)

---

## ✨ Features

| Feature | Description |
|:--------|:------------|
| **Multi-Agent Pipeline** | Strategist → Executor → Critic three-stage collaboration for content extraction and layout generation |
| **Static + Visual QA** | Automatically detects text overflow, element overlap, low contrast, and triggers repair |
| **Icon Semantic Matching** | RAG semantic search via Gemini Embedding to automatically match icons to slide content |
| **Feedback Iteration** | Targeted or full regeneration with structural changes (insert, remove, reorder) and version snapshots |
| **Real-time Observability** | Agent log stream, Token usage aggregation, per-page Critic detail panel |
| **Multi-language** | Chinese, English, bilingual, and custom language output |
| **Multi-model** | OpenAI / Anthropic / Gemini / DeepSeek and custom-compatible APIs |
| **Template System** | Pre-built industry-style templates with custom font configuration |
| **Deep Research** | External research enrichment (arXiv / Semantic Scholar / Web) with relevance filtering |

## 📸 Demo

<p align="center">
  <img src="./demo.png" width="700" alt="Demo">
</p>

## ⚙️ Requirements

| Dependency | Version |
|:-----------|:--------|
| 🐍 Python | 3.11+ |
| 📦 [uv](https://docs.astral.sh/uv/) | latest |
| 🟢 Node.js | 18+ |

An API key for at least one model provider: OpenAI / Anthropic / Gemini / DeepSeek or a custom BaseURL-compatible API.

## 🚀 Quick Start

```bash
# Clone the repository
git clone https://github.com/CRui5in/paper-ppt-agent.git
cd paper-ppt-agent

# One-click start (auto-installs deps + launches frontend & backend)
# Windows
.\start-dev.bat
# Linux
sh start-dev.sh
```

After starting: Frontend [http://127.0.0.1:5173](http://127.0.0.1:5173) · Backend [http://127.0.0.1:8000](http://127.0.0.1:8000)

<details>
<summary>📎 Manual start</summary>

```bash
# Install dependencies
uv sync --locked
cd frontend && npm install && cd ..

# Backend
uv run python -m uvicorn backend.app:app --host 127.0.0.1 --port 8000 --reload --reload-dir backend

# Frontend
cd frontend && npm run dev -- --host 127.0.0.1 --port 5173 --strictPort
```

</details>

---

## 📋 Changelog

### May 2026

- 🧠 **DeepSeek Provider** — Dedicated DeepSeek provider support with thinking mode configuration
- 👁️ **Visual QA (Experimental)** — Multimodal LLM renders slides as images for layout and contrast review
- 🖥️ **Real-time SVG Preview + Log Panel + Critic Detail View** — Live slide preview, Agent logs, and review details during generation
- 🎯 **Icon RAG Semantic Search** — Gemini Embedding-based semantic search for icon candidates, independently toggleable
- 🎨 **Template System & Custom Fonts** — Pre-built industry-style templates with custom heading/body font configuration
- 🔬 **Deep Research Workflow** — External research enrichment (arXiv / Semantic Scholar / Web) with relevance filtering
- 🖼️ **Online Image Search** — Search for images online using Tavily / SerpAPI, with AI layout analysis, one-click undo, and download

### April 2026

- 🔒 **Static Critic Enhancements** — Decorative-line occlusion detection, low-contrast text detection, multi-line text width estimation fix
- 📁 **Version History Management** — Automatic snapshot archival per feedback iteration with comparison and rollback
- 🔎 **Token Log Filtering** — Filter LLM calls by model, stage, page, and job with click-to-expand detail view
- ⏹️ **Generation Cancellation** — Cancel a running pipeline mid-execution
- 🤖 **Multi-Agent Pipeline** — Strategist → Executor → Critic three-stage collaboration with automatic SVG repair and feedback iteration

---

## 🗺️ Roadmap

- [ ] 🎨 UI Refactor
- [ ] 📐 Template management further implementation and optimization
- [ ] 🧠 Local model support

---

## 🙏 Acknowledgements

- [PPTAgent](https://github.com/icip-cas/PPTAgent) — Pipeline design and Agent architecture reference
- [ppt-master](https://github.com/hugohe3/ppt-master) — Parts of the engineering approach reference

## ⭐ Star History

<a href="https://www.star-history.com/?repos=CRui5in%2Fpaper-ppt-agent&type=date&legend=top-left">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="https://api.star-history.com/chart?repos=CRui5in/paper-ppt-agent&type=date&theme=dark&legend=top-left" />
    <source media="(prefers-color-scheme: light)" srcset="https://api.star-history.com/chart?repos=CRui5in/paper-ppt-agent&type=date&legend=top-left" />
    <img alt="Star History Chart" src="https://api.star-history.com/chart?repos=CRui5in/paper-ppt-agent&type=date&legend=top-left" />
  </picture>
</a>

## 📄 License

[MIT License](./LICENSE)

## 📬 Contact

- 💬 GitHub Issues: [CRui5in/paper-ppt-agent/issues](https://github.com/CRui5in/paper-ppt-agent/issues)
- 📧 Email: qinruoxuan2018@gmail.com
