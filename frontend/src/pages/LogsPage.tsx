import { useCallback, useEffect, useMemo, useState } from "react";
import ReactECharts from "echarts-for-react";
import type { EChartsOption } from "echarts";
import { Layout } from "../components/layout/Layout";
import { useLocale } from "../i18n";
import { fetchUsageSnapshot } from "../lib/api";
import { translateStageStatus } from "../lib/i18nStatus";
import { openUsageSocket } from "../lib/ws";
import { Button } from "../components/ui/button";
import { Input } from "../components/ui/input";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "../components/ui/select";

interface DailyRow {
  day: string;
  calls: number;
  prompt_tokens: number;
  completion_tokens: number;
  total_tokens: number;
}

interface ModelRow {
  model: string;
  calls: number;
  prompt_tokens: number;
  completion_tokens: number;
  total_tokens: number;
}

interface StageRow {
  stage: string;
  calls: number;
  prompt_tokens: number;
  completion_tokens: number;
  total_tokens: number;
}

interface UsageRecord {
  ts: string;
  day: string;
  provider: string;
  model: string;
  prompt_tokens: number;
  completion_tokens: number;
  total_tokens: number;
  job_id: string | null;
  stage: string | null;
  page: number | null;
  attempt: number;
  duration_ms: number;
}

interface Summary {
  total_calls: number;
  total_prompt: number;
  total_completion: number;
  total_tokens: number;
}

const EMPTY_SUMMARY: Summary = {
  total_calls: 0,
  total_prompt: 0,
  total_completion: 0,
  total_tokens: 0,
};

function formatNumber(n: number): string {
  return new Intl.NumberFormat().format(n);
}

function usageRecordKey(record: UsageRecord): string {
  return [
    record.ts,
    record.provider,
    record.model,
    record.job_id ?? "",
    record.stage ?? "",
    record.page ?? "",
    record.attempt,
    record.prompt_tokens,
    record.completion_tokens,
    record.duration_ms,
  ].join("|");
}

