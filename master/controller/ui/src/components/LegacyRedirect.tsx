import { Navigate, useLocation } from "react-router-dom";
import { resolveLegacy } from "../lib/nav";

/** Old routes (/live, /tasks, /pipelines, /overview) forward to their new homes; `?task=` survives. */
export function LegacyRedirect() {
  const { pathname, search } = useLocation();
  return <Navigate to={resolveLegacy(pathname, search) ?? "/"} replace />;
}
