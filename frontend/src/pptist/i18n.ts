export type PptistLocale = 'en' | 'zh'

type Dictionary = Record<string, string>
type TextDictionary = Record<string, string>

const dictionaries: Record<PptistLocale, Dictionary> = {
  en: {
    'pptist.opening': 'Opening PPTist Studio ...',
    'pptist.loadingData': 'Loading PPTist data ...',
    'pptist.parsingPptx': 'Parsing PPTX ...',
    'pptist.importTimedOut': 'PPTX import timed out.',
    'pptist.pptxFallback': 'PPTX parsing failed. Preview fallback is now used.',
    'pptist.savingJson': 'Saving PPTist JSON ...',
    'pptist.exportingPptx': 'Exporting PPTX ...',
    'pptist.saved': 'Saved',
    'pptist.saveFailed': 'Save failed: {message}',
    'pptist.initFailed': 'PPTist initialization failed: {message}',
    'pptist.saveToPaper': 'Save to Paper PPT Agent',
    'pptist.save': 'Save',
    'pptist.saving': 'Saving',
    'pptist.slideshow': 'Slideshow (F5)',
    'pptist.fromStart': 'From beginning',
    'pptist.fromCurrent': 'From current slide',
    'pptist.downloadPptx': 'Download PPTX',
    'pptist.waitingOutput': 'Waiting output',
    'pptist.downloadOptions': 'Download options',
    'pptist.downloadLatest': 'Download latest PPTX',
    'pptist.importTemplate': 'Import',
    'pptist.importingTemplate': 'Importing',
    'pptist.cancelImport': 'Cancel import',
    'pptist.reexport': 'Re-export',
    'pptist.delete': 'Delete task',
    'pptist.deleting': 'Deleting',
    'pptist.deleteConfirm': 'Delete this task and local files? This action cannot be undone.',
    'pptist.expandProperties': 'Expand properties panel',
    'pptist.collapseProperties': 'Collapse properties panel',
    'pptist.properties': 'Properties',
    'pptist.byPptist': 'PPTist by pipipi-pikachu',
    'pptist.undo': 'Undo (Ctrl + Z)',
    'pptist.redo': 'Redo (Ctrl + Y)',
    'pptist.comments': 'Comments',
    'pptist.selectionPane': 'Selection pane',
    'pptist.searchReplace': 'Find / Replace (Ctrl + F)',
    'pptist.insertText': 'Insert text',
    'pptist.textbox': 'Text box',
    'pptist.horizontalText': 'Horizontal text box',
    'pptist.verticalText': 'Vertical text box',
    'pptist.insertShape': 'Insert shape',
    'pptist.shape': 'Shape',
    'pptist.presetShapes': 'Preset shapes',
    'pptist.freeDraw': 'Free draw',
    'pptist.insertImage': 'Insert image',
    'pptist.image': 'Image',
    'pptist.uploadImage': 'Upload image',
    'pptist.onlineGallery': 'Online gallery',
    'pptist.line': 'Line',
    'pptist.chart': 'Chart',
    'pptist.table': 'Table',
    'pptist.formula': 'Formula',
    'pptist.media': 'Audio/Video',
    'pptist.symbol': 'Symbol',
    'pptist.zoomOut': 'Zoom out (Ctrl + -)',
    'pptist.zoomIn': 'Zoom in (Ctrl + =)',
    'pptist.fitScreen': 'Fit to screen (Ctrl + 0)',
  },
  zh: {
    'pptist.opening': '正在打开 PPTist Studio ...',
    'pptist.loadingData': '正在加载 PPTist 数据 ...',
    'pptist.parsingPptx': '正在解析 PPTX ...',
    'pptist.importTimedOut': 'PPTX 导入超时。',
    'pptist.pptxFallback': 'PPTX 解析失败，已使用预览图兜底。',
    'pptist.savingJson': '正在保存 PPTist JSON ...',
    'pptist.exportingPptx': '正在导出 PPTX ...',
    'pptist.saved': '已保存',
    'pptist.saveFailed': '保存失败：{message}',
    'pptist.initFailed': 'PPTist 初始化失败：{message}',
    'pptist.saveToPaper': '保存到 Paper PPT Agent',
    'pptist.save': '保存',
    'pptist.saving': '保存中',
    'pptist.slideshow': '幻灯片放映（F5）',
    'pptist.fromStart': '从头开始',
    'pptist.fromCurrent': '从当前页开始',
    'pptist.downloadPptx': '下载 PPTX',
    'pptist.waitingOutput': '等待输出',
    'pptist.downloadOptions': '下载选项',
    'pptist.downloadLatest': '下载最新版 PPTX',
    'pptist.importTemplate': '导入',
    'pptist.importingTemplate': '导入中',
    'pptist.cancelImport': '取消导入',
    'pptist.reexport': '重新导出',
    'pptist.delete': '删除任务',
    'pptist.deleting': '删除中',
    'pptist.deleteConfirm': '是否要删除该任务以及本地文件？该操作不可撤销。',
    'pptist.expandProperties': '展开属性栏',
    'pptist.collapseProperties': '折叠属性栏',
    'pptist.properties': '属性',
    'pptist.byPptist': 'PPTist by pipipi-pikachu',
    'pptist.undo': '撤销（Ctrl + Z）',
    'pptist.redo': '重做（Ctrl + Y）',
    'pptist.comments': '批注面板',
    'pptist.selectionPane': '选择窗格',
    'pptist.searchReplace': '查找/替换（Ctrl + F）',
    'pptist.insertText': '插入文字',
    'pptist.textbox': '文本框',
    'pptist.horizontalText': '横向文本框',
    'pptist.verticalText': '竖向文本框',
    'pptist.insertShape': '插入形状',
    'pptist.shape': '形状',
    'pptist.presetShapes': '预设形状',
    'pptist.freeDraw': '自由绘制',
    'pptist.insertImage': '插入图片',
    'pptist.image': '图片',
    'pptist.uploadImage': '上传图片',
    'pptist.onlineGallery': '在线图库',
    'pptist.line': '线条',
    'pptist.chart': '图表',
    'pptist.table': '表格',
    'pptist.formula': '公式',
    'pptist.media': '音视频',
    'pptist.symbol': '符号',
    'pptist.zoomOut': '画布缩小（Ctrl + -）',
    'pptist.zoomIn': '画布放大（Ctrl + =）',
    'pptist.fitScreen': '适应屏幕（Ctrl + 0）',
  },
}

