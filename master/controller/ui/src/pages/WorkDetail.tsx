import { useParams } from "react-router-dom";
import { Card, Empty, Truncate } from "../components/ui";
import { PageHead } from "../components/bits";

/** Placeholder: replaced by the work item detail (chain strip, attempts, log, diff, gate, actions). */
export function WorkDetailPage() {
  const { root = "" } = useParams();
  return (
    <>
      <PageHead title="Work item" intro={<Truncate text={root} className="mono" />} />
      <Card><Empty title="Work detail is being built">Timeline, diff, gate and actions arrive here.</Empty></Card>
    </>
  );
}
