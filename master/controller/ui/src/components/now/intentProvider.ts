// Registers the intent provider in the palette (lib/palette.ts). Imported once by pages/Now.tsx, which main.tsx loads
// at startup. Choosing an item only navigates to `/?intent=…`; Now's <IntentHost> shows the request and asks to confirm.
import { queryClient } from "../../lib/data";
import { parseIntent } from "../../lib/intent";
import { registerPaletteProvider } from "../../lib/palette";
import { INTENT_PARAM, intentCtx } from "./Intent";

registerPaletteProvider({
  id: "intent",
  items(query) {
    const intent = parseIntent(query, intentCtx((k) => queryClient.getQueryData(k as never)));
    if (!intent) return [];
    const q = query.trim();
    return [{
      id: `intent:${q}`, group: "Intent", title: intent.request ? intent.summary : `${intent.summary} — ${intent.warnings[0] ?? "can't run"}`,
      hint: intent.request ? "Preview, then confirm" : "Not runnable", keywords: q,
      to: intent.request ? `/?${INTENT_PARAM}=${encodeURIComponent(q)}` : undefined,
    }];
  },
});
