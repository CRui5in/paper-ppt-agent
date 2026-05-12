import { useState, type PropsWithChildren } from "react";
import { ErrorBanner } from "./ErrorBanner";
import { Header } from "./Header";
import { Sidebar } from "./Sidebar";

interface LayoutProps extends PropsWithChildren {
  showSidebar?: boolean;
  contentClassName?: string;
}

export function Layout({ children, showSidebar = true, contentClassName = "" }: LayoutProps) {
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false);
  return (
    <div className={`app-shell ${sidebarCollapsed ? "sidebar-collapsed" : ""}`}>
      <Header />
      <ErrorBanner />
      <div className={`app-body ${showSidebar ? "app-body-with-sidebar" : "app-body-no-sidebar"}`}>
        {showSidebar ? (
          <Sidebar collapsed={sidebarCollapsed} onCollapsedChange={setSidebarCollapsed} />
        ) : null}
        <main className={`app-content motion-page ${showSidebar ? "app-content-with-sidebar" : "app-content-full"} ${contentClassName}`.trim()}>
          <div className={`app-content-inner ${showSidebar ? "app-content-inner-wide" : "app-content-inner-centered"}`}>
            {children}
          </div>
        </main>
      </div>
    </div>
  );
}
