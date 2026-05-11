import { Route, Routes } from "react-router-dom";
import { GeneratePage } from "./pages/GeneratePage";
import { LogsPage } from "./pages/LogsPage";
import { ResultPage } from "./pages/ResultPage";

export default function App() {
  return (
    <Routes>
      <Route path="/" element={<GeneratePage />} />
      <Route path="/generate" element={<GeneratePage />} />
      <Route path="/result" element={<ResultPage />} />
      <Route path="/logs" element={<LogsPage />} />
      <Route path="*" element={<GeneratePage />} />
    </Routes>
  );
}
