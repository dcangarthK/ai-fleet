import { useEffect, useState } from "react";

const isVisible = () => typeof document === "undefined" || document.visibilityState !== "hidden";

/** False while the tab is hidden; loops should run only when true. */
export function useVisible(): boolean {
  const [visible, setVisible] = useState(isVisible);
  useEffect(() => {
    const on = () => setVisible(isVisible());
    on();
    document.addEventListener("visibilitychange", on);
    return () => document.removeEventListener("visibilitychange", on);
  }, []);
  return visible;
}
