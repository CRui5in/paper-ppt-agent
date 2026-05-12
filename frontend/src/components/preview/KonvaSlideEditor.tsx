import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { AlignCenter, AlignLeft, AlignRight, Bold, Italic } from "lucide-react";
import Konva from "konva";
import { Group, Image as KonvaImage, Layer, Rect, Stage, Text, Transformer } from "react-konva";
import type { PreviewSlide, SlideDocument } from "../../lib/types";
import { useLocale } from "../../i18n";

export type EditorCommandType =
  | "addText"
  | "addRect"
  | "duplicate"
  | "delete"
  | "save"
  | "backward"
  | "forward"
  | "toggleAutosave"
  | "undo"
  | "redo"
  | "addImage"
  | "addTable";

export interface EditorCommand {
  id: number;
  type: EditorCommandType;
}

export interface EditorState {
  selectedType?: NodeKind;
  autoSave: boolean;
  saveState: "idle" | "dirty" | "saving" | "saved" | "error";
  canEdit: boolean;
  canUndo: boolean;
  canRedo: boolean;
}

interface KonvaSlideEditorProps {
  slide?: PreviewSlide;
  editable: boolean;
  command?: EditorCommand;
  onStateChange?: (state: EditorState) => void;
  onSave: (slide: PreviewSlide, content: string, document: SlideDocument) => Promise<void>;
}

type NodeKind = "text" | "rect" | "image" | "table";

interface BaseNode {
  id: string;
  type: NodeKind;
  x: number;
  y: number;
  width: number;
  height: number;
  rotation?: number;
  sourceTag?: string;
  sourceIndex?: number;
  committed?: boolean;
}

interface TextNode extends BaseNode {
  type: "text";
  text: string;
  fontSize: number;
  fontFamily: string;
  fill: string;
  fontStyle: string;
  align: "left" | "center" | "right";
}

interface RectNode extends BaseNode {
  type: "rect";
  fill: string;
  stroke: string;
  strokeWidth: number;
  cornerRadius: number;
}

interface ImageNode extends BaseNode {
  type: "image";
  src: string;
}

interface TableNode extends BaseNode {
  type: "table";
  rows: number;
  cols: number;
  fill: string;
  stroke: string;
  textFill: string;
  fontSize: number;
  cells: string[][];
}

type EditorNode = TextNode | RectNode | ImageNode | TableNode;

interface ParsedSlide {
  backgroundSvg: string;
  width: number;
  height: number;
  nodes: EditorNode[];
}

interface TextEditState {
  id: string;
  value: string;
  left: number;
  top: number;
  width: number;
  height: number;
  fontSize: number;
  fontFamily: string;
  color: string;
  fontStyle: string;
  align: "left" | "center" | "right";
}

interface ContextMenuState {
  left: number;
  top: number;
}

interface SelectionRectState {
  x: number;
  y: number;
  width: number;
  height: number;
}

const SVG_NS = "http://www.w3.org/2000/svg";
const DEFAULT_WIDTH = 1280;
const DEFAULT_HEIGHT = 720;
const AUTOSAVE_STORAGE_KEY = "paper-ppt-agent-slide-editor-autosave-v2";
const FONT_OPTIONS = ["Arial", "Calibri", "Inter", "Microsoft YaHei", "SimHei", "Times New Roman", "Georgia"];

