export interface FontOption {
  label: string;
  value: string;
}

export interface FontCategory {
  label: string;
  labelKey: string;
  fonts: FontOption[];
}

export const DEFAULT_PRESENTATION_FONT_STACK =
  '"Source Han Sans SC", "Noto Sans CJK SC", "Noto Sans SC", "PingFang SC", "Microsoft YaHei", Arial, sans-serif';

export const DEFAULT_EDITOR_FONT = "Source Han Sans SC";

export const FONT_CATEGORIES: FontCategory[] = [
  {
    label: "中文无衬线",
    labelKey: "font.catZhSans",
    fonts: [
      { label: "思源黑体", value: "Source Han Sans SC" },
      { label: "Noto Sans CJK SC", value: "Noto Sans CJK SC" },
      { label: "Noto Sans SC", value: "Noto Sans SC" },
      { label: "苹方", value: "PingFang SC" },
      { label: "微软雅黑", value: "Microsoft YaHei" },
      { label: "黑体", value: "SimHei" },
      { label: "等线", value: "DengXian" },
      { label: "华文黑体", value: "STHeiti" },
    ],
  },
  {
    label: "中文衬线",
    labelKey: "font.catZhSerif",
    fonts: [
      { label: "思源宋体", value: "Source Han Serif SC" },
      { label: "Noto Serif CJK SC", value: "Noto Serif CJK SC" },
      { label: "Noto Serif SC", value: "Noto Serif SC" },
      { label: "宋体", value: "SimSun" },
      { label: "楷体", value: "KaiTi" },
      { label: "仿宋", value: "FangSong" },
      { label: "华文宋体", value: "STSong" },
      { label: "华文楷体", value: "STKaiti" },
    ],
  },
  {
    label: "英文无衬线",
    labelKey: "font.catEnSans",
    fonts: [
      { label: "Arial", value: "Arial" },
      { label: "Calibri", value: "Calibri" },
      { label: "Segoe UI", value: "Segoe UI" },
      { label: "Helvetica", value: "Helvetica" },
      { label: "Helvetica Neue", value: "Helvetica Neue" },
      { label: "Inter", value: "Inter" },
      { label: "Roboto", value: "Roboto" },
      { label: "SF Pro", value: "SF Pro" },
      { label: "Verdana", value: "Verdana" },
    ],
  },
  {
    label: "英文衬线",
    labelKey: "font.catEnSerif",
    fonts: [
      { label: "Times New Roman", value: "Times New Roman" },
      { label: "Georgia", value: "Georgia" },
      { label: "Cambria", value: "Cambria" },
      { label: "Garamond", value: "Garamond" },
      { label: "Palatino", value: "Palatino" },
    ],
  },
  {
    label: "代码",
    labelKey: "font.catMono",
    fonts: [
      { label: "Consolas", value: "Consolas" },
      { label: "Cascadia Code", value: "Cascadia Code" },
      { label: "SF Mono", value: "SF Mono" },
      { label: "Monaco", value: "Monaco" },
      { label: "Menlo", value: "Menlo" },
      { label: "Courier New", value: "Courier New" },
    ],
  },
  {
    label: "CSS Generic",
    labelKey: "font.catGeneric",
    fonts: [
      { label: "sans-serif", value: "sans-serif" },
      { label: "serif", value: "serif" },
      { label: "monospace", value: "monospace" },
    ],
  },
];

function uniqueFontOptions(options: FontOption[]): FontOption[] {
  return options.filter(
    (item, index, array) =>
      array.findIndex((candidate) => candidate.value === item.value) === index,
  );
}

const zhSansFonts = FONT_CATEGORIES[0].fonts;
const zhSerifFonts = FONT_CATEGORIES[1].fonts;
const enSansFonts = FONT_CATEGORIES[2].fonts;
const enSerifFonts = FONT_CATEGORIES[3].fonts;
const monoFonts = FONT_CATEGORIES[4].fonts;

export const WESTERN_HEADING_FONT_OPTIONS: FontOption[] = uniqueFontOptions([
  { label: "Arial Black", value: "Arial Black" },
  { label: "Impact", value: "Impact" },
  ...enSansFonts,
  ...enSerifFonts,
]);

export const WESTERN_BODY_FONT_OPTIONS: FontOption[] = uniqueFontOptions([
  ...enSansFonts,
  ...enSerifFonts,
]);

export const CJK_HEADING_FONT_OPTIONS: FontOption[] = uniqueFontOptions([
  ...zhSansFonts,
  ...zhSerifFonts,
]);

export const CJK_BODY_FONT_OPTIONS: FontOption[] = uniqueFontOptions([
  ...zhSansFonts,
  ...zhSerifFonts,
]);

export const EDITOR_FONT_OPTIONS: FontOption[] = uniqueFontOptions([
  ...zhSansFonts,
  ...enSansFonts,
  ...zhSerifFonts,
  ...enSerifFonts,
  ...monoFonts,
]);

export function splitFontStack(stack: string): string[] {
  if (!stack.trim()) return [];
  return stack
    .split(",")
    .map((font) => font.trim().replace(/^['"]|['"]$/g, ""))
    .filter(Boolean);
}

export function joinFontStack(fonts: string[]): string {
  return fonts
    .map((font) => (font.includes(" ") ? `"${font}"` : font))
    .join(", ");
}

export function fontPreviewFamily(fontOrStack: string | undefined | null): string | undefined {
  const value = (fontOrStack ?? "").trim();
  return value || undefined;
}
