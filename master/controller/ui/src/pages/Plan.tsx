import { Card, Empty } from "../components/ui";
import { PageHead } from "../components/bits";

/** Placeholder: replaced by the Plan page (orchestrator plan and leader state). */
export function PlanPage() {
  return (
    <>
      <PageHead title="Plan" intro="The orchestrator plan and leader state." />
      <Card><Empty title="Plan is being built">Plan items and leader state arrive here.</Empty></Card>
    </>
  );
}