const englishUiText: TextDictionary = {
  '取消': 'Cancel',
  '确认': 'Confirm',
  '关闭': 'Close',
  '删除': 'Delete',
  '复制': 'Copy',
  '粘贴': 'Paste',
  '剪切': 'Cut',
  '全选': 'Select all',
  '解锁': 'Unlock',
  '锁定': 'Lock',
  '组合': 'Group',
  '取消组合': 'Ungroup',
  '水平居中': 'Align center',
  '垂直居中': 'Align middle',
  '水平垂直居中': 'Center on slide',
  '左对齐': 'Align left',
  '右对齐': 'Align right',
  '顶部对齐': 'Align top',
  '底部对齐': 'Align bottom',
  '置于顶层': 'Bring to front',
  '上移一层': 'Bring forward',
  '置于底层': 'Send to back',
  '下移一层': 'Send backward',
  '设置链接': 'Set link',
  '插入': 'Insert',
  '搜索': 'Search',
  '搜索图片': 'Search images',
  '加载中...': 'Loading...',
  '图片库（来自 pexels.com）': 'Image library (from pexels.com)',
  '全部': 'All',
  '横向': 'Landscape',
  '纵向': 'Portrait',
  '方形': 'Square',
  '导出格式：': 'Export format:',
  '导出范围：': 'Export range:',
  '当前页': 'Current slide',
  '自定义': 'Custom',
  '自定义范围：': 'Custom range:',
  '图片质量：': 'Image quality:',
  '忽略在线字体：': 'Ignore web fonts:',
  '导出图片': 'Export image',
  '导出 JSON': 'Export JSON',
  '正在导出...': 'Exporting...',
  '最近使用：': 'Recent:',
  '搜索字体': 'Search fonts',
  '搜索字号': 'Search sizes',
  '文字颜色': 'Text color',
  '单元格填充': 'Cell fill',
  '加粗': 'Bold',
  '斜体': 'Italic',
  '下划线': 'Underline',
  '删除线': 'Strikethrough',
  '两端对齐': 'Justify',
  '行数：': 'Rows:',
  '列数：': 'Columns:',
  '启用主题表格：': 'Use theme table:',
  '标题行': 'Header row',
  '汇总行': 'Total row',
  '第一列': 'First column',
  '最后一列': 'Last column',
  '主题颜色：': 'Theme color:',
  '编辑图表': 'Edit chart',
  '背景填充：': 'Background fill:',
  '坐标与文字：': 'Axes and text:',
  '网格颜色：': 'Grid color:',
  '主题配色：': 'Theme colors:',
  '预置图表主题：': 'Preset chart theme:',
  '幻灯片主题：': 'Slide theme:',
  '自定义配色': 'Custom palette',
  '图表主题配色': 'Chart theme colors',
  '添加主题色': 'Add theme color',
  '点击更换': 'Click to change',
  '点击替换形状': 'Click to replace shape',
  '纯色填充': 'Solid fill',
  '渐变填充': 'Gradient fill',
  '图片填充': 'Image fill',
  '线性渐变': 'Linear gradient',
  '径向渐变': 'Radial gradient',
  '当前色块：': 'Current color:',
  '渐变角度：': 'Gradient angle:',
  '行间距：': 'Line spacing:',
  '段间距：': 'Paragraph spacing:',
  '字间距：': 'Letter spacing:',
  '文本框填充：': 'Text box fill:',
  '上边距：': 'Top margin:',
  '下边距：': 'Bottom margin:',
  '左边距：': 'Left margin:',
  '右边距：': 'Right margin:',
  '顶对齐': 'Align top',
  '居中': 'Center',
  '底对齐': 'Align bottom',
  '双击连续使用': 'Double-click to keep using',
  '形状格式刷': 'Shape format painter',
  '启用滤镜：': 'Enable filter:',
  '模糊': 'Blur',
  '亮度': 'Brightness',
  '对比度': 'Contrast',
  '灰度': 'Grayscale',
  '饱和度': 'Saturation',
  '色相': 'Hue',
  '褐色': 'Sepia',
  '反转': 'Invert',
  '不透明度': 'Opacity',
  '黑白': 'Black and white',
  '复古': 'Vintage',
  '锐化': 'Sharpen',
  '柔和': 'Soft',
  '暖色': 'Warm',
  '明亮': 'Bright',
  '鲜艳': 'Vivid',
  '启用阴影：': 'Enable shadow:',
  '水平阴影：': 'Horizontal shadow:',
  '垂直阴影：': 'Vertical shadow:',
  '模糊距离：': 'Blur radius:',
  '阴影颜色：': 'Shadow color:',
  '着色（蒙版）：': 'Color mask:',
  '蒙版颜色：': 'Mask color:',
  '视频预览封面': 'Video poster',
  '设置首帧为封面': 'Use first frame as poster',
  '重置封面': 'Reset poster',
  '自动播放：': 'Autoplay:',
  '启用边框：': 'Enable border:',
  '边框样式：': 'Border style:',
  '边框颜色：': 'Border color:',
  '边框粗细：': 'Border width:',
  '减小段落缩进': 'Decrease paragraph indent',
  '增大段落缩进': 'Increase paragraph indent',
  '减小首行缩进': 'Decrease first-line indent',
  '增大首行缩进': 'Increase first-line indent',
  '不是正确的网页链接地址': 'Invalid web link.',
  '没有可以执行的文本内容': 'There is no text to process.',
  '按 ESC 键关闭取色吸管': 'Press ESC to close the color picker.',
  '取色吸管初始化失败': 'Failed to initialize the color picker.',
}

