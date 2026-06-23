# Paper PPT Agent v1.0.0 🎉

<p align="center">
  <b>The first official release of Paper PPT Agent</b>
</p>

Upload an academic paper (PDF / TeX) — or links and text — and a multi-agent pipeline automatically generates an editable PowerPoint presentation.

![screenshot](https://raw.githubusercontent.com/CRui5in/paper-ppt-agent/master/screenshot.png)

---

## ✨ Highlights

- **Multi-Agent Pipeline** — Strategist → Executor → Critic collaboration for content extraction and layout generation
- **Agent Generation Mode** — Workbench supports local Claude Code / Codex runtimes
- **Static + Visual QA** — Auto-detects text overflow, element overlap, low contrast, and triggers repair
- **Feedback Iteration** — Per-page or full regeneration with structural changes (insert / remove / reorder) and version snapshots
- **Real-time Observability** — Agent log stream, token usage aggregation, per-page Critic detail panel
- **Multi-Source Input** — Combine papers, links, and text as generation sources
- **Multi-language** — Chinese, English, bilingual, and custom language output
- **Multi-model** — OpenAI / Anthropic / Gemini / DeepSeek and custom-compatible APIs
- **Template Import** — Import PPTX as a five-page template, or use Claude Code / Codex for automated analysis and templateization
- **PPT Editor** — Built-in PPTist-based visual editor for slides, notes, fonts, saving, and re-export
- **Deep Research** — External research enrichment (arXiv / Semantic Scholar / Web) with relevance filtering

## 🚀 Quick Start

```bash
# Clone the repository
git clone https://github.com/CRui5in/paper-ppt-agent.git
cd paper-ppt-agent

# One-click launch (auto-installs dependencies + starts frontend & backend)
# Windows
.\start-dev.bat
# macOS / Linux
./start-dev.sh
```

Then open <http://localhost:5173> and configure at least one model provider API Key.

### Requirements

| Dependency | Version |
|:-----------|:--------|
| 🐍 Python | 3.11+ |
| 📦 [uv](https://docs.astral.sh/uv/) | latest |
| 🟢 Node.js | 18+ |

## 📦 What's included in this release

<details>
<summary><b>June 2026</b></summary>

- 📚 **Multi-source data support** — Papers, links, and text combined as generation sources
- 🧠 **Paper parsing enhanced** — Improved parsing, section planning, and content filtering
- 🖼️ **Figure-text matching** — Better matching accuracy between body figures and page images
- 🧭 **Report structure optimization** — Tighter TOC and sectioning
- 🛠️ **Generation stability** — More robust Provider and Agent generation
- 🌐 **State & preview improvements** — SVG repair, preview parsing, and generation state feedback
- 🧩 **Codex support for template import**
- 🚀 **Interactive launcher**

</details>

<details>
<summary><b>May 2026</b></summary>

- 👁️ **Visual QA (experimental)** — Multi-modal VLM renders slides as images for layout & contrast review
- 🖥️ **Real-time SVG preview + log panel + Critic detail view**
- 🎨 **Template system & custom fonts** — Preset industry templates, customizable title/body fonts
- 🧩 **Template import** — Direct PPTX import, five-page template mapping, Claude Code-based auto analysis
- 🤖 **Agent generation mode** — Claude Code / Codex integration
- 📝 **PPT editor** — Visual editor with slide editing, notes, save, and re-export
- 🔬 **Deep Research workflow** — arXiv / Semantic Scholar / Web with relevance filtering
- 🎨 **Frontend UI refactor** — Workbench, result page, template import, upgraded SVG-to-PPTX converter

</details>

<details>
<summary><b>April 2026</b></summary>

- 🔒 **Static Critic enhancements** — Decorative-line occlusion detection, low-contrast text detection, multiline width estimation fixes
- 📁 **Version history management** — Auto-archive snapshots per feedback iteration with diff & rollback
- 🔎 **Token log filtering** — Filter LLM calls by model / stage / page / task
- ⏹️ **Generation cancel** — Cancel a running pipeline task
- 🤖 **Multi-agent pipeline** — Strategist → Executor → Critic with SVG auto-repair and feedback iteration

</details>

## ⚠️ Known Limitations

- You must bring your own API key for at least one model provider (OpenAI / Anthropic / Gemini / DeepSeek or a custom-compatible API)
- Agent generation mode requires Claude Code or Codex installed and configured locally
- Visual QA is experimental and depends on a multi-modal model
- This release runs from source; no precompiled binary is provided yet

## 🙏 Acknowledgements

- [PPTAgent](https://github.com/icip-cas/PPTAgent) — Pipeline design and agent architecture reference
- [ppt-master](https://github.com/hugohe3/ppt-master) — Engineering implementation reference
- [PPTist](https://github.com/pipipi-pikachu/PPTist) — PPT editor reference and integration base

## 📄 License

Released under the [GNU Affero General Public License v3.0 (AGPL-3.0)](https://github.com/CRui5in/paper-ppt-agent/blob/master/LICENSE).

---

**Full Changelog**: https://github.com/CRui5in/paper-ppt-agent/commits/v1.0.0