export function LogsPage() {
  const { t, locale } = useLocale();
  const [summary, setSummary] = useState<Summary>(EMPTY_SUMMARY);
  const [daily, setDaily] = useState<DailyRow[]>([]);
  const [byModel, setByModel] = useState<ModelRow[]>([]);
  const [byStage, setByStage] = useState<StageRow[]>([]);
  const [records, setRecords] = useState<UsageRecord[]>([]);
  const [connected, setConnected] = useState(false);
  const [chartRevision, setChartRevision] = useState(0);

  // Filters
  const [filterStage, setFilterStage] = useState("");
  const [filterModel, setFilterModel] = useState("");
  const [filterPage, setFilterPage] = useState("");
  const [filterJob, setFilterJob] = useState("");
  const [selectedRecord, setSelectedRecord] = useState<UsageRecord | null>(null);

  useEffect(() => {
    const root = document.documentElement;
    const refreshCharts = () => {
      setChartRevision((current) => current + 1);
    };
    const observer = new MutationObserver(refreshCharts);
    observer.observe(root, { attributes: true, attributeFilter: ["data-theme"] });
    const frame = window.requestAnimationFrame(refreshCharts);
    return () => {
      observer.disconnect();
      window.cancelAnimationFrame(frame);
    };
  }, []);

  useEffect(() => {
    let disposed = false;
    let hydratedFromSocket = false;

    fetchUsageSnapshot()
      .then((snapshot) => {
        if (disposed || hydratedFromSocket) {
          return;
        }
        setSummary(snapshot.summary ?? EMPTY_SUMMARY);
        setDaily(snapshot.daily ?? []);
        setByModel(snapshot.by_model ?? []);
        setByStage(snapshot.by_stage ?? []);
        setRecords(snapshot.recent ?? []);
      })
      .catch(() => {
        // Keep the page usable even if the realtime socket is unavailable.
      });

    const socket = openUsageSocket(
      (event) => {
        if (event.type === "snapshot") {
          hydratedFromSocket = true;
          setSummary((event.summary as Summary) ?? EMPTY_SUMMARY);
          setDaily((event.daily as DailyRow[]) ?? []);
          setByModel((event.by_model as ModelRow[]) ?? []);
          setByStage((event.by_stage as StageRow[]) ?? []);
          setRecords((event.recent as UsageRecord[]) ?? []);
          return;
        }
        if (event.type === "usage") {
          const rec = event.record as UsageRecord;
          setRecords((prev) => [rec, ...prev].slice(0, 200));
          setSummary((prev) => ({
            total_calls: prev.total_calls + 1,
            total_prompt: prev.total_prompt + rec.prompt_tokens,
            total_completion: prev.total_completion + rec.completion_tokens,
            total_tokens: prev.total_tokens + rec.total_tokens,
          }));
          setByModel((prev) => mergeRow(prev, "model", rec.model, rec));
          setByStage((prev) =>
            mergeRow(prev, "stage", rec.stage ?? "(unknown)", rec),
          );
          setDaily((prev) => mergeRow(prev, "day", rec.day, rec));
        }
      },
      () => setConnected(true),
      () => setConnected(false),
    );

    return () => {
      disposed = true;
      socket.close();
    };
  }, []);

  const dailyRows = useMemo(
    () => [...daily].sort((left, right) => left.day.localeCompare(right.day)),
    [daily],
  );
  const topModels = useMemo(
    () => [...byModel].sort((left, right) => right.total_tokens - left.total_tokens).slice(0, 6),
    [byModel],
  );
  const stageRows = useMemo(
    () => [...byStage].sort((left, right) => right.total_tokens - left.total_tokens),
    [byStage],
  );
  const stageTotalTokens = useMemo(
    () => stageRows.reduce((sum, row) => sum + row.total_tokens, 0),
    [stageRows],
  );

  const uniqueStages = useMemo(() => {
    const set = new Set(records.map((r) => r.stage).filter(Boolean));
    return Array.from(set).sort() as string[];
  }, [records]);
  const uniqueModels = useMemo(() => {
    const set = new Set(records.map((r) => r.model));
    return Array.from(set).sort();
  }, [records]);

  const filteredRecords = useMemo(() => {
    let items = records;
    if (filterStage) items = items.filter((r) => r.stage === filterStage);
    if (filterModel) items = items.filter((r) => r.model === filterModel);
    if (filterPage) items = items.filter((r) => r.page != null && String(r.page) === filterPage);
    if (filterJob) items = items.filter((r) => r.job_id?.startsWith(filterJob));
    return items;
  }, [records, filterStage, filterModel, filterPage, filterJob]);
  const selectedRecordKey = selectedRecord ? usageRecordKey(selectedRecord) : "";

  const clearFilters = useCallback(() => {
    setFilterStage("");
    setFilterModel("");
    setFilterPage("");
    setFilterJob("");
  }, []);

  const formatter = new Intl.DateTimeFormat(locale === "zh" ? "zh-CN" : "en-US", {
    month: "numeric",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });

  const chartColors = useMemo(() => {
    const isDark = document.documentElement.dataset.theme === "dark";
    const styles = window.getComputedStyle(document.documentElement);
    const text = styles.getPropertyValue("--text").trim() || (isDark ? "#f8fafc" : "#191b23");
    const surfaceStrong = styles.getPropertyValue("--surface-strong").trim() || (isDark ? "#0f172a" : "#ffffff");
    return {
      text: isDark ? "#f8fafc" : text,
      muted: isDark ? "#cbd5e1" : "#64748b",
      line: isDark ? "rgba(148, 163, 184, 0.2)" : "#dfe5f0",
      surfaceStrong,
      tooltipBg: isDark ? "rgba(15, 23, 42, 0.98)" : "rgba(255, 255, 255, 0.99)",
      tooltipText: isDark ? "#f8fafc" : "#191b23",
      tooltipBorder: isDark ? "rgba(148, 163, 184, 0.3)" : "#dfe5f0",
    };
  }, [chartRevision]);

  const tooltipPosition = useMemo(
    () =>
      (
        point: number[],
        _params: unknown,
        _dom: unknown,
        _rect: unknown,
        size: { contentSize: number[]; viewSize: number[] },
      ) => {
        const [mouseX, mouseY] = point;
        const [contentWidth, contentHeight] = size.contentSize;
        const [viewWidth, viewHeight] = size.viewSize;
        return [
          Math.min(viewWidth - contentWidth - 12, Math.max(12, mouseX + 14)),
          Math.min(viewHeight - contentHeight - 12, Math.max(12, mouseY + 14)),
        ];
      },
    [],
  );

  const dailyOption = useMemo<EChartsOption>(() => ({
    animationDuration: 700,
    animationDurationUpdate: 450,
    color: ["#1A5AD7", "#06B6D4"],
    textStyle: { color: chartColors.text, fontFamily: "Inter, Manrope, Segoe UI, sans-serif" },
    grid: { left: 18, right: 20, top: 26, bottom: 18, containLabel: true },
    tooltip: {
      appendToBody: true,
      trigger: "axis",
      position: tooltipPosition,
      backgroundColor: chartColors.tooltipBg,
      borderColor: chartColors.tooltipBorder,
      borderWidth: 1,
      textStyle: { color: chartColors.tooltipText, fontSize: 13, fontWeight: 600 },
      extraCssText: "border-radius:12px;padding:10px 12px;line-height:1.5;box-shadow:0 16px 36px rgba(0,0,0,0.18);",
      valueFormatter: (value) => formatNumber(Number(value ?? 0)),
    },
    xAxis: {
      type: "category",
      boundaryGap: false,
      data: dailyRows.map((row) => row.day.slice(5)),
      axisLine: { lineStyle: { color: chartColors.line } },
      axisTick: { show: false },
      axisLabel: { color: chartColors.muted, fontSize: 12, fontWeight: 600 },
    },
    yAxis: {
      type: "value",
      splitNumber: 4,
      axisLine: { show: false },
      axisTick: { show: false },
      axisLabel: {
        color: chartColors.muted,
        fontSize: 12,
        fontWeight: 600,
        formatter: (value: number) => formatNumber(value),
      },
      splitLine: { lineStyle: { color: chartColors.line } },
    },
    series: [
      {
        type: "line",
        smooth: true,
        showSymbol: false,
        symbol: "circle",
        symbolSize: 8,
        data: dailyRows.map((row) => row.total_tokens),
        lineStyle: { width: 3, color: "#1A5AD7" },
        itemStyle: { color: "#06B6D4" },
        areaStyle: {
          color: {
            type: "linear",
            x: 0,
            y: 0,
            x2: 0,
            y2: 1,
            colorStops: [
              { offset: 0, color: "rgba(26, 90, 215, 0.2)" },
              { offset: 1, color: "rgba(6, 182, 212, 0.02)" },
            ],
          },
        },
      },
    ],
  }), [chartColors, dailyRows, tooltipPosition]);

  const modelOption = useMemo<EChartsOption>(() => ({
    animationDuration: 650,
    animationDurationUpdate: 400,
    color: ["#1A5AD7", "#06B6D4"],
    textStyle: { color: chartColors.text, fontFamily: "Inter, Manrope, Segoe UI, sans-serif" },
    grid: { left: 18, right: 18, top: 18, bottom: 12, containLabel: true },
    tooltip: {
      appendToBody: true,
      trigger: "axis",
      position: tooltipPosition,
      axisPointer: { type: "shadow" },
      backgroundColor: chartColors.tooltipBg,
      borderColor: chartColors.tooltipBorder,
      borderWidth: 1,
      textStyle: { color: chartColors.tooltipText, fontSize: 13, fontWeight: 600 },
      extraCssText: "border-radius:12px;padding:10px 12px;line-height:1.5;box-shadow:0 16px 36px rgba(0,0,0,0.18);",
      valueFormatter: (value) => formatNumber(Number(value ?? 0)),
    },
    xAxis: {
      type: "value",
      axisLine: { show: false },
      axisTick: { show: false },
      axisLabel: {
        color: chartColors.muted,
        fontSize: 12,
        fontWeight: 600,
        formatter: (value: number) => formatNumber(value),
      },
      splitLine: { lineStyle: { color: chartColors.line } },
    },
    yAxis: {
      type: "category",
      inverse: true,
      data: topModels.map((row) => row.model),
      axisLine: { show: false },
      axisTick: { show: false },
      axisLabel: {
        color: chartColors.text,
        fontSize: 12,
        fontWeight: 600,
        width: 126,
        overflow: "truncate",
      },
    },
    series: [
      {
        type: "bar",
        barWidth: 14,
        data: topModels.map((row) => row.total_tokens),
        itemStyle: {
          borderRadius: [0, 999, 999, 0],
          color: {
            type: "linear",
            x: 0,
            y: 0,
            x2: 1,
            y2: 0,
            colorStops: [
              { offset: 0, color: "#06B6D4" },
              { offset: 1, color: "#1A5AD7" },
            ],
          },
        },
      },
    ],
  }), [chartColors, topModels, tooltipPosition]);

  const stageOption = useMemo<EChartsOption>(() => ({
    animationDuration: 700,
    animationDurationUpdate: 450,
    color: STAGE_COLORS,
    textStyle: { color: chartColors.text, fontFamily: "Inter, Manrope, Segoe UI, sans-serif" },
    grid: { left: 84, right: 92, top: 16, bottom: 18, containLabel: false },
    tooltip: {
      appendToBody: true,
      trigger: "axis",
      position: tooltipPosition,
      axisPointer: { type: "shadow" },
      backgroundColor: chartColors.tooltipBg,
      borderColor: chartColors.tooltipBorder,
      borderWidth: 1,
      textStyle: { color: chartColors.tooltipText, fontSize: 13, fontWeight: 600 },
      extraCssText: "border-radius:12px;padding:10px 12px;line-height:1.5;box-shadow:0 16px 36px rgba(0,0,0,0.18);",
      formatter: (params: any) => {
        const item = Array.isArray(params) ? params[0] : params;
        const data = item?.data as { value?: number; tokens?: number } | undefined;
        return `${String(item?.name ?? "")}<br/>${(Number(data?.value ?? 0)).toFixed(2)}% · ${formatNumber(Number(data?.tokens ?? 0))} tokens`;
      },
    },
    xAxis: {
      type: "value",
      min: 0,
      max: 100,
      axisLine: { show: false },
      axisTick: { show: false },
      axisLabel: {
        color: chartColors.muted,
        fontSize: 11,
        formatter: "{value}%",
      },
      splitLine: { lineStyle: { color: chartColors.line } },
    },
    yAxis: {
      type: "category",
      inverse: true,
      data: stageRows.map((row) => translateStageStatus(row.stage, locale, "logs")),
      axisLine: { show: false },
      axisTick: { show: false },
      axisLabel: {
        color: chartColors.text,
        fontSize: 12,
        fontWeight: 700,
        width: 72,
        overflow: "truncate",
      },
    },
    series: [
      {
        type: "bar",
        barWidth: 16,
        barGap: "42%",
        data: stageRows.map((row, idx) => {
          const percent = stageTotalTokens > 0 ? (row.total_tokens / stageTotalTokens) * 100 : 0;
          return {
            value: Number(percent.toFixed(4)),
            tokens: row.total_tokens,
            itemStyle: {
              color: STAGE_COLORS[idx % STAGE_COLORS.length],
              borderRadius: [0, 999, 999, 0],
            },
          };
        }),
        label: {
          show: true,
          position: "right",
          color: chartColors.text,
          fontSize: 12,
          fontWeight: 750,
          formatter: (params: any) => `${Number(params?.value ?? 0).toFixed(2)}%`,
        },
      },
    ],
  }), [chartColors, locale, stageRows, stageTotalTokens, tooltipPosition]);

  const subtitle = t("logs.subtitle");

  return (
    <Layout showSidebar={false} contentClassName="studio-page">
      <section className="logs-page logs-workspace-page">
        <header className="logs-header">
          <h1>{t("logs.title")}</h1>
          <p className="muted-copy">
            {subtitle ? subtitle : null}
            <span
              className={`logs-status ${connected ? "logs-status-on" : "logs-status-off"}`}
            >
              {connected ? t("logs.live") : t("logs.offline")}
            </span>
          </p>
        </header>

        <section className="logs-summary">
          <SummaryCard label={t("logs.calls")} value={summary.total_calls} />
          <SummaryCard label={t("logs.promptTokens")} value={summary.total_prompt} />
          <SummaryCard label={t("logs.completionTokens")} value={summary.total_completion} />
          <SummaryCard label={t("logs.totalTokens")} value={summary.total_tokens} />
        </section>

        <section className="logs-grid">
          <article className="logs-card">
            <h2>{t("logs.dailyTitle")}</h2>
            <ChartPanel
              hasData={dailyRows.length > 0}
              option={dailyOption}
              renderKey={`daily-${chartRevision}`}
              emptyText={t("logs.noData")}
            />
          </article>
          <article className="logs-card">
            <h2>{t("logs.byModel")}</h2>
            <ChartPanel
              hasData={topModels.length > 0}
              option={modelOption}
              renderKey={`model-${chartRevision}`}
              emptyText={t("logs.noData")}
            />
          </article>
          <article className="logs-card">
            <h2>{t("logs.byStage")}</h2>
            <ChartPanel
              hasData={stageRows.length > 0}
              option={stageOption}
              renderKey={`stage-${chartRevision}`}
              emptyText={t("logs.noData")}
            />
          </article>
        </section>

        <section className="logs-card">
          <div className="logs-table-header">
            <h2>{t("logs.recent")}</h2>
            <div className="logs-filters">
              <Select value={filterStage || "__all__"} onValueChange={(value) => setFilterStage(value === "__all__" ? "" : value)}>
                <SelectTrigger className="logs-filter-select">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                <SelectItem value="__all__">{t("logs.filterStage")}</SelectItem>
                {uniqueStages.map((s) => (
                  <SelectItem key={s} value={s}>{translateStageStatus(s, locale, "logs")}</SelectItem>
                ))}
                </SelectContent>
              </Select>
              <Select value={filterModel || "__all__"} onValueChange={(value) => setFilterModel(value === "__all__" ? "" : value)}>
                <SelectTrigger className="logs-filter-select">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                <SelectItem value="__all__">{t("logs.filterModel")}</SelectItem>
                {uniqueModels.map((m) => (
                  <SelectItem key={m} value={m}>{m}</SelectItem>
                ))}
                </SelectContent>
              </Select>
              <Input
                className="logs-filter-input"
                type="text"
                placeholder={t("logs.filterPage")}
                value={filterPage}
                onChange={(e) => setFilterPage(e.target.value.replace(/\D/g, ""))}
              />
              <Input
                className="logs-filter-input"
                type="text"
                placeholder={t("logs.filterJob")}
                value={filterJob}
                onChange={(e) => setFilterJob(e.target.value)}
              />
              {(filterStage || filterModel || filterPage || filterJob) ? (
                <Button type="button" variant="outline" size="sm" className="logs-filter-clear" onClick={clearFilters}>
                  {t("logs.clearFilters")}
                </Button>
              ) : null}
            </div>
          </div>
          <div className="logs-table-wrap">
            <table className="logs-table">
              <thead>
                <tr>
                  <th>{t("logs.time")}</th>
                  <th>{t("logs.provider")}</th>
                  <th>{t("logs.model")}</th>
                  <th>{t("logs.stage")}</th>
                  <th>{t("logs.job")}</th>
                  <th>{t("logs.page")}</th>
                  <th>{t("logs.attempt")}</th>
                  <th>{t("logs.promptTokens")}</th>
                  <th>{t("logs.completionTokens")}</th>
                  <th>{t("logs.totalTokens")}</th>
                  <th>ms</th>
                </tr>
              </thead>
              <tbody>
                {filteredRecords.map((r) => {
                  const recordKey = usageRecordKey(r);
                  const isSelected = selectedRecordKey === recordKey;
                  return (
                  <tr
                    key={recordKey}
                    className={isSelected ? "logs-row-selected" : ""}
                    onClick={() => setSelectedRecord(isSelected ? null : r)}
                  >
                    <td>{formatter.format(new Date(r.ts))}</td>
                    <td>{r.provider}</td>
                    <td>{r.model}</td>
                    <td>{r.stage ? translateStageStatus(r.stage, locale, "logs") : "-"}</td>
                    <td title={r.job_id ?? ""}>
                      {r.job_id ? r.job_id.slice(0, 8) : "-"}
                    </td>
                    <td>{r.page ?? "-"}</td>
                    <td>{r.attempt}</td>
                    <td>{formatNumber(r.prompt_tokens)}</td>
                    <td>{formatNumber(r.completion_tokens)}</td>
                    <td>{formatNumber(r.total_tokens)}</td>
                    <td>{r.duration_ms}</td>
                  </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
          {selectedRecord ? (
            <div className="logs-detail-panel">
              <div className="logs-detail-header">
                <span className="logs-detail-title">{t("logs.recordDetail")}</span>
                <button type="button" className="logs-detail-close" onClick={() => setSelectedRecord(null)}>×</button>
              </div>
              <div className="logs-detail-grid">
                <div className="logs-detail-item">
                  <span className="logs-detail-label">{t("logs.time")}</span>
                  <span className="logs-detail-value">{formatter.format(new Date(selectedRecord.ts))}</span>
                </div>
                <div className="logs-detail-item">
                  <span className="logs-detail-label">{t("logs.provider")}</span>
                  <span className="logs-detail-value">{selectedRecord.provider}</span>
                </div>
                <div className="logs-detail-item">
                  <span className="logs-detail-label">{t("logs.model")}</span>
                  <span className="logs-detail-value">{selectedRecord.model}</span>
                </div>
                <div className="logs-detail-item">
                  <span className="logs-detail-label">{t("logs.stage")}</span>
                  <span className="logs-detail-value">{selectedRecord.stage ? translateStageStatus(selectedRecord.stage, locale, "logs") : "-"}</span>
                </div>
                <div className="logs-detail-item">
                  <span className="logs-detail-label">{t("logs.job")}</span>
                  <span className="logs-detail-value logs-detail-mono">{selectedRecord.job_id ?? "-"}</span>
                </div>
                <div className="logs-detail-item">
                  <span className="logs-detail-label">{t("logs.page")}</span>
                  <span className="logs-detail-value">{selectedRecord.page ?? "-"}</span>
                </div>
                <div className="logs-detail-item">
                  <span className="logs-detail-label">{t("logs.attempt")}</span>
                  <span className="logs-detail-value">{selectedRecord.attempt}</span>
                </div>
                <div className="logs-detail-item">
                  <span className="logs-detail-label">{t("logs.duration")}</span>
                  <span className="logs-detail-value">{selectedRecord.duration_ms} ms</span>
                </div>
                <div className="logs-detail-item">
                  <span className="logs-detail-label">{t("logs.promptTokens")}</span>
                  <span className="logs-detail-value">{formatNumber(selectedRecord.prompt_tokens)}</span>
                </div>
                <div className="logs-detail-item">
                  <span className="logs-detail-label">{t("logs.completionTokens")}</span>
                  <span className="logs-detail-value">{formatNumber(selectedRecord.completion_tokens)}</span>
                </div>
                <div className="logs-detail-item">
                  <span className="logs-detail-label">{t("logs.totalTokens")}</span>
                  <span className="logs-detail-value">{formatNumber(selectedRecord.total_tokens)}</span>
                </div>
              </div>
            </div>
          ) : null}
        </section>
      </section>
    </Layout>
  );
}

const STAGE_COLORS = [
  "#1a5ad7",
  "#06b6d4",
  "#0891b2",
  "#4f7dd8",
  "#8aa0bd",
  "#94a3b8",
  "#0f766e",
  "#64748b",
];

function ChartPanel({
  hasData,
  option,
  renderKey,
  emptyText,
}: {
  hasData: boolean;
  option: EChartsOption;
  renderKey: string;
  emptyText: string;
}) {
  if (!hasData) {
    return <p className="muted-copy">{emptyText}</p>;
  }
  return (
    <div className="logs-chart-shell">
      <ReactECharts
        key={renderKey}
        option={option}
        notMerge
        lazyUpdate
        opts={{ renderer: "svg" }}
        className="logs-chart"
      />
    </div>
  );
}

function SummaryCard({ label, value }: { label: string; value: number }) {
  return (
    <div className="logs-summary-card">
      <span className="logs-summary-label">{label}</span>
      <span className="logs-summary-value">{formatNumber(value)}</span>
    </div>
  );
}

function mergeRow<T>(
  prev: T[],
  keyField: string,
  keyValue: string,
  rec: UsageRecord,
): T[] {
  const idx = prev.findIndex((r) => (r as Record<string, unknown>)[keyField] === keyValue);
  if (idx === -1) {
    return [
      {
        [keyField]: keyValue,
        calls: 1,
        prompt_tokens: rec.prompt_tokens,
        completion_tokens: rec.completion_tokens,
        total_tokens: rec.total_tokens,
      } as unknown as T,
      ...prev,
    ];
  }
  const row = prev[idx] as unknown as {
    calls: number;
    prompt_tokens: number;
    completion_tokens: number;
    total_tokens: number;
  };
  const next = [...prev];
  next[idx] = {
    ...prev[idx],
    calls: row.calls + 1,
    prompt_tokens: row.prompt_tokens + rec.prompt_tokens,
    completion_tokens: row.completion_tokens + rec.completion_tokens,
    total_tokens: row.total_tokens + rec.total_tokens,
  } as unknown as T;
  return next;
}
