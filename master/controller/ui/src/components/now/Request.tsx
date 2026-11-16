import { Fragment, type ReactNode } from "react";
import type { ApiRequest } from "../../lib/insights";
import type { IntentField } from "../../lib/intent";

export function Kv({ fields }: { fields: IntentField[] }) {
  return <dl className="kv">{fields.map((f) => <Fragment key={f.label}><dt>{f.label}</dt><dd>{f.value}</dd></Fragment>)}</dl>;
}

/** The exact request that will be sent, shown in every confirmation. */
export function RequestPreview({ request, fields, note }: { request: ApiRequest; fields?: IntentField[]; note?: ReactNode }) {
  return (
    <div className="now">
      {note && <p style={{ margin: 0 }}>{note}</p>}
      {fields && fields.length > 0 && <Kv fields={fields} />}
      <pre className="req" aria-label="Request">{`${request.method} ${request.path}${request.body === undefined ? "" : `\n${JSON.stringify(request.body, null, 2)}`}`}</pre>
    </div>
  );
}
