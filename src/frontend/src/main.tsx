import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { StrictMode, Suspense, lazy } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter, Navigate, Route, Routes } from "react-router-dom";
import { ApiError } from "./api/client";
import { Layout } from "./components/Layout";
import { Loading } from "./components/States";
import Corpus from "./pages/Corpus";
import Home from "./pages/Home";
import LiveProgress from "./pages/LiveProgress";
import Method from "./pages/Method";
import NotFound from "./pages/NotFound";
import { AnalysisReport, PaperReport } from "./pages/Report";
import "./index.css";

// The chart pages pull in Recharts; load them on demand.
const Findings = lazy(() => import("./pages/Findings"));
const Trust = lazy(() => import("./pages/Trust"));

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 30_000,
      refetchOnWindowFocus: false,
      retry: (count, err) => !(err instanceof ApiError && err.status >= 400 && err.status < 500) && count < 2,
    },
  },
});

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <Suspense fallback={<Loading />}>
          <Routes>
            <Route element={<Layout />}>
              <Route index element={<Home />} />
              <Route path="corpus" element={<Corpus />} />
              <Route path="papers/:ref" element={<PaperReport />} />
              <Route path="analyses/:id" element={<AnalysisReport />} />
              <Route path="live/:id" element={<LiveProgress />} />
              <Route path="findings" element={<Findings />} />
              <Route path="trust" element={<Trust />} />
              <Route path="method" element={<Method />} />
              <Route path="home" element={<Navigate to="/" replace />} />
              <Route path="*" element={<NotFound />} />
            </Route>
          </Routes>
        </Suspense>
      </BrowserRouter>
    </QueryClientProvider>
  </StrictMode>,
);