const skipDomI18nSelector = [
  '.canvas',
  '.canvas-wrapper',
  '.slide-thumbnail',
  '.thumbnail',
  '.thumbnail-item',
  '.paper-pptist-screen-portal',
  '.title',
  '[contenteditable="true"]',
  'input',
  'textarea',
  'select',
].join(',')

const translateTextValue = (value: string, locale: PptistLocale) => {
  if (locale !== 'en') return value
  const leading = value.match(/^\s*/)?.[0] ?? ''
  const trailing = value.match(/\s*$/)?.[0] ?? ''
  const normalized = value.trim().replace(/\s+/g, ' ')
  const translated = englishUiText[normalized]
  return translated ? `${leading}${translated}${trailing}` : value
}

export const getPptistLocale = (): PptistLocale => {
  const locale = (window as any).__PAPER_PPTIST_LOCALE__
  if (locale === 'en' || locale === 'zh') return locale
  return document.documentElement.lang?.toLowerCase().startsWith('en') ? 'en' : 'zh'
}

export const pptistT = (key: string, replacements?: Record<string, string>) => {
  const locale = getPptistLocale()
  let value = dictionaries[locale][key] ?? dictionaries.en[key] ?? key
  if (replacements) {
    for (const [name, replacement] of Object.entries(replacements)) {
      value = value.replace(`{${name}}`, replacement)
    }
  }
  return value
}

