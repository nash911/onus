import "@fontsource-variable/inter";
import "@fontsource-variable/source-serif-4";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { StrictMode, Suspense } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter, Navigate, Route, Routes, useLocation } from "react-router-dom";
import { ApiError } from "./api/client";
import { Layout } from "./components/Layout";
import { Loading } from "./components/States";
import { redirectFor } from "./lib/routes";
import { installTracking } from "./lib/track";
import Home from "./pages/Home";
import NotFound from "./pages/NotFound";
import ReportPage from "./pages/Report";
import "./index.css";

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 30_000,
      refetchOnWindowFocus: false,
      retry: (count, err) => !(err instanceof ApiError && err.status >= 400 && err.status < 500) && count < 2,
    },
  },
});

/** Old routes (/live/:id, /corpus, /findings, /trust, /method, /papers/*) go to the two pages; see lib/routes.ts. */
function Fallback() {
  const loc = useLocation();
  const to = redirectFor(loc.pathname);
  return to ? <Navigate to={{ pathname: to, search: loc.search }} replace /> : <NotFound />;
}

installTracking();

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <Suspense fallback={<Loading />}>
          <Routes>
            <Route element={<Layout />}>
              <Route index element={<Home />} />
              <Route path="analyses/:id" element={<ReportPage />} />
              <Route path="*" element={<Fallback />} />
            </Route>
          </Routes>
        </Suspense>
      </BrowserRouter>
    </QueryClientProvider>
  </StrictMode>,
);