export function KonvaSlideEditor({ slide, editable, command, onStateChange, onSave }: KonvaSlideEditorProps) {
  const { t } = useLocale();
  const containerRef = useRef<HTMLDivElement | null>(null);
  const stageRef = useRef<Konva.Stage | null>(null);
  const transformerRef = useRef<Konva.Transformer | null>(null);
  const saveTimerRef = useRef<number | null>(null);
  const imageInputRef = useRef<HTMLInputElement | null>(null);
  const textAreaRef = useRef<HTMLTextAreaElement | null>(null);
  const undoStackRef = useRef<EditorNode[][]>([]);
  const redoStackRef = useRef<EditorNode[][]>([]);
  const dragStateRef = useRef<{ id: string; startX: number; startY: number; snapshot: EditorNode[] } | null>(null);
  const [containerWidth, setContainerWidth] = useState(1200);
  const [parsed, setParsed] = useState<ParsedSlide>(() => parseSlide(""));
  const [nodes, setNodes] = useState<EditorNode[]>([]);
  const [selectedIds, setSelectedIds] = useState<string[]>([]);
  const [backgroundImage, setBackgroundImage] = useState<HTMLImageElement | null>(null);
  const [autoSave, setAutoSave] = useState(() => window.localStorage.getItem(AUTOSAVE_STORAGE_KEY) !== "0");
  const [saveState, setSaveState] = useState<EditorState["saveState"]>("idle");
  const [editingText, setEditingText] = useState<TextEditState | null>(null);
  const [contextMenu, setContextMenu] = useState<ContextMenuState | null>(null);
  const [selectionRect, setSelectionRect] = useState<SelectionRectState | null>(null);
  const selectionOriginRef = useRef<{ x: number; y: number } | null>(null);
  const selectionRectRef = useRef<SelectionRectState | null>(null);
  const [, bumpHistoryVersion] = useState(0);

  const scale = Math.min(containerWidth / parsed.width, 1.15);
  const stageWidth = parsed.width * scale;
  const stageHeight = parsed.height * scale;
  const selectedNodes = nodes.filter((node) => selectedIds.includes(node.id));
  const selectedNode = selectedIds.length === 1 ? selectedNodes[0] : undefined;
  const selectedType = selectedNodes[0]?.type;
  const selectedSourceKey = selectedNodes.map((node) => `${node.sourceTag ?? ""}:${node.sourceIndex ?? ""}`).join("|");
  const backgroundSvgForRender = useMemo(() => hideSourceNodes(composeBackgroundSvg(parsed.backgroundSvg, nodes), selectedNodes.filter((node) => node.sourceTag !== "text")), [parsed.backgroundSvg, nodes, selectedSourceKey]);

  useEffect(() => {
    const element = containerRef.current;
    if (!element) return undefined;
    const observer = new ResizeObserver((entries) => {
      const width = entries[0]?.contentRect.width;
      if (width) setContainerWidth(Math.max(360, width));
    });
    observer.observe(element);
    return () => observer.disconnect();
  }, []);

  useEffect(() => {
    const next = slide?.document ? documentToParsedSlide(slide.document, slide.content) : parseSlide(slide?.content ?? "");
    setParsed(next);
    setNodes(next.nodes);
    setSelectedIds([]);
    setEditingText(null);
    setContextMenu(null);
    setSelectionRect(null);
    selectionRectRef.current = null;
    selectionOriginRef.current = null;
    dragStateRef.current = null;
    undoStackRef.current = [];
    redoStackRef.current = [];
    setSaveState("idle");
  }, [slide?.index, slide?.content, slide?.document]);

  useEffect(() => {
    const image = new window.Image();
    image.onload = () => setBackgroundImage(image);
    image.onerror = () => setBackgroundImage(null);
    image.src = svgToDataUrl(backgroundSvgForRender);
  }, [backgroundSvgForRender]);

  useEffect(() => {
    const transformer = transformerRef.current;
    const stage = stageRef.current;
    if (!transformer || !stage) return;
    const targets = selectedIds.map((id) => stage.findOne(`#${id}`)).filter((node): node is Konva.Node => Boolean(node));
    transformer.nodes(targets);
    transformer.getLayer()?.batchDraw();
  }, [selectedIds, nodes, editingText]);

  useEffect(() => {
    window.localStorage.setItem(AUTOSAVE_STORAGE_KEY, autoSave ? "1" : "0");
  }, [autoSave]);

  useEffect(() => {
    onStateChange?.({
      selectedType,
      autoSave,
      saveState,
      canEdit: editable,
      canUndo: undoStackRef.current.length > 0,
      canRedo: redoStackRef.current.length > 0,
    });
  }, [autoSave, editable, onStateChange, saveState, selectedType, selectedIds.length, nodes]);

  const save = useCallback(async () => {
    if (!slide || !editable) return;
    setSaveState("saving");
    try {
      const content = composeSvg(parsed.backgroundSvg, nodes);
      const document = buildSlideDocument(parsed, nodes);
      await onSave(slide, content, document);
      setParsed((current) => ({ ...current, backgroundSvg: document.backgroundSvg }));
      setSaveState("saved");
      window.setTimeout(() => setSaveState((state) => (state === "saved" ? "idle" : state)), 1200);
    } catch {
      setSaveState("error");
    }
  }, [editable, nodes, onSave, parsed.backgroundSvg, slide]);

  useEffect(() => {
    if (!editable || !autoSave || saveState !== "dirty") return undefined;
    if (saveTimerRef.current) window.clearTimeout(saveTimerRef.current);
    saveTimerRef.current = window.setTimeout(() => void save(), 700);
    return () => {
      if (saveTimerRef.current) window.clearTimeout(saveTimerRef.current);
    };
  }, [autoSave, editable, save, saveState]);

  useEffect(() => {
    if (!editingText) return;
    const textarea = textAreaRef.current;
    if (!textarea) return;
    resizeTextAreaToContent(textarea);
    textarea.focus();
    textarea.select();
  }, [editingText?.id]);

  const markDirty = () => {
    if (editable) setSaveState("dirty");
  };

  const pushHistory = (snapshot: EditorNode[] = nodes) => {
    undoStackRef.current = [...undoStackRef.current.slice(-39), cloneNodes(snapshot)];
    redoStackRef.current = [];
    bumpHistoryVersion((value) => value + 1);
  };

  const setNodesWithHistory = (updater: (current: EditorNode[]) => EditorNode[]) => {
    setNodes((current) => {
      pushHistory(current);
      return updater(current);
    });
    markDirty();
  };

  const updateNode = (id: string, patch: Partial<EditorNode>) => {
    setNodesWithHistory((current) => current.map((node) => (node.id === id ? ({ ...node, ...patch } as EditorNode) : node)));
  };

  const selectNode = (id: string, additive = false) => {
    setSelectedIds((current) => {
      if (!additive) return [id];
      if (current.includes(id)) return current.filter((item) => item !== id);
      return [...current, id];
    });
  };

  const addText = () => {
    const node: TextNode = {
      id: createId("text"),
      type: "text",
      x: 96,
      y: 96,
      width: 460,
      height: 64,
      text: t("editor.defaultText"),
      fontSize: 36,
      fontFamily: "Microsoft YaHei",
      fill: "#0f172a",
      fontStyle: "normal",
    align: "left",
      committed: true,
    };
    setNodesWithHistory((current) => [...current, node]);
    setSelectedIds([node.id]);
  };

  const addRect = () => {
    const node: RectNode = {
      id: createId("rect"),
      type: "rect",
      x: 112,
      y: 112,
      width: 260,
      height: 128,
      fill: "#e0f2fe",
      stroke: "#2563eb",
      strokeWidth: 2,
    cornerRadius: 12,
      committed: true,
    };
    setNodesWithHistory((current) => [...current, node]);
    setSelectedIds([node.id]);
  };

  const addTable = () => {
    const node: TableNode = {
      id: createId("table"),
      type: "table",
      x: 128,
      y: 128,
      width: 520,
      height: 220,
      rows: 3,
      cols: 4,
      fill: "#ffffff",
      stroke: "#94a3b8",
      textFill: "#0f172a",
      fontSize: 18,
      cells: Array.from({ length: 3 }, (_, row) => Array.from({ length: 4 }, (_, col) => (row === 0 ? `${t("editor.tableHeader")} ${col + 1}` : ""))),
      committed: true,
    };
    setNodesWithHistory((current) => [...current, node]);
    setSelectedIds([node.id]);
  };

  const addImageFromFile = (file: File) => {
    const reader = new FileReader();
    reader.onload = () => {
      const src = String(reader.result || "");
      if (!src) return;
      const node: ImageNode = {
        id: createId("image"),
        type: "image",
        x: 120,
        y: 120,
        width: 360,
      height: 220,
      src,
        committed: true,
      };
      setNodesWithHistory((current) => [...current, node]);
      setSelectedIds([node.id]);
    };
    reader.readAsDataURL(file);
  };

  const deleteSelected = () => {
    if (!selectedIds.length) return;
    const selected = new Set(selectedIds);
    setNodesWithHistory((current) => current.filter((node) => !selected.has(node.id)));
    setSelectedIds([]);
  };

  const duplicateSelected = () => {
    if (!selectedNodes.length) return;
    const copies = selectedNodes.map((node) => ({ ...node, id: createId(node.type), x: node.x + 24, y: node.y + 24, committed: true, sourceTag: undefined, sourceIndex: undefined }) as EditorNode);
    setNodesWithHistory((current) => [...current, ...copies]);
    setSelectedIds(copies.map((node) => node.id));
  };

  const moveLayer = (direction: "forward" | "backward") => {
    if (!selectedIds.length) return;
    const selected = new Set(selectedIds);
    setNodesWithHistory((current) => {
      const next = [...current];
      if (direction === "forward") {
        for (let index = next.length - 2; index >= 0; index -= 1) {
          if (selected.has(next[index].id) && !selected.has(next[index + 1].id)) {
            [next[index], next[index + 1]] = [next[index + 1], next[index]];
          }
        }
      } else {
        for (let index = 1; index < next.length; index += 1) {
          if (selected.has(next[index].id) && !selected.has(next[index - 1].id)) {
            [next[index], next[index - 1]] = [next[index - 1], next[index]];
          }
        }
      }
      return next;
    });
  };

  const insertTableRow = (id: string) => {
    const table = nodes.find((node) => node.id === id);
    if (table?.type !== "table") return;
    updateNode(id, {
      rows: table.rows + 1,
      cells: [...table.cells, Array.from({ length: table.cols }, () => "")],
    } as Partial<EditorNode>);
  };

  const insertTableCol = (id: string) => {
    const table = nodes.find((node) => node.id === id);
    if (table?.type !== "table") return;
    updateNode(id, {
      cols: table.cols + 1,
      cells: table.cells.map((row) => [...row, ""]),
    } as Partial<EditorNode>);
  };

  const undo = () => {
    const previous = undoStackRef.current.pop();
    if (!previous) return;
    redoStackRef.current.push(cloneNodes(nodes));
    setNodes(previous);
    setSelectedIds([]);
    markDirty();
    bumpHistoryVersion((value) => value + 1);
  };

  const redo = () => {
    const next = redoStackRef.current.pop();
    if (!next) return;
    undoStackRef.current.push(cloneNodes(nodes));
    setNodes(next);
    setSelectedIds([]);
    markDirty();
    bumpHistoryVersion((value) => value + 1);
  };

  useEffect(() => {
    if (!command || !editable) return;
    if (command.type === "addText") addText();
    if (command.type === "addRect") addRect();
    if (command.type === "duplicate") duplicateSelected();
    if (command.type === "delete") deleteSelected();
    if (command.type === "backward") moveLayer("backward");
    if (command.type === "forward") moveLayer("forward");
    if (command.type === "undo") undo();
    if (command.type === "redo") redo();
    if (command.type === "addImage") imageInputRef.current?.click();
    if (command.type === "addTable") addTable();
    if (command.type === "toggleAutosave") setAutoSave((value) => !value);
    if (command.type === "save") void save();
  }, [command?.id]);

  useEffect(() => {
    if (!editable) return undefined;
    const onKeyDown = (event: KeyboardEvent) => {
      const meta = event.ctrlKey || event.metaKey;
      if (meta && event.key.toLowerCase() === "s") {
        event.preventDefault();
        void save();
      } else if (meta && event.key.toLowerCase() === "z" && !event.shiftKey) {
        event.preventDefault();
        undo();
      } else if ((meta && event.key.toLowerCase() === "y") || (meta && event.shiftKey && event.key.toLowerCase() === "z")) {
        event.preventDefault();
        redo();
      } else if (meta && event.key.toLowerCase() === "d") {
        event.preventDefault();
        duplicateSelected();
      } else if (meta && event.key.toLowerCase() === "a") {
        if (document.activeElement?.tagName === "TEXTAREA" || document.activeElement?.tagName === "INPUT") return;
        event.preventDefault();
        setSelectedIds(nodes.map((node) => node.id));
      } else if (event.key === "Delete" || event.key === "Backspace") {
        if (document.activeElement?.tagName === "TEXTAREA" || document.activeElement?.tagName === "INPUT") return;
        event.preventDefault();
        deleteSelected();
      } else if (event.key === "Escape") {
        setSelectedIds([]);
        setContextMenu(null);
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [editable, nodes, selectedIds, save]);

  const handleTransformEnd = (id: string, target: Konva.Node) => {
    const scaleX = target.scaleX();
    const scaleY = target.scaleY();
    target.scaleX(1);
    target.scaleY(1);
    const current = nodes.find((node) => node.id === id);
    if (!current) return;
    updateNode(id, {
      x: target.x(),
      y: target.y(),
      width: Math.max(current.type === "text" ? 1 : 18, current.width * scaleX),
      height: Math.max(current.type === "text" ? current.fontSize * 1.1 : 18, current.height * scaleY),
      rotation: target.rotation(),
    });
  };

  const handleDragStart = (id: string, target: Konva.Node) => {
    if (!selectedIds.includes(id)) setSelectedIds([id]);
    dragStateRef.current = {
      id,
      startX: target.x(),
      startY: target.y(),
      snapshot: cloneNodes(nodes),
    };
  };

  const handleDragMove = (id: string, target: Konva.Node) => {
    const dragState = dragStateRef.current;
    if (!dragState || dragState.id !== id) return;
    const selected = new Set(selectedIds.includes(id) ? selectedIds : [id]);
    if (selected.size <= 1) return;
    const dx = target.x() - dragState.startX;
    const dy = target.y() - dragState.startY;
    const next = dragState.snapshot.map((node) => (selected.has(node.id) ? ({ ...node, x: node.x + dx, y: node.y + dy } as EditorNode) : node));
    setNodes(next);
  };

  const handleDragEnd = (id: string, target: Konva.Node) => {
    const dragState = dragStateRef.current;
    dragStateRef.current = null;
    const selected = new Set(selectedIds.includes(id) ? selectedIds : [id]);
    if (dragState && dragState.id === id && selected.size > 1) {
      const dx = target.x() - dragState.startX;
      const dy = target.y() - dragState.startY;
      const next = dragState.snapshot.map((node) => (selected.has(node.id) ? ({ ...node, x: node.x + dx, y: node.y + dy } as EditorNode) : node));
      pushHistory(dragState.snapshot);
      setNodes(next);
      markDirty();
      return;
    }
    const current = nodes.find((node) => node.id === id);
    updateNode(id, {
      x: target.x(),
      y: target.y(),
    });
  };

  const beginTextEdit = (node: TextNode) => {
    if (!editable) return;
    setSelectedIds([node.id]);
    const editBox = textEditBox(node, scale);
    const origin = svgTextScreenOrigin(parsed.backgroundSvg, node, scale);
    setEditingText({
      id: node.id,
      value: node.text,
      left: origin.left,
      top: origin.top,
      width: editBox.width,
      height: editBox.height,
      fontSize: node.fontSize * scale,
      fontFamily: node.fontFamily,
      color: node.fill,
      fontStyle: node.fontStyle,
      align: node.align,
    });
  };

  const commitTextEdit = () => {
    if (!editingText) return;
    const textarea = textAreaRef.current;
    const node = nodes.find((item) => item.id === editingText.id);
    const minHeight = node?.type === "text" ? node.fontSize * 1.1 : 18;
    const height = textarea ? Math.max(minHeight, textarea.offsetHeight / scale) : undefined;
    updateNode(editingText.id, { text: editingText.value, height } as Partial<EditorNode>);
    setEditingText(null);
  };

  const stagePoint = () => {
    const pointer = stageRef.current?.getPointerPosition();
    return pointer ? { x: pointer.x / scale, y: pointer.y / scale } : null;
  };

  const selectNodesInRect = (rect: SelectionRectState, additive: boolean) => {
    const normalized = normalizeRect(rect);
    const hits = nodes.filter((node) => rectsIntersect(normalized, nodeBounds(node))).map((node) => node.id);
    setSelectedIds((current) => {
      if (!additive) return hits;
      const next = new Set(current);
      hits.forEach((id) => next.add(id));
      return Array.from(next);
    });
  };

  return (
    <div className={`konva-slide-editor ${editable ? "konva-slide-editor-editable" : "konva-slide-editor-readonly"}`} ref={containerRef}>
      <input
        ref={imageInputRef}
        className="konva-hidden-input"
        type="file"
        accept="image/*"
        onChange={(event) => {
          const file = event.target.files?.[0];
          if (file) addImageFromFile(file);
          event.target.value = "";
        }}
      />
      <div className="konva-stage-shell" style={{ width: stageWidth, height: stageHeight }}>
        <Stage
          ref={stageRef}
          width={stageWidth}
          height={stageHeight}
          scaleX={scale}
          scaleY={scale}
          onMouseDown={(event) => {
            setContextMenu(null);
            if (event.target === event.target.getStage()) {
              const point = stagePoint();
              if (!point) return;
              if (!(event.evt.ctrlKey || event.evt.metaKey || event.evt.shiftKey)) setSelectedIds([]);
              selectionOriginRef.current = point;
              const nextRect = { x: point.x, y: point.y, width: 0, height: 0 };
              selectionRectRef.current = nextRect;
              setSelectionRect(nextRect);
            }
          }}
          onMouseMove={() => {
            if (!selectionOriginRef.current) return;
            const point = stagePoint();
            if (!point) return;
            const origin = selectionOriginRef.current;
            const nextRect = { x: origin.x, y: origin.y, width: point.x - origin.x, height: point.y - origin.y };
            selectionRectRef.current = nextRect;
            setSelectionRect(nextRect);
          }}
          onMouseUp={(event) => {
            const rect = selectionRectRef.current;
            if (!selectionOriginRef.current || !rect) return;
            selectionOriginRef.current = null;
            selectionRectRef.current = null;
            setSelectionRect(null);
            if (Math.abs(rect.width) < 4 && Math.abs(rect.height) < 4) return;
            selectNodesInRect(rect, event.evt.ctrlKey || event.evt.metaKey || event.evt.shiftKey);
          }}
          onContextMenu={(event) => {
            event.evt.preventDefault();
            const stage = event.target.getStage();
            const pointer = stage?.getPointerPosition();
            const target = event.target;
            if (target !== stage && target.id()) {
              selectNode(target.id(), event.evt.ctrlKey || event.evt.metaKey || event.evt.shiftKey);
            }
            if (pointer) setContextMenu({ left: pointer.x, top: pointer.y });
          }}
        >
          <Layer listening={false}>
            {backgroundImage ? <KonvaImage image={backgroundImage} x={0} y={0} width={parsed.width} height={parsed.height} /> : null}
          </Layer>
          <Layer>
            {nodes.map((node) => {
              if (node.type === "text") return (
                <Text
                  key={node.id}
                  {...node}
                  width={textRenderWidth(node)}
                  height={textRenderHeight(node)}
                  wrap="none"
                  opacity={node.sourceTag === "text" ? 0.01 : editingText?.id === node.id ? 0 : 1}
                  draggable={editable}
                  onClick={(event) => { event.cancelBubble = true; selectNode(node.id, event.evt.ctrlKey || event.evt.metaKey || event.evt.shiftKey); }}
                  onTap={(event) => { event.cancelBubble = true; selectNode(node.id); }}
                  onDblClick={() => beginTextEdit(node)}
                  onDblTap={() => beginTextEdit(node)}
                  onDragStart={(event) => handleDragStart(node.id, event.target)}
                  onDragMove={(event) => handleDragMove(node.id, event.target)}
                  onDragEnd={(event) => handleDragEnd(node.id, event.target)}
                  onTransformEnd={(event) => handleTransformEnd(node.id, event.target)}
                />
              );
              if (node.type === "rect") return (
                <Rect
                  key={node.id}
                  {...node}
                  opacity={node.committed || selectedIds.includes(node.id) ? 1 : 0.01}
                  draggable={editable}
                  onClick={(event) => { event.cancelBubble = true; selectNode(node.id, event.evt.ctrlKey || event.evt.metaKey || event.evt.shiftKey); }}
                  onTap={(event) => { event.cancelBubble = true; selectNode(node.id); }}
                  onDragStart={(event) => handleDragStart(node.id, event.target)}
                  onDragMove={(event) => handleDragMove(node.id, event.target)}
                  onDragEnd={(event) => handleDragEnd(node.id, event.target)}
                  onTransformEnd={(event) => handleTransformEnd(node.id, event.target)}
                />
              );
              if (node.type === "image") return (
                <CanvasImage
                  key={node.id}
                  node={node}
                  editable={editable}
                  selected={selectedIds.includes(node.id)}
                  onSelect={(additive) => selectNode(node.id, additive)}
                  onDragStart={(target) => handleDragStart(node.id, target)}
                  onDragMove={(target) => handleDragMove(node.id, target)}
                  onDragEnd={(target) => handleDragEnd(node.id, target)}
                  onTransformEnd={(target) => handleTransformEnd(node.id, target)}
                />
              );
              return (
                <TableShape
                  key={node.id}
                  node={node}
                  editable={editable}
                  onSelect={(additive) => selectNode(node.id, additive)}
                  onDragStart={(target) => handleDragStart(node.id, target)}
                  onDragMove={(target) => handleDragMove(node.id, target)}
                  onDragEnd={(target) => handleDragEnd(node.id, target)}
                  onTransformEnd={(target) => handleTransformEnd(node.id, target)}
                />
              );
            })}
            {selectionRect ? (
              <Rect
                {...normalizeRect(selectionRect)}
                listening={false}
                fill="rgba(37, 99, 235, 0.08)"
                stroke="#2563eb"
                strokeWidth={1}
                dash={[6, 4]}
              />
            ) : null}
            <Transformer
              ref={transformerRef}
              rotateEnabled
              keepRatio={false}
              anchorSize={8}
              borderStroke="#2563eb"
              anchorStroke="#2563eb"
              anchorFill="#ffffff"
              enabledAnchors={["top-left", "top-center", "top-right", "middle-left", "middle-right", "bottom-left", "bottom-center", "bottom-right"]}
            />
          </Layer>
        </Stage>

        {editable && selectedNode ? (
          <FloatingTextTools
            node={selectedNode}
            scale={scale}
            onChange={(patch) => updateNode(selectedNode.id, patch)}
          />
        ) : null}

        {editingText ? (
          <textarea
            ref={textAreaRef}
            className="konva-inline-textarea"
            style={{
              left: editingText.left,
              top: editingText.top,
              width: editingText.width,
              height: editingText.height,
              fontSize: editingText.fontSize,
              fontFamily: editingText.fontFamily,
              color: editingText.color,
              fontWeight: editingText.fontStyle.includes("bold") ? 700 : 400,
              fontStyle: editingText.fontStyle.includes("italic") ? "italic" : "normal",
              textAlign: editingText.align,
            }}
            value={editingText.value}
            onMouseDown={(event) => event.stopPropagation()}
            onChange={(event) => {
              resizeTextAreaToContent(event.currentTarget);
              setEditingText((current) => current ? { ...current, value: event.target.value } : current);
            }}
            onBlur={commitTextEdit}
            onKeyDown={(event) => {
              if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) commitTextEdit();
              if (event.key === "Escape") setEditingText(null);
            }}
          />
        ) : null}
        {contextMenu ? (
          <div className="konva-context-menu" style={{ left: contextMenu.left, top: contextMenu.top }}>
            {selectedNode ? (
              <ContextMenuItems
                node={selectedNode}
                onEditText={() => {
                  if (selectedNode.type === "text") beginTextEdit(selectedNode);
                  setContextMenu(null);
                }}
                onDuplicate={() => { duplicateSelected(); setContextMenu(null); }}
                onDelete={() => { deleteSelected(); setContextMenu(null); }}
                onForward={() => { moveLayer("forward"); setContextMenu(null); }}
                onBackward={() => { moveLayer("backward"); setContextMenu(null); }}
                onInsertRow={() => { if (selectedNode.type === "table") insertTableRow(selectedNode.id); setContextMenu(null); }}
                onInsertCol={() => { if (selectedNode.type === "table") insertTableCol(selectedNode.id); setContextMenu(null); }}
                onReplaceImage={() => { imageInputRef.current?.click(); setContextMenu(null); }}
                t={t}
              />
            ) : (
              <>
                <strong>{t("editor.slide")}</strong>
                <button type="button" onClick={() => { addText(); setContextMenu(null); }}>{t("editor.newTextBox")}</button>
                <button type="button" onClick={() => { addRect(); setContextMenu(null); }}>{t("editor.newShape")}</button>
                <button type="button" onClick={() => { imageInputRef.current?.click(); setContextMenu(null); }}>{t("editor.insertPicture")}</button>
                <button type="button" onClick={() => { addTable(); setContextMenu(null); }}>{t("editor.insertTable")}</button>
                <span className="konva-menu-shortcut">{t("editor.shortcutSlide")}</span>
              </>
            )}
          </div>
        ) : null}
      </div>
    </div>
  );
}

function FloatingTextTools({
  node,
  scale,
  onChange,
}: {
  node: EditorNode;
  scale: number;
  onChange: (patch: Partial<EditorNode>) => void;
}) {
  const { t } = useLocale();
  const left = Math.max(8, node.x * scale);
  const top = Math.max(8, node.y * scale - 46);
  if (node.type === "rect") {
    return (
      <div className="konva-floating-tools" style={{ left, top }}>
        <input title={t("editor.fill")} type="color" value={normalizeColor(node.fill)} onChange={(event) => onChange({ fill: event.target.value } as Partial<EditorNode>)} />
        <input title={t("editor.stroke")} type="color" value={normalizeColor(node.stroke)} onChange={(event) => onChange({ stroke: event.target.value } as Partial<EditorNode>)} />
        <input title={t("editor.borderWidth")} type="number" min={0} max={20} value={node.strokeWidth} onChange={(event) => onChange({ strokeWidth: Number(event.target.value) } as Partial<EditorNode>)} />
      </div>
    );
  }
  if (node.type === "image") {
    return (
      <div className="konva-floating-tools" style={{ left, top }}>
        <span>{t("editor.picture")}</span>
      </div>
    );
  }
  if (node.type === "table") {
    return (
      <div className="konva-floating-tools" style={{ left, top }}>
        <span>{t("editor.table")}</span>
        <input title={t("editor.fill")} type="color" value={normalizeColor(node.fill)} onChange={(event) => onChange({ fill: event.target.value } as Partial<EditorNode>)} />
        <input title={t("editor.stroke")} type="color" value={normalizeColor(node.stroke)} onChange={(event) => onChange({ stroke: event.target.value } as Partial<EditorNode>)} />
      </div>
    );
  }
  return (
    <div className="konva-floating-tools" style={{ left, top }}>
      <select title={t("editor.font")} value={node.fontFamily} onChange={(event) => onChange({ fontFamily: event.target.value } as Partial<EditorNode>)}>
        {FONT_OPTIONS.map((font) => <option key={font} value={font}>{font}</option>)}
      </select>
      <input title={t("editor.fontSize")} type="number" min={8} max={160} value={node.fontSize} onChange={(event) => onChange({ fontSize: Number(event.target.value) } as Partial<EditorNode>)} />
      <input title={t("editor.color")} type="color" value={normalizeColor(node.fill)} onChange={(event) => onChange({ fill: event.target.value } as Partial<EditorNode>)} />
      <button title={t("editor.bold")} type="button" className={node.fontStyle.includes("bold") ? "active" : ""} onClick={() => onChange({ fontStyle: toggleFontStyle(node.fontStyle, "bold") } as Partial<EditorNode>)}><Bold size={14} /></button>
      <button title={t("editor.italic")} type="button" className={node.fontStyle.includes("italic") ? "active" : ""} onClick={() => onChange({ fontStyle: toggleFontStyle(node.fontStyle, "italic") } as Partial<EditorNode>)}><Italic size={14} /></button>
      <button title={t("editor.alignLeft")} type="button" className={node.align === "left" ? "active" : ""} onClick={() => onChange({ align: "left" } as Partial<EditorNode>)}><AlignLeft size={14} /></button>
      <button title={t("editor.alignCenter")} type="button" className={node.align === "center" ? "active" : ""} onClick={() => onChange({ align: "center" } as Partial<EditorNode>)}><AlignCenter size={14} /></button>
      <button title={t("editor.alignRight")} type="button" className={node.align === "right" ? "active" : ""} onClick={() => onChange({ align: "right" } as Partial<EditorNode>)}><AlignRight size={14} /></button>
    </div>
  );
}

function ContextMenuItems({
  node,
  onEditText,
  onDuplicate,
  onDelete,
  onForward,
  onBackward,
  onInsertRow,
  onInsertCol,
  onReplaceImage,
  t,
}: {
  node: EditorNode;
  onEditText: () => void;
  onDuplicate: () => void;
  onDelete: () => void;
  onForward: () => void;
  onBackward: () => void;
  onInsertRow: () => void;
  onInsertCol: () => void;
  onReplaceImage: () => void;
  t: (key: string) => string;
}) {
  const label = node.type === "text" ? t("editor.text") : node.type === "rect" ? t("editor.shape") : node.type === "image" ? t("editor.picture") : t("editor.table");
  return (
    <>
      <strong>{label}</strong>
      {node.type === "text" ? <button type="button" onClick={onEditText}>{t("editor.editText")}</button> : null}
      {node.type === "image" ? <button type="button" onClick={onReplaceImage}>{t("editor.replacePicture")}</button> : null}
      {node.type === "table" ? (
        <>
          <button type="button" onClick={onInsertRow}>{t("editor.insertRow")}</button>
          <button type="button" onClick={onInsertCol}>{t("editor.insertColumn")}</button>
        </>
      ) : null}
      <button type="button" onClick={onDuplicate}>{t("editor.duplicate")}</button>
      <button type="button" onClick={onForward}>{t("editor.bringForward")}</button>
      <button type="button" onClick={onBackward}>{t("editor.sendBackward")}</button>
      <button type="button" onClick={onDelete}>{t("editor.delete")}</button>
      <span className="konva-menu-shortcut">{t("editor.shortcutObject")}</span>
    </>
  );
}

function CanvasImage({
  node,
  editable,
  selected,
  onSelect,
  onDragStart,
  onDragMove,
  onDragEnd,
  onTransformEnd,
}: {
  node: ImageNode;
  editable: boolean;
  selected: boolean;
  onSelect: (additive: boolean) => void;
  onDragStart: (target: Konva.Node) => void;
  onDragMove: (target: Konva.Node) => void;
  onDragEnd: (target: Konva.Node) => void;
  onTransformEnd: (target: Konva.Node) => void;
}) {
  const [image, setImage] = useState<HTMLImageElement | null>(null);
  useEffect(() => {
    const img = new window.Image();
    img.onload = () => setImage(img);
    img.src = node.src;
  }, [node.src]);
  return (
    <KonvaImage
      id={node.id}
      image={image ?? undefined}
      x={node.x}
      y={node.y}
      width={node.width}
      height={node.height}
      rotation={node.rotation}
      opacity={node.committed || selected ? 1 : 0.01}
      draggable={editable}
      onClick={(event) => { event.cancelBubble = true; onSelect(event.evt.ctrlKey || event.evt.metaKey || event.evt.shiftKey); }}
      onTap={(event) => { event.cancelBubble = true; onSelect(false); }}
      onDragStart={(event) => onDragStart(event.target)}
      onDragMove={(event) => onDragMove(event.target)}
      onDragEnd={(event) => onDragEnd(event.target)}
      onTransformEnd={(event) => onTransformEnd(event.target)}
    />
  );
}

function TableShape({
  node,
  editable,
  onSelect,
  onDragStart,
  onDragMove,
  onDragEnd,
  onTransformEnd,
}: {
  node: TableNode;
  editable: boolean;
  onSelect: (additive: boolean) => void;
  onDragStart: (target: Konva.Node) => void;
  onDragMove: (target: Konva.Node) => void;
  onDragEnd: (target: Konva.Node) => void;
  onTransformEnd: (target: Konva.Node) => void;
}) {
  const cellW = node.width / node.cols;
  const cellH = node.height / node.rows;
  return (
    <Group
      id={node.id}
      x={node.x}
      y={node.y}
      rotation={node.rotation}
      draggable={editable}
      onClick={(event) => { event.cancelBubble = true; onSelect(event.evt.ctrlKey || event.evt.metaKey || event.evt.shiftKey); }}
      onTap={(event) => { event.cancelBubble = true; onSelect(false); }}
      onDragStart={(event) => onDragStart(event.target)}
      onDragMove={(event) => onDragMove(event.target)}
      onDragEnd={(event) => onDragEnd(event.target)}
      onTransformEnd={(event) => onTransformEnd(event.target)}
    >
      {Array.from({ length: node.rows }).map((_, row) =>
        Array.from({ length: node.cols }).map((__, col) => (
          <Group key={`${row}-${col}`} x={col * cellW} y={row * cellH}>
            <Rect width={cellW} height={cellH} fill={row === 0 ? "#f8fafc" : node.fill} stroke={node.stroke} strokeWidth={1} />
            <Text
              x={8}
              y={6}
              width={Math.max(10, cellW - 16)}
              height={Math.max(10, cellH - 12)}
              text={node.cells[row]?.[col] ?? ""}
              fill={node.textFill}
              fontSize={node.fontSize}
              fontFamily="Microsoft YaHei"
            />
          </Group>
        )),
      )}
    </Group>
  );
}

function parseSlide(content: string): ParsedSlide {
  if (!content.trim()) return { backgroundSvg: blankSvg(), width: DEFAULT_WIDTH, height: DEFAULT_HEIGHT, nodes: [] };
  try {
    const document = new DOMParser().parseFromString(content, "image/svg+xml");
    const root = document.documentElement;
    if (root.nodeName.toLowerCase() !== "svg") throw new Error("Not SVG");
    const nodes: EditorNode[] = [];
    const originalTextIndex = new Map<Element, number>();
    const originalRectIndex = new Map<Element, number>();
    const originalImageIndex = new Map<Element, number>();
    Array.from(root.querySelectorAll("text")).filter((element) => element.getAttribute("data-paper-editor") !== "text").forEach((element, index) => {
      originalTextIndex.set(element, index);
    });
    Array.from(root.querySelectorAll("rect")).filter((element) => element.getAttribute("data-paper-editor") !== "rect").forEach((element, index) => {
      originalRectIndex.set(element, index);
    });
    Array.from(root.querySelectorAll("image")).filter((element) => element.getAttribute("data-paper-editor") !== "image").forEach((element, index) => {
      originalImageIndex.set(element, index);
    });
    Array.from(root.querySelectorAll("text, rect, image, g[data-paper-editor='table']")).forEach((element, index) => {
      if (element.closest("g[data-paper-editor='table']") && element.getAttribute("data-paper-editor") !== "table") return;
      if (element.tagName.toLowerCase() === "text") {
        if (!isVisibleElement(element)) return;
        const inserted = element.getAttribute("data-paper-editor") === "text";
        const node = textFromElement(element as SVGTextElement, index, {
          committed: inserted,
          sourceTag: inserted ? undefined : "text",
          sourceIndex: inserted ? undefined : originalTextIndex.get(element),
        });
        nodes.push(node);
        if (inserted) element.remove();
        return;
      }
      if (element.tagName.toLowerCase() === "rect") {
        const inserted = element.getAttribute("data-paper-editor") === "rect";
        if (!inserted && !isEditableRect(element as SVGRectElement, root)) return;
        const node = rectFromElement(element as SVGRectElement, index, {
          committed: inserted,
          sourceTag: inserted ? undefined : "rect",
          sourceIndex: inserted ? undefined : originalRectIndex.get(element),
        });
        nodes.push(node);
        if (inserted) element.remove();
        return;
      }
      if (element.tagName.toLowerCase() === "image") {
        const inserted = element.getAttribute("data-paper-editor") === "image";
        nodes.push(imageFromElement(element as SVGImageElement, index, {
          committed: inserted,
          sourceTag: inserted ? undefined : "image",
          sourceIndex: inserted ? undefined : originalImageIndex.get(element),
        }));
        if (inserted) element.remove();
        return;
      }
      nodes.push(tableFromElement(element as SVGGElement, index));
      element.remove();
    });
    return { backgroundSvg: new XMLSerializer().serializeToString(root), ...readCanvas(root), nodes };
  } catch {
    return { backgroundSvg: content, width: DEFAULT_WIDTH, height: DEFAULT_HEIGHT, nodes: [] };
  }
}

function documentToParsedSlide(document: SlideDocument, fallbackSvg: string): ParsedSlide {
  const width = positiveNumber(document.width, DEFAULT_WIDTH);
  const height = positiveNumber(document.height, DEFAULT_HEIGHT);
  const backgroundSvg = document.backgroundSvg?.trim() || fallbackSvg || blankSvg();
  const documentNodes = document.elements.map(normalizeDocumentNode).filter((node): node is EditorNode => Boolean(node));
  const imageSourceIndexes = new Set(documentNodes.filter((node) => node.type === "image" && node.sourceTag === "image").map((node) => node.sourceIndex));
  const missingSourceImages = parseSlide(backgroundSvg).nodes.filter((node): node is ImageNode => (
    node.type === "image" &&
    node.sourceTag === "image" &&
    typeof node.sourceIndex === "number" &&
    !imageSourceIndexes.has(node.sourceIndex)
  ));
  return {
    backgroundSvg,
    width,
    height,
    nodes: [...documentNodes, ...missingSourceImages],
  };
}

function buildSlideDocument(parsed: ParsedSlide, nodes: EditorNode[]): SlideDocument {
  return {
    version: 1,
    width: parsed.width,
    height: parsed.height,
    backgroundSvg: composeBackgroundSvg(parsed.backgroundSvg, nodes),
    elements: cloneNodes(nodes) as unknown as SlideDocument["elements"],
  };
}

function normalizeDocumentNode(raw: unknown): EditorNode | null {
  if (!raw || typeof raw !== "object") return null;
  const node = raw as Partial<EditorNode> & { type?: string };
  const base = {
    id: typeof node.id === "string" ? node.id : createId(String(node.type || "node")),
    x: positiveNumber(node.x, 0),
    y: positiveNumber(node.y, 0),
    width: positiveNumber(node.width, 80),
    height: positiveNumber(node.height, 40),
    rotation: positiveNumber(node.rotation, 0),
    sourceTag: typeof node.sourceTag === "string" ? node.sourceTag : undefined,
    sourceIndex: typeof node.sourceIndex === "number" ? node.sourceIndex : undefined,
    committed: Boolean(node.committed),
  };
  if (node.type === "text") {
    return {
      ...base,
      type: "text",
      text: typeof node.text === "string" ? node.text : "Text",
      fontSize: positiveNumber(node.fontSize, 28),
      fontFamily: typeof node.fontFamily === "string" ? node.fontFamily : "Microsoft YaHei",
      fill: normalizeColor(typeof node.fill === "string" ? node.fill : "#0f172a"),
      fontStyle: typeof node.fontStyle === "string" ? node.fontStyle : "normal",
      align: node.align === "center" || node.align === "right" ? node.align : "left",
    };
  }
  if (node.type === "rect") {
    return {
      ...base,
      type: "rect",
      fill: normalizeColor(typeof node.fill === "string" ? node.fill : "#e0f2fe"),
      stroke: normalizeColor(typeof node.stroke === "string" ? node.stroke : "#2563eb"),
      strokeWidth: nonNegativeNumber(node.strokeWidth, 1),
      cornerRadius: positiveNumber(node.cornerRadius, 0),
    };
  }
  if (node.type === "image") {
    return {
      ...base,
      type: "image",
      src: typeof node.src === "string" ? node.src : "",
      committed: true,
    };
  }
  if (node.type === "table") {
    const rows = positiveNumber(node.rows, 3);
    const cols = positiveNumber(node.cols, 3);
    const cells = Array.isArray(node.cells) ? node.cells as string[][] : Array.from({ length: rows }, () => Array.from({ length: cols }, () => ""));
    return {
      ...base,
      type: "table",
      rows,
      cols,
      fill: normalizeColor(typeof node.fill === "string" ? node.fill : "#ffffff"),
      stroke: normalizeColor(typeof node.stroke === "string" ? node.stroke : "#94a3b8"),
      textFill: normalizeColor(typeof node.textFill === "string" ? node.textFill : "#0f172a"),
      fontSize: positiveNumber(node.fontSize, 18),
      cells,
      committed: true,
    };
  }
  return null;
}

function composeBackgroundSvg(backgroundSvg: string, nodes: EditorNode[]): string {
  return composeSvgFromNodes(backgroundSvg, nodes, false);
}

function hideSourceNodes(backgroundSvg: string, nodes: EditorNode[]): string {
  const sources = nodes
    .filter((node) => (node.sourceTag === "text" || node.sourceTag === "rect" || node.sourceTag === "image") && typeof node.sourceIndex === "number")
    .map((node) => ({ tag: node.sourceTag as "text" | "rect" | "image", index: node.sourceIndex as number }));
  if (!sources.length) return backgroundSvg;
  try {
    const document = new DOMParser().parseFromString(backgroundSvg, "image/svg+xml");
    const root = document.documentElement;
    sources.forEach((source) => {
      const element = root.querySelectorAll(source.tag)[source.index];
      if (!element) return;
      element.setAttribute("visibility", "hidden");
      element.setAttribute("opacity", "0");
    });
    return new XMLSerializer().serializeToString(root);
  } catch {
    return backgroundSvg;
  }
}

function composeSvg(backgroundSvg: string, nodes: EditorNode[]): string {
  return composeSvgFromNodes(backgroundSvg, nodes, true);
}

function composeSvgFromNodes(backgroundSvg: string, nodes: EditorNode[], appendCommitted: boolean): string {
  const document = new DOMParser().parseFromString(backgroundSvg, "image/svg+xml");
  const root = document.documentElement;
  nodes.forEach((node) => {
    if (node.sourceTag === "text" && typeof node.sourceIndex === "number") {
      const source = root.querySelectorAll("text")[node.sourceIndex];
      if (source && node.type === "text") {
        source.setAttribute("x", String(round(textAnchorX(node))));
        source.setAttribute("y", String(round(node.y + node.fontSize)));
        source.setAttribute("data-width", String(round(node.width)));
        source.setAttribute("data-height", String(round(node.height)));
        source.setAttribute("font-size", String(round(node.fontSize)));
        source.setAttribute("font-family", node.fontFamily);
        source.setAttribute("fill", node.fill);
        source.setAttribute("font-style", node.fontStyle.includes("italic") ? "italic" : "normal");
        source.setAttribute("font-weight", node.fontStyle.includes("bold") ? "700" : "400");
        source.setAttribute("text-anchor", node.align === "center" ? "middle" : node.align === "right" ? "end" : "start");
        source.removeAttribute("visibility");
        source.removeAttribute("opacity");
        setSvgTextContent(document, source, node);
      }
      return;
    }
    if (node.sourceTag === "rect" && typeof node.sourceIndex === "number") {
      const source = root.querySelectorAll("rect")[node.sourceIndex];
      if (source && node.type === "rect") {
        source.setAttribute("x", String(round(node.x)));
        source.setAttribute("y", String(round(node.y)));
        source.setAttribute("width", String(round(node.width)));
        source.setAttribute("height", String(round(node.height)));
        source.setAttribute("fill", node.fill);
        source.setAttribute("stroke", node.stroke);
        source.setAttribute("stroke-width", String(round(node.strokeWidth)));
        source.setAttribute("rx", String(round(node.cornerRadius)));
        source.removeAttribute("visibility");
        source.removeAttribute("opacity");
      }
      return;
    }
    if (node.sourceTag === "image" && typeof node.sourceIndex === "number") {
      const source = root.querySelectorAll("image")[node.sourceIndex];
      if (source && node.type === "image") {
        source.setAttribute("href", node.src);
        source.setAttribute("x", String(round(node.x)));
        source.setAttribute("y", String(round(node.y)));
        source.setAttribute("width", String(round(node.width)));
        source.setAttribute("height", String(round(node.height)));
        if (node.rotation) source.setAttribute("transform", `rotate(${round(node.rotation)} ${round(node.x)} ${round(node.y)})`);
        else source.removeAttribute("transform");
        source.removeAttribute("visibility");
        source.removeAttribute("opacity");
      }
      return;
    }
    if (!appendCommitted) return;
    if (node.type === "text") {
      const text = document.createElementNS(SVG_NS, "text");
      text.setAttribute("data-paper-editor", "text");
      text.setAttribute("x", String(round(textAnchorX(node))));
      text.setAttribute("y", String(round(node.y + node.fontSize)));
      text.setAttribute("data-width", String(round(node.width)));
      text.setAttribute("data-height", String(round(node.height)));
      text.setAttribute("font-size", String(round(node.fontSize)));
      text.setAttribute("font-family", node.fontFamily);
      text.setAttribute("fill", node.fill);
      text.setAttribute("font-style", node.fontStyle.includes("italic") ? "italic" : "normal");
      text.setAttribute("font-weight", node.fontStyle.includes("bold") ? "700" : "400");
      text.setAttribute("text-anchor", node.align === "center" ? "middle" : node.align === "right" ? "end" : "start");
      if (node.rotation) text.setAttribute("transform", `rotate(${round(node.rotation)} ${round(node.x)} ${round(node.y)})`);
      setSvgTextContent(document, text, node);
      root.appendChild(text);
    } else if (node.type === "rect") {
      const rect = document.createElementNS(SVG_NS, "rect");
      rect.setAttribute("data-paper-editor", "rect");
      rect.setAttribute("x", String(round(node.x)));
      rect.setAttribute("y", String(round(node.y)));
      rect.setAttribute("width", String(round(node.width)));
      rect.setAttribute("height", String(round(node.height)));
      rect.setAttribute("fill", node.fill);
      rect.setAttribute("stroke", node.stroke);
      rect.setAttribute("stroke-width", String(round(node.strokeWidth)));
      rect.setAttribute("rx", String(round(node.cornerRadius)));
      if (node.rotation) rect.setAttribute("transform", `rotate(${round(node.rotation)} ${round(node.x)} ${round(node.y)})`);
      root.appendChild(rect);
    } else if (node.type === "image") {
      const image = document.createElementNS(SVG_NS, "image");
      image.setAttribute("data-paper-editor", "image");
      image.setAttribute("href", node.src);
      image.setAttribute("x", String(round(node.x)));
      image.setAttribute("y", String(round(node.y)));
      image.setAttribute("width", String(round(node.width)));
      image.setAttribute("height", String(round(node.height)));
      if (node.rotation) image.setAttribute("transform", `rotate(${round(node.rotation)} ${round(node.x)} ${round(node.y)})`);
      root.appendChild(image);
    } else {
      const group = document.createElementNS(SVG_NS, "g");
      group.setAttribute("data-paper-editor", "table");
      group.setAttribute("data-rows", String(node.rows));
      group.setAttribute("data-cols", String(node.cols));
      group.setAttribute("data-width", String(round(node.width)));
      group.setAttribute("data-height", String(round(node.height)));
      group.setAttribute("data-cells", JSON.stringify(node.cells));
      group.setAttribute("transform", `translate(${round(node.x)} ${round(node.y)})${node.rotation ? ` rotate(${round(node.rotation)})` : ""}`);
      const cellW = node.width / node.cols;
      const cellH = node.height / node.rows;
      for (let row = 0; row < node.rows; row += 1) {
        for (let col = 0; col < node.cols; col += 1) {
          const rect = document.createElementNS(SVG_NS, "rect");
          rect.setAttribute("x", String(round(col * cellW)));
          rect.setAttribute("y", String(round(row * cellH)));
          rect.setAttribute("width", String(round(cellW)));
          rect.setAttribute("height", String(round(cellH)));
          rect.setAttribute("fill", row === 0 ? "#f8fafc" : node.fill);
          rect.setAttribute("stroke", node.stroke);
          rect.setAttribute("stroke-width", "1");
          group.appendChild(rect);
          const text = document.createElementNS(SVG_NS, "text");
          text.setAttribute("x", String(round(col * cellW + 8)));
          text.setAttribute("y", String(round(row * cellH + node.fontSize + 6)));
          text.setAttribute("font-size", String(node.fontSize));
          text.setAttribute("font-family", "Microsoft YaHei");
          text.setAttribute("fill", node.textFill);
          text.textContent = node.cells[row]?.[col] ?? "";
          group.appendChild(text);
        }
      }
      root.appendChild(group);
    }
  });
  return new XMLSerializer().serializeToString(root);
}

function textFromElement(element: SVGTextElement, index: number, overrides: Partial<BaseNode> = {}): TextNode {
  const matrix = cumulativeTransform(element);
  const fontSize = numberAttr(element, "font-size", inheritedNumberAttr(element, "font-size", 28)) * matrix.scale;
  const anchor = element.getAttribute("text-anchor");
  const firstTspan = element.querySelector("tspan");
  const rawX = numberAttr(element, "x", firstTspan ? numberAttr(firstTspan, "x", 96) : 96);
  const rawY = numberAttr(element, "y", firstTspan ? numberAttr(firstTspan, "y", 96) : 96);
  const text = extractSvgText(element);
  const metrics = textMetrics(element, text, fontSize);
  const align = anchor === "middle" ? "center" : anchor === "end" ? "right" : "left";
  const width = numberAttr(element, "data-width", metrics.width);
  const height = numberAttr(element, "data-height", metrics.height);
  const anchoredX = rawX * matrix.scale + matrix.x;
  const x = align === "center" ? anchoredX - width / 2 : align === "right" ? anchoredX - width : anchoredX;
  return {
    id: createId(`text-${index}`),
    type: "text",
    sourceTag: overrides.sourceTag,
    sourceIndex: overrides.sourceIndex,
    committed: overrides.committed ?? false,
    x,
    y: rawY * matrix.scale + matrix.y - fontSize,
    width,
    height,
    rotation: 0,
    text,
    fontSize,
    fontFamily: cleanFontFamily(element.getAttribute("font-family") || inheritedAttr(element, "font-family") || "Microsoft YaHei"),
    fill: normalizeColor(element.getAttribute("fill") || inheritedAttr(element, "fill") || "#0f172a"),
    fontStyle: [fontWeightToStyle(element.getAttribute("font-weight") || inheritedAttr(element, "font-weight")), element.getAttribute("font-style") === "italic" ? "italic" : ""].filter(Boolean).join(" ") || "normal",
    align,
  };
}

function rectFromElement(element: SVGRectElement, index: number, overrides: Partial<BaseNode> = {}): RectNode {
  const matrix = cumulativeTransform(element);
  const rawFill = element.getAttribute("fill") || "#e0f2fe";
  const rawStroke = element.getAttribute("stroke");
  const hasStroke = Boolean(rawStroke && rawStroke !== "none");
  return {
    id: createId(`rect-${index}`),
    type: "rect",
    sourceTag: overrides.sourceTag,
    sourceIndex: overrides.sourceIndex,
    committed: overrides.committed ?? false,
    x: numberAttr(element, "x", 0) * matrix.scale + matrix.x,
    y: numberAttr(element, "y", 0) * matrix.scale + matrix.y,
    width: numberAttr(element, "width", 120) * matrix.scale,
    height: numberAttr(element, "height", 72) * matrix.scale,
    rotation: 0,
    fill: normalizeColor(rawFill),
    stroke: normalizeColor(hasStroke ? rawStroke ?? rawFill : rawFill),
    strokeWidth: hasStroke ? numberAttr(element, "stroke-width", 1) : 0,
    cornerRadius: numberAttr(element, "rx", 0),
  };
}

function imageFromElement(element: SVGImageElement, index: number, overrides: Partial<BaseNode> = {}): ImageNode {
  const matrix = cumulativeTransform(element);
  return {
    id: createId(`image-${index}`),
    type: "image",
    sourceTag: overrides.sourceTag,
    sourceIndex: overrides.sourceIndex,
    committed: overrides.committed ?? false,
    x: numberAttr(element, "x", 0) * matrix.scale + matrix.x,
    y: numberAttr(element, "y", 0) * matrix.scale + matrix.y,
    width: numberAttr(element, "width", 240) * matrix.scale,
    height: numberAttr(element, "height", 160) * matrix.scale,
    rotation: 0,
    src: element.getAttribute("href") || element.getAttributeNS("http://www.w3.org/1999/xlink", "href") || "",
  };
}

function tableFromElement(element: SVGGElement, index: number): TableNode {
  const rows = numberAttr(element, "data-rows", 3);
  const cols = numberAttr(element, "data-cols", 3);
  let cells: string[][] = Array.from({ length: rows }, () => Array.from({ length: cols }, () => ""));
  try {
    const parsed = JSON.parse(element.getAttribute("data-cells") || "[]") as string[][];
    if (Array.isArray(parsed)) cells = parsed;
  } catch {
    // Keep default empty cells.
  }
  const transform = parseTranslate(element.getAttribute("transform"));
  return {
    id: createId(`table-${index}`),
    type: "table",
    committed: true,
    x: transform.x,
    y: transform.y,
    width: numberAttr(element, "data-width", 480),
    height: numberAttr(element, "data-height", 220),
    rotation: 0,
    rows,
    cols,
    fill: "#ffffff",
    stroke: "#94a3b8",
    textFill: "#0f172a",
    fontSize: 18,
    cells,
  };
}

function textMetrics(element: SVGTextElement, text: string, fontSize: number) {
  const tspanLines = Array.from(element.querySelectorAll("tspan")).map((item) => normalizeText(item.textContent || "")).filter(Boolean);
  const explicitLines = text.split(/\n+/).map((line) => line.trim()).filter(Boolean);
  const lines = tspanLines.length > 1 ? tspanLines : explicitLines.length ? explicitLines : [text];
  const longest = lines.reduce((max, line) => Math.max(max, visualTextLength(line)), 0);
  return {
    width: Math.max(fontSize * 2.4, longest * fontSize * 1.02),
    height: Math.max(fontSize * 1.25, lines.length * fontSize * 1.25),
  };
}

function extractSvgText(element: SVGTextElement) {
  const tspans = Array.from(element.querySelectorAll("tspan"));
  if (tspans.length) {
    const lines = tspans.map((item) => normalizeText(item.textContent || "")).filter(Boolean);
    if (lines.length) return lines.join("\n");
  }
  return normalizeText(element.textContent || "Text");
}

function setSvgTextContent(document: Document, element: Element, node: TextNode) {
  while (element.firstChild) element.removeChild(element.firstChild);
  const lines = node.text.split(/\n/);
  if (lines.length <= 1) {
    element.textContent = node.text;
    return;
  }
  const x = String(round(textAnchorX(node)));
  const baseY = node.y + node.fontSize;
  lines.forEach((line, index) => {
    const tspan = document.createElementNS(SVG_NS, "tspan");
    tspan.setAttribute("x", x);
    tspan.setAttribute("y", String(round(baseY + index * node.fontSize * 1.25)));
    tspan.textContent = line || " ";
    element.appendChild(tspan);
  });
}

function visualTextLength(text: string) {
  return Array.from(text).reduce((total, char) => total + (/[\u4e00-\u9fff]/.test(char) ? 1 : 0.55), 0);
}

function textAnchorX(node: TextNode) {
  if (node.align === "center") return node.x + node.width / 2;
  if (node.align === "right") return node.x + node.width;
  return node.x;
}

function normalizeRect(rect: SelectionRectState) {
  const x = rect.width < 0 ? rect.x + rect.width : rect.x;
  const y = rect.height < 0 ? rect.y + rect.height : rect.y;
  return {
    x,
    y,
    width: Math.abs(rect.width),
    height: Math.abs(rect.height),
  };
}

function nodeBounds(node: EditorNode) {
  if (node.type === "text") return { x: node.x, y: node.y, width: textRenderWidth(node), height: textRenderHeight(node) };
  return { x: node.x, y: node.y, width: node.width, height: node.height };
}

function rectsIntersect(a: SelectionRectState, b: SelectionRectState) {
  return a.x <= b.x + b.width && a.x + a.width >= b.x && a.y <= b.y + b.height && a.y + a.height >= b.y;
}

function isVisibleElement(element: Element) {
  return element.getAttribute("display") !== "none" && element.getAttribute("visibility") !== "hidden" && element.textContent?.trim();
}

function isEditableRect(element: SVGRectElement, root: Element) {
  if (element.getAttribute("data-paper-editor") === "rect") return true;
  const width = numberAttr(element, "width", 0);
  const height = numberAttr(element, "height", 0);
  const canvas = readCanvas(root);
  if (width >= canvas.width * 0.9 && height >= canvas.height * 0.9) return false;
  if (width * height > canvas.width * canvas.height * 0.18) return false;
  if (width < 18 || height < 18) return false;
  const fill = element.getAttribute("fill");
  const stroke = element.getAttribute("stroke");
  return Boolean(fill && fill !== "none") || Boolean(stroke && stroke !== "none");
}

function readCanvas(root: Element) {
  const viewBox = root.getAttribute("viewBox")?.split(/\s+/).map(Number);
  if (viewBox?.length === 4 && viewBox.every(Number.isFinite)) return { width: viewBox[2], height: viewBox[3] };
  return { width: numberAttr(root, "width", DEFAULT_WIDTH), height: numberAttr(root, "height", DEFAULT_HEIGHT) };
}

function blankSvg() {
  return `<svg xmlns="${SVG_NS}" viewBox="0 0 ${DEFAULT_WIDTH} ${DEFAULT_HEIGHT}" width="${DEFAULT_WIDTH}" height="${DEFAULT_HEIGHT}"><rect width="${DEFAULT_WIDTH}" height="${DEFAULT_HEIGHT}" fill="#ffffff"/></svg>`;
}

function svgToDataUrl(svg: string) {
  return `data:image/svg+xml;charset=utf-8,${encodeURIComponent(svg)}`;
}

function numberAttr(element: Element, name: string, fallback: number) {
  const value = Number.parseFloat(element.getAttribute(name) || "");
  return Number.isFinite(value) ? value : fallback;
}

function positiveNumber(value: unknown, fallback: number) {
  const parsed = typeof value === "number" ? value : Number.parseFloat(String(value ?? ""));
  return Number.isFinite(parsed) && parsed > 0 ? parsed : fallback;
}

function nonNegativeNumber(value: unknown, fallback: number) {
  const parsed = typeof value === "number" ? value : Number.parseFloat(String(value ?? ""));
  return Number.isFinite(parsed) && parsed >= 0 ? parsed : fallback;
}

function parseTranslate(transform: string | null) {
  const match = /translate\(([-\d.]+)[,\s]+([-\d.]+)\)/.exec(transform || "");
  return {
    x: match ? Number.parseFloat(match[1]) || 0 : 0,
    y: match ? Number.parseFloat(match[2]) || 0 : 0,
  };
}

function cloneNodes(nodes: EditorNode[]) {
  return JSON.parse(JSON.stringify(nodes)) as EditorNode[];
}

function textEditBox(node: TextNode, scale: number) {
  const lines = node.text.split(/\n+/).map((line) => line.trim()).filter(Boolean);
  const longest = (lines.length ? lines : [node.text]).reduce((max, line) => Math.max(max, visualTextLength(line)), 0);
  const contentWidth = longest * node.fontSize * scale * 1.08 + 20;
  const contentHeight = Math.max(1, lines.length || 1) * node.fontSize * scale * 1.28 + 10;
  return {
    width: Math.max(140, Math.round(node.width * scale), Math.ceil(contentWidth)),
    height: Math.max(34, Math.round(node.height * scale), Math.ceil(contentHeight)),
  };
}

function svgTextScreenOrigin(backgroundSvg: string, node: TextNode, scale: number) {
  if (node.sourceTag !== "text" || typeof node.sourceIndex !== "number") {
    return { left: Math.round(node.x * scale), top: Math.round(node.y * scale) };
  }
  try {
    const document = new DOMParser().parseFromString(backgroundSvg, "image/svg+xml");
    const source = document.documentElement.querySelectorAll("text")[node.sourceIndex];
    if (!source) return { left: Math.round(node.x * scale), top: Math.round(node.y * scale) };
    const rawX = numberAttr(source, "x", textAnchorX(node));
    const rawY = numberAttr(source, "y", node.y + node.fontSize);
    const align = source.getAttribute("text-anchor") === "middle" ? "center" : source.getAttribute("text-anchor") === "end" ? "right" : "left";
    const width = textRenderWidth(node);
    const x = align === "center" ? rawX - width / 2 : align === "right" ? rawX - width : rawX;
    return {
      left: Math.round(x * scale),
      top: Math.round((rawY - node.fontSize) * scale),
    };
  } catch {
    return { left: Math.round(node.x * scale), top: Math.round(node.y * scale) };
  }
}

function textRenderWidth(node: TextNode) {
  const lines = node.text.split(/\n+/).map((line) => line.trim()).filter(Boolean);
  const longest = (lines.length ? lines : [node.text]).reduce((max, line) => Math.max(max, visualTextLength(line)), 0);
  return Math.max(node.width, longest * node.fontSize * 1.06, node.fontSize * 2);
}

function textRenderHeight(node: TextNode) {
  const lines = Math.max(1, node.text.split(/\n+/).filter(Boolean).length);
  return Math.max(node.height, lines * node.fontSize * 1.25);
}

function resizeTextAreaToContent(textarea: HTMLTextAreaElement) {
  textarea.style.height = "auto";
  textarea.style.height = `${Math.max(textarea.offsetHeight, textarea.scrollHeight + 2)}px`;
  textarea.style.width = `${Math.max(textarea.offsetWidth, textarea.scrollWidth + 2)}px`;
}

function inheritedAttr(element: Element, name: string): string | null {
  let current: Element | null = element;
  while (current) {
    const value = current.getAttribute(name);
    if (value) return value;
    current = current.parentElement;
  }
  return null;
}

function inheritedNumberAttr(element: Element, name: string, fallback: number) {
  const value = inheritedAttr(element, name);
  if (!value) return fallback;
  const parsed = Number.parseFloat(value);
  return Number.isFinite(parsed) ? parsed : fallback;
}

function cumulativeTransform(element: Element) {
  let x = 0;
  let y = 0;
  let scale = 1;
  const chain: Element[] = [];
  let current: Element | null = element;
  while (current) {
    chain.unshift(current);
    current = current.parentElement;
  }
  chain.forEach((item) => {
    const transform = item.getAttribute("transform") || "";
    const translate = /translate\(([-\d.]+)(?:[,\s]+([-\d.]+))?\)/.exec(transform);
    if (translate) {
      x += (Number.parseFloat(translate[1]) || 0) * scale;
      y += (Number.parseFloat(translate[2] || "0") || 0) * scale;
    }
    const scaleMatch = /scale\(([-\d.]+)(?:[,\s]+([-\d.]+))?\)/.exec(transform);
    if (scaleMatch) {
      scale *= Number.parseFloat(scaleMatch[1]) || 1;
    }
    const matrix = /matrix\(([-\d.]+)[,\s]+([-\d.]+)[,\s]+([-\d.]+)[,\s]+([-\d.]+)[,\s]+([-\d.]+)[,\s]+([-\d.]+)\)/.exec(transform);
    if (matrix) {
      const a = Number.parseFloat(matrix[1]) || 1;
      const d = Number.parseFloat(matrix[4]) || a;
      x += Number.parseFloat(matrix[5]) || 0;
      y += Number.parseFloat(matrix[6]) || 0;
      scale *= Math.abs((a + d) / 2) || 1;
    }
  });
  return { x, y, scale };
}

function normalizeText(text: string) {
  return text.replace(/\s+/g, " ").trim() || "Text";
}

function cleanFontFamily(font: string) {
  return font.split(",")[0]?.replace(/['"]/g, "").trim() || "Microsoft YaHei";
}

function fontWeightToStyle(weight: string | null) {
  if (!weight) return "";
  return weight === "bold" || Number.parseInt(weight, 10) >= 600 ? "bold" : "";
}

function toggleFontStyle(style: string, token: "bold" | "italic") {
  const parts = new Set(style.split(/\s+/).filter((part) => part && part !== "normal"));
  if (parts.has(token)) parts.delete(token);
  else parts.add(token);
  return Array.from(parts).join(" ") || "normal";
}

function normalizeColor(value: string) {
  if (/^#[0-9a-f]{6}$/i.test(value)) return value;
  if (/^#[0-9a-f]{3}$/i.test(value)) return `#${value.slice(1).split("").map((char) => `${char}${char}`).join("")}`;
  return "#0f172a";
}

function createId(prefix: string) {
  return `${prefix}-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`;
}

function round(value: number) {
  return Math.round(value * 100) / 100;
}
