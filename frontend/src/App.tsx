import { useEffect, useState } from "react";
import { Route, Routes, useLocation } from "react-router-dom";
import { GeneratePage } from "./pages/GeneratePage";
import { LogsPage } from "./pages/LogsPage";
import { ResultPage } from "./pages/ResultPage";
import { TemplatesPage } from "./pages/TemplatesPage";

export default function App() {
  const location = useLocation();
  const [routeAnimating, setRouteAnimating] = useState(false);

  useEffect(() => {
    setRouteAnimating(true);
    const timer = window.setTimeout(() => setRouteAnimating(false), 360);
    return () => window.clearTimeout(timer);
  }, [location.pathname, location.search]);

  return (
    <>
      <div className={`route-loading-bar ${routeAnimating ? "route-loading-bar-active" : ""}`} />
      <div key={`${location.pathname}${location.search}`} className="route-motion-layer">
        <Routes location={location}>
          <Route path="/" element={<GeneratePage />} />
          <Route path="/generate" element={<GeneratePage />} />
          <Route path="/result" element={<ResultPage />} />
          <Route path="/logs" element={<LogsPage />} />
          <Route path="/templates" element={<TemplatesPage />} />
          <Route path="*" element={<GeneratePage />} />
        </Routes>
      </div>
    </>
  );
}