export const installPptistDomI18n = (root: HTMLElement, locale: PptistLocale) => {
  if (locale !== 'en') return () => undefined

  const shouldSkip = (node: Node) => {
    const element = node.nodeType === Node.ELEMENT_NODE
      ? (node as Element)
      : node.parentElement
    return !!element?.closest(skipDomI18nSelector)
  }

  const translateAttributes = (element: Element) => {
    if (shouldSkip(element)) return
    for (const attr of ['title', 'aria-label', 'placeholder', 'data-tippy-content']) {
      const value = element.getAttribute(attr)
      if (!value) continue
      const translated = translateTextValue(value, locale)
      if (translated !== value) element.setAttribute(attr, translated)
    }
  }

  const translateTree = (target: Node) => {
    if (shouldSkip(target)) return
    if (target.nodeType === Node.ELEMENT_NODE) translateAttributes(target as Element)
    if (target.nodeType === Node.TEXT_NODE) {
      const current = target.textContent ?? ''
      const translated = translateTextValue(current, locale)
      if (translated !== current) target.textContent = translated
      return
    }

    const walker = document.createTreeWalker(target, NodeFilter.SHOW_ELEMENT | NodeFilter.SHOW_TEXT)
    let next = walker.nextNode()
    while (next) {
      if (!shouldSkip(next)) {
        if (next.nodeType === Node.ELEMENT_NODE) translateAttributes(next as Element)
        if (next.nodeType === Node.TEXT_NODE) {
          const current = next.textContent ?? ''
          const translated = translateTextValue(current, locale)
          if (translated !== current) next.textContent = translated
        }
      }
      next = walker.nextNode()
    }
  }

  translateTree(root)
  const rootObserver = new MutationObserver(mutations => {
    for (const mutation of mutations) {
      if (mutation.type === 'attributes' && mutation.target instanceof Element) translateAttributes(mutation.target)
      if (mutation.type === 'characterData') translateTree(mutation.target)
      for (const node of Array.from(mutation.addedNodes)) translateTree(node)
    }
  })
  rootObserver.observe(root, {
    subtree: true,
    childList: true,
    characterData: true,
    attributes: true,
    attributeFilter: ['title', 'aria-label', 'placeholder', 'data-tippy-content'],
  })

  const overlayObserver = new MutationObserver(mutations => {
    for (const mutation of mutations) {
      for (const node of Array.from(mutation.addedNodes)) {
        if (!(node instanceof Element)) continue
        if (node.matches('.tippy-box, .tippy-box *') || node.querySelector('.tippy-box')) translateTree(node)
      }
    }
  })
  overlayObserver.observe(document.body, { childList: true, subtree: true })

  return () => {
    rootObserver.disconnect()
    overlayObserver.disconnect()
  }
}
