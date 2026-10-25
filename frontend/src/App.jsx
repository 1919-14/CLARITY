import { Route, Routes } from 'react-router-dom';

import { AppShell } from './components/layout/AppShell.jsx';
import { BenchmarksPage } from './pages/BenchmarksPage.jsx';
import { CreateRunPage } from './pages/CreateRunPage.jsx';
import { DashboardPage } from './pages/DashboardPage.jsx';
import { HypothesesPage } from './pages/HypothesesPage.jsx';
import { NotFoundPage } from './pages/NotFoundPage.jsx';
import { OracleLibraryPage } from './pages/OracleLibraryPage.jsx';
import { ReportsPage } from './pages/ReportsPage.jsx';
import { RunDetailsPage } from './pages/RunDetailsPage.jsx';

function App() {
  return (
    <Routes>
      <Route element={<AppShell />}>
        <Route index element={<DashboardPage />} />
        <Route path="runs/new" element={<CreateRunPage />} />
        <Route path="runs/:runId" element={<RunDetailsPage />} />
        <Route path="hypotheses" element={<HypothesesPage />} />
        <Route path="oracles" element={<OracleLibraryPage />} />
        <Route path="reports" element={<ReportsPage />} />
        <Route path="benchmarks" element={<BenchmarksPage />} />
        <Route path="*" element={<NotFoundPage />} />
      </Route>
    </Routes>
  );
}

export default App;
