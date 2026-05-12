import { Link, useLocation } from "react-router-dom";
import { useEffect } from "react";
import { useLocale } from "../../i18n";
import { useGeneration } from "../../hooks/useGeneration";
import { useTheme } from "../../hooks/useTheme";
import {
  Languages,
  LayoutDashboard,
  Moon,
  Plus,
  Sun,
  TableProperties,
} from "lucide-react";
import { Button } from "../ui/button";
import { Separator } from "../ui/separator";
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from "../ui/tooltip";

export function Header() {
  const { t, locale, toggleLocale } = useLocale();
  const { theme, toggleTheme } = useTheme();
  const { backendStatus, checkBackendStatus } = useGeneration();
  const location = useLocation();
  const isWorkspace = location.pathname === "/" || location.pathname === "/generate" || location.pathname === "/result";
  const isLogs = location.pathname === "/logs";
  const statusLabel =
    backendStatus === "connected"
      ? t("status.connected")
      : backendStatus === "connecting"
        ? t("status.connecting")
        : t("status.disconnected");

  useEffect(() => {
    void checkBackendStatus();
    const timer = window.setInterval(() => {
      void checkBackendStatus();
    }, 10000);
    return () => window.clearInterval(timer);
  }, [checkBackendStatus]);

  return (
    <header className="app-header">
      <div className="app-header-left">
        <Link to="/generate" className="brand-mark" aria-label="Paper PPT Agent">
          <span className="brand-bars" aria-hidden="true">
            <span />
            <span />
            <span />
          </span>
          <span className="brand-name">Paper PPT Agent</span>
        </Link>
        <nav className="workspace-nav" aria-label="Primary">
          <Link
            to="/generate?fresh=1"
            className={`workspace-nav-item ${isWorkspace ? "workspace-nav-item-active" : ""}`}
          >
            <LayoutDashboard size={17} strokeWidth={1.8} />
            <span>{t("nav.workspace")}</span>
          </Link>
          <Link
            to="/logs"
            className={`workspace-nav-item ${isLogs ? "workspace-nav-item-active" : ""}`}
          >
            <TableProperties size={17} strokeWidth={1.8} />
            <span>{t("nav.tokenLogs")}</span>
          </Link>
        </nav>
      </div>
      <div className="app-header-actions">
        <span className={`system-status system-status-${backendStatus}`}>
          <span className="system-status-dot" />
          {statusLabel}
        </span>
        <Separator className="header-separator h-6 w-px" />
        <TooltipProvider delayDuration={100}>
          <Tooltip>
            <TooltipTrigger asChild>
              <Button variant="ghost" size="icon" type="button" onClick={toggleLocale}>
                <Languages size={18} />
              </Button>
            </TooltipTrigger>
            <TooltipContent>{locale === "en" ? t("locale.zh") : t("locale.en")}</TooltipContent>
          </Tooltip>
          <Tooltip>
            <TooltipTrigger asChild>
              <Button variant="ghost" size="icon" type="button" onClick={toggleTheme}>
                {theme === "light" ? <Moon size={18} /> : <Sun size={18} />}
              </Button>
            </TooltipTrigger>
            <TooltipContent>{theme === "light" ? t("theme.dark") : t("theme.light")}</TooltipContent>
          </Tooltip>
        </TooltipProvider>
        <Button className="h-10 rounded-md bg-primary px-4 text-primary-foreground shadow-sm" asChild>
          <Link to="/generate?fresh=1">
            <Plus size={17} />
            <span>{t("action.newPptTask")}</span>
          </Link>
        </Button>
      </div>
    </header>
  );
}
