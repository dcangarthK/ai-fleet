import React from "react";
import ReactDOM from "react-dom/client";
import { QueryClientProvider } from "@tanstack/react-query";
import { createBrowserRouter, RouterProvider } from "react-router-dom";
import "./styles.css";
import { Shell } from "./components/Shell";
import { queryClient } from "./lib/data";
import { NowPage } from "./pages/Now";
import { WorkPage } from "./pages/Work";
import { WorkDetailPage } from "./pages/WorkDetail";
import { PlanPage } from "./pages/Plan";
import { LegacyRedirect } from "./components/LegacyRedirect";
import { ConfirmProvider } from "./components/ui";
import { NodeDetailPage, NodesPage } from "./pages/Nodes";
import { RolesPage } from "./pages/Roles";
import { ModelsPage } from "./pages/Models";
import { ActivityPage, LaterPage, SettingsPage } from "./pages/Misc";
import { PoliciesPage } from "./pages/Policies";
import { SkillsPage } from "./pages/Skills";
import { EvalsPage } from "./pages/Evals";
import { BudgetPage } from "./pages/Budget";

try {
  const t = localStorage.getItem("fleet-theme");
  if (t === "light" || t === "dark") document.documentElement.setAttribute("data-theme", t);
} catch {
  /* storage blocked: follow the system */
}

const router = createBrowserRouter(
  [
    {
      path: "/",
      element: <Shell />,
      children: [
        { index: true, element: <NowPage /> },
        { path: "work", element: <WorkPage /> },
        { path: "work/:root", element: <WorkDetailPage /> },
        { path: "plan", element: <PlanPage /> },
        { path: "live", element: <LegacyRedirect /> },
        { path: "overview", element: <LegacyRedirect /> },
        { path: "tasks", element: <LegacyRedirect /> },
        { path: "pipelines", element: <LegacyRedirect /> },
        { path: "budget", element: <BudgetPage /> },
        { path: "models", element: <ModelsPage /> },
        { path: "roles", element: <RolesPage /> },
        { path: "nodes", element: <NodesPage /> },
        { path: "nodes/:id", element: <NodeDetailPage /> },
        { path: "policies", element: <PoliciesPage /> },
        { path: "skills", element: <SkillsPage /> },
        { path: "evals", element: <EvalsPage /> },
        { path: "activity", element: <ActivityPage /> },
        { path: "settings", element: <SettingsPage /> },
        { path: "*", element: <LaterPage title="Page not found" what="That address isn't part of the console." /> },
      ],
    },
  ],
  { basename: "/app" },
);

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <QueryClientProvider client={queryClient}>
      <ConfirmProvider>
        <RouterProvider router={router} />
      </ConfirmProvider>
    </QueryClientProvider>
  </React.StrictMode>,
);
