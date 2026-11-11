import React from "react";
import ReactDOM from "react-dom/client";
import { QueryClientProvider } from "@tanstack/react-query";
import { createBrowserRouter, RouterProvider } from "react-router-dom";
import "@fontsource/ibm-plex-sans/400.css";
import "@fontsource/ibm-plex-sans/500.css";
import "@fontsource/ibm-plex-sans/600.css";
import "@fontsource/ibm-plex-mono/400.css";
import "./styles.css";
import { Shell } from "./components/Shell";
import { queryClient } from "./lib/data";
import { OverviewPage } from "./pages/Overview";
import { TasksPage } from "./pages/Tasks";
import { NodeDetailPage, NodesPage } from "./pages/Nodes";
import { RolesPage } from "./pages/Roles";
import { ModelsPage } from "./pages/Models";
import { ActivityPage, LaterPage, SettingsPage } from "./pages/Misc";
import { PoliciesPage } from "./pages/Policies";

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
        { index: true, element: <OverviewPage /> },
        { path: "tasks", element: <TasksPage /> },
        { path: "models", element: <ModelsPage /> },
        { path: "roles", element: <RolesPage /> },
        { path: "nodes", element: <NodesPage /> },
        { path: "nodes/:id", element: <NodeDetailPage /> },
        { path: "policies", element: <PoliciesPage /> },
        { path: "skills", element: <LaterPage title="Skills" what="Reusable, versioned instructions you bind to the fleet, a node, a role or a task." /> },
        { path: "evals", element: <LaterPage title="Evals" what="Score models per role on your own hardware, and get suggested role assignments." /> },
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
      <RouterProvider router={router} />
    </QueryClientProvider>
  </React.StrictMode>,
);
